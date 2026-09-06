"""
app.py — RideIQ REST API (FastAPI). A ride-hailing "brain" where all 15 ML algorithms
power real product features. Same live-product architecture as the other backends:

  REST / ML API : /quote, /predict-eta, /estimate-fare, /cancellation-risk, /fraud-check,
                  /surge-zones, /rider-segments, /driver-shift, /cancellation-causes,
                  /pipeline, /health, /metrics  (+ OpenAPI docs at /docs)
  Caching       : Redis (analytics endpoints are cached; identical quotes too)
  Database      : SQLite logs every quote
  Load balancing: stateless -> nginx across replicas (docker-compose.yml)
  Reliability   : /health, /metrics

Run:  uvicorn app:app --host 0.0.0.0 --port 8000
"""
import os
import time
import json
import hashlib
import sqlite3
import urllib.parse
import urllib.request
from collections import OrderedDict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import mlloop as ML
import models as M
import places as P
import resilience as RES
import routing as R
import transit_client as T

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "rideiq.db"))
REDIS_URL = os.environ.get("REDIS_URL")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "3600"))
INSTANCE = os.environ.get("INSTANCE_ID", "api-1")

API_KEY = os.environ.get("API_KEY", "").strip()
# Browsers are the only thing CORS constrains, and the only client here is a
# native Android app, which CORS does not apply to. So the default is "no browser
# origin", not "*" — set ALLOWED_ORIGINS if a web client ever appears.
ALLOWED_ORIGINS = [o for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o]

app = FastAPI(title="RideIQ API", version="1.0",
              description="Ride-hailing platform brain — 16 ML algorithms behind real features.")
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
                   allow_methods=["*"], allow_headers=["*"])


# Endpoints anyone may call without a key: the load balancer's health probe, and
# the OpenAPI docs, which are the point of a portfolio project.
OPEN_PATHS = {"/health", "/metrics", "/docs", "/openapi.json", "/redoc", "/"}


@app.middleware("http")
async def require_api_key(request, call_next):
    """Gate the expensive endpoints behind a shared key.

    Be clear about what this is and is not. The key ships inside a public APK, so
    anyone determined can extract it — this is not authentication, and calling it
    that would be a lie. What it does do is stop casual scraping and drive-by
    scripts, and it gives us something to rotate if a key does leak. The real
    protection against abuse is the per-IP rate limit at nginx, in front of this.

    Unset API_KEY disables the check entirely, so local development and CI keep
    working without ceremony.
    """
    if API_KEY and request.url.path not in OPEN_PATHS:
        if request.headers.get("X-API-Key") != API_KEY:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "missing or invalid X-API-Key"},
                                status_code=401)
    return await call_next(request)

# ---- cache ----
_local, _redis = OrderedDict(), None
if REDIS_URL:
    try:
        import redis
        _redis = redis.from_url(REDIS_URL, decode_responses=True); _redis.ping()
    except Exception:
        _redis = None


def cache_get(k):
    if _redis:
        v = _redis.get(k); return json.loads(v) if v else None
    return _local.get(k)


def cache_set(k, v, ttl=None):
    if _redis:
        _redis.setex(k, ttl or CACHE_TTL, json.dumps(v))
    else:
        _local[k] = v
        if len(_local) > 200:
            _local.popitem(last=False)


# ---- db ----
def db():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c


with db() as _c:
    ML.ensure_schema(_c)

# The challenger is loaded once and scored in the background. Absent on a fresh
# deployment, which is the normal state: there is nothing to train one on until
# real trip outcomes have been collected.
CHALLENGER = ML.Challenger()
SHADOW = ML.ShadowRunner(CHALLENGER, db)

METRICS = {"requests": 0, "cache_hits": 0}


# ---- schemas ----
class EtaIn(BaseModel):
    distance: float = 8.0; hour: int = 8; weather: int = 0; traffic: float = 0.5


class FareIn(BaseModel):
    distance: float = 8.0; duration: float = 20.0; surge: float = 1.0


class CancelIn(BaseModel):
    wait_time: float = 3.0; surge: float = 1.0; traffic: float = 0.5; rider_rating: float = 4.6


class FraudIn(BaseModel):
    payment: float = 20.0; distance: float = 8.0; duration: float = 20.0; surge: float = 1.0


class QuoteIn(BaseModel):
    distance: float = 8.0; hour: int = 8; weather: int = 0
    traffic: float = 0.5; surge: float = 1.0; rider_rating: float = 4.6


class RouteIn(BaseModel):
    # origin (ax, ay) and destination (bx, by) as city coords in 0..1
    ax: float = 0.1; ay: float = 0.1; bx: float = 0.9; by: float = 0.9
    hour: int = 8; weather: int = 0; traffic: float = 0.5; surge: float = 1.0
    mode: str = "drive"          # "drive" or "walk"


class TripOutcomeIn(BaseModel):
    """What actually happened, reported by the app when a trip ends."""
    quote_id: int
    actual_min: float
    actual_fare: float = None
    completed: bool = True
    source: str = "app"


class RouteLatLonIn(BaseModel):
    lat1: float; lon1: float; lat2: float; lon2: float
    hour: int = 8; weather: int = 0; traffic: float = 0.5; surge: float = 1.0
    mode: str = "drive"          # "drive" or "walk"


WALK_SPEED_KMH = 4.8             # average walking pace, for walk-mode ETA


def _price_route(rt, i, endpoint=None, log=False):
    """Attach ETA + fare to a route result, mode-aware. Walking has an ETA but no fare.

    `log` writes the prediction and its features to the quotes table and puts the
    returned id on the response as quote_id. This is the input to the whole
    feedback loop and it did not exist: /quote logged, but the app never calls
    /quote -- every prediction a rider has ever seen came through here and
    vanished. Only the primary route is logged, not the alternatives, because an
    alternative the rider did not take has no outcome to wait for.
    """
    if rt.get("mode") == "walk":
        # A walking ETA is distance over a constant. There is no model, so there
        # is nothing to learn and nothing worth logging.
        eta_min = round(rt["distance_km"] / WALK_SPEED_KMH * 60.0, 1)
        return {**rt, "eta_min": eta_min, "fare_usd": 0.0, "instance": INSTANCE}

    eta = M.predict_eta(rt["distance_km"], i.hour, i.weather, i.traffic)
    fare = M.estimate_fare(rt["distance_km"], eta["ensemble_min"], i.surge)
    out = {**rt, "eta_min": eta["ensemble_min"], "fare_usd": fare["random_forest"],
           "instance": INSTANCE}
    if log:
        try:
            with db() as c:
                qid = ML.log_prediction(
                    c, distance=rt["distance_km"], hour=i.hour, weather=i.weather,
                    traffic=i.traffic, surge=i.surge, eta=eta["ensemble_min"],
                    fare=fare["random_forest"], instance=INSTANCE,
                    mode=rt.get("mode", "drive"), endpoint=endpoint)
            out["quote_id"] = qid
            # Non-blocking. The challenger runs on another thread and cannot
            # slow this response down or break it.
            SHADOW.submit(qid, rt["distance_km"], i.traffic, i.weather, i.hour,
                          eta["ensemble_min"])
        except Exception:
            # Losing a log row must never cost a rider their route.
            pass
    return out


# Preset Edmonton landmarks (real coordinates). Used once a real OSM city is loaded.
LANDMARKS = [
    {"name": "Downtown (Churchill Sq)", "lat": 53.5445, "lon": -113.4909},
    {"name": "University of Alberta",   "lat": 53.5232, "lon": -113.5263},
    {"name": "Airport (YEG)",           "lat": 53.3097, "lon": -113.5797},
    {"name": "West Edmonton Mall",      "lat": 53.5225, "lon": -113.6242},
]


# ---- basic ----
@app.get("/health")
def health():
    return {"status": "ok", "instance": INSTANCE,
            "cache": "redis" if _redis else "local",
            "places": {"loaded": P.LOAD_INFO.get("loaded", False),
                       "entries": P.LOAD_INFO.get("entries", 0)}}


@app.get("/metrics")
def metrics():
    hr = METRICS["cache_hits"] / METRICS["requests"] if METRICS["requests"] else 0
    g = OTP_GUARD.state()
    return {**METRICS, "cache_hit_rate": round(hr, 3),
            "otp_breaker": g["breaker"]["state"],
            "otp_in_flight": g["bulkhead"]["in_flight"],
            "otp_shed": g["bulkhead"]["rejected"] + g["breaker"]["rejected"],
            "instance": INSTANCE}


@app.get("/pipeline")
def pipeline():
    return {"features": M.PIPELINE, "instance": INSTANCE}


# ---- per-feature predictions ----
@app.post("/predict-eta")
def predict_eta(i: EtaIn):
    METRICS["requests"] += 1
    return {**M.predict_eta(i.distance, i.hour, i.weather, i.traffic), "instance": INSTANCE}


@app.post("/estimate-fare")
def estimate_fare(i: FareIn):
    METRICS["requests"] += 1
    return {**M.estimate_fare(i.distance, i.duration, i.surge), "instance": INSTANCE}


@app.post("/cancellation-risk")
def cancellation_risk(i: CancelIn):
    METRICS["requests"] += 1
    return {**M.cancellation_risk(i.wait_time, i.surge, i.traffic, i.rider_rating), "instance": INSTANCE}


@app.post("/fraud-check")
def fraud_check(i: FraudIn):
    METRICS["requests"] += 1
    return {**M.fraud_check(i.payment, i.distance, i.duration, i.surge), "instance": INSTANCE}


@app.get("/graph")
def graph():
    """The road network (nodes + edges) for the app to draw the map once. Cached."""
    return _cached_analytic("an:graph", R.graph_json)


@app.post("/route")
def route(i: RouteIn):
    """Compute a route with A*, then price it: distance → ETA (ML) → fare (ML)."""
    METRICS["requests"] += 1
    routes = R.route_multi(i.ax, i.ay, i.bx, i.by, mode=i.mode, want=3)
    if not routes:
        raise HTTPException(400, "no route found")
    primary = _price_route(routes[0], i, endpoint="/route", log=True)
    primary["alternatives"] = [_price_route(r, i) for r in routes[1:]]
    return primary


@app.get("/landmarks")
def landmarks():
    """Preset Edmonton landmarks + whether real-city routing is available."""
    return {"landmarks": LANDMARKS, "real_city": R.has_latlon(),
            "city": R.CITY_NAME, "instance": INSTANCE}


@app.get("/reverse-geocode")
def reverse_geocode(lat: float, lon: float):
    """Coordinates -> a human street address, via free OSM Nominatim. Cached to respect
    Nominatim's 1 req/sec policy and to keep the app snappy when dragging a pin."""
    key = f"rg:{round(lat, 5)}:{round(lon, 5)}"
    hit = cache_get(key)
    if hit:
        return {**hit, "cached": True, "instance": INSTANCE}
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 18, "addressdetails": 1})
    req = urllib.request.Request(url, headers={"User-Agent": "RideIQ/1.0 (student project)"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise HTTPException(502, f"geocoder unavailable: {e}")
    a = data.get("address", {})
    road = a.get("road") or a.get("pedestrian") or a.get("footway") or ""
    num = a.get("house_number", "")
    area = (a.get("neighbourhood") or a.get("suburb") or a.get("city_district")
            or a.get("city") or a.get("town") or "")
    line1 = (f"{num} {road}").strip()
    short = ", ".join([p for p in [line1, area] if p]) or (data.get("display_name", "")[:60])
    out = {"display_name": data.get("display_name", ""), "short": short, "lat": lat, "lon": lon}
    cache_set(key, out)
    return {**out, "cached": False, "instance": INSTANCE}


def _short_address(a, fallback):
    road = a.get("road") or a.get("pedestrian") or a.get("footway") or ""
    num = a.get("house_number", "")
    area = (a.get("neighbourhood") or a.get("suburb") or a.get("city_district")
            or a.get("city") or a.get("town") or "")
    line1 = (f"{num} {road}").strip()
    return ", ".join([p for p in [line1, area] if p]) or (fallback[:60] if fallback else "")


@app.get("/geocode")
def geocode(q: str):
    """Address/place text -> coordinates, via free OSM Nominatim. Biased to Edmonton so
    results land inside the routable city graph. Cached."""
    q = q.strip()
    if not q:
        raise HTTPException(400, "empty query")
    key = f"gc:{q.lower()}"
    hit = cache_get(key)
    if hit:
        return {**hit, "cached": True, "instance": INSTANCE}

    # A name the gazetteer holds exactly needs no network round trip. Restricted
    # to an exact match on purpose -- /geocode returns ONE coordinate and the app
    # routes straight to it, so a near-miss here silently sends someone to the
    # wrong place. Autocomplete can afford to guess; this cannot.
    for cand in P.search(q, limit=3):
        if cand["display_name"].lower() == q.lower():
            out = {"display_name": cand["display_name"], "short": cand["short"],
                   "lat": cand["lat"], "lon": cand["lon"], "source": "local"}
            cache_set(key, out)
            return {**out, "cached": False, "instance": INSTANCE}

    query = q if "edmonton" in q.lower() else f"{q}, Edmonton, Alberta, Canada"
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1, "countrycodes": "ca"})
    req = urllib.request.Request(url, headers={"User-Agent": "RideIQ/1.0 (student project)"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise HTTPException(502, f"geocoder unavailable: {e}")
    if not data:
        raise HTTPException(404, "address not found in Edmonton")
    top = data[0]
    lat, lon = float(top["lat"]), float(top["lon"])
    short = _short_address(top.get("address", {}), top.get("display_name", ""))
    out = {"display_name": top.get("display_name", ""), "short": short, "lat": lat, "lon": lon}
    cache_set(key, out)
    return {**out, "cached": False, "instance": INSTANCE}


def _nominatim_search(q, limit=5):
    """The old behaviour, now the fallback rather than the whole endpoint."""
    query = q if "edmonton" in q.lower() else f"{q}, Edmonton, Alberta, Canada"
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "jsonv2", "limit": limit,
         "addressdetails": 1, "countrycodes": "ca"})
    req = urllib.request.Request(url, headers={"User-Agent": "RideIQ/1.0 (student project)"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    return [{"lat": float(t["lat"]), "lon": float(t["lon"]),
             "display_name": t.get("display_name", ""),
             "short": _short_address(t.get("address", {}), t.get("display_name", "")),
             "kind": t.get("type") or "address", "source": "nominatim"}
            for t in data]


@app.get("/search")
def search(q: str):
    """Autocomplete: address/place text -> up to 5 Edmonton matches.

    Answered from the local trie first. That is the whole point: this endpoint is
    called on every keystroke, and proxying every keystroke to Nominatim cost
    430 ms and violated their usage policy, which exists precisely to stop people
    using it as a typeahead backend.

    Nominatim is still here, because a gazetteer of stops and street names cannot
    resolve a house number or a business, and quietly returning nothing for those
    would be a worse search. It is now reached only when the local index is thin
    on a query -- which for Edmonton is the minority of them.
    """
    q = q.strip()
    if len(q) < 3:
        return {"results": [], "instance": INSTANCE}

    local = P.search(q, limit=5)
    if len(local) >= 3:
        # Enough good local answers. Never touch the network, never cache: the
        # trie is already faster than reading the cache back out of Redis.
        return {"results": local, "cached": False, "source": "local",
                "instance": INSTANCE}

    key = f"se:{q.lower()}"
    hit = cache_get(key)
    if hit:
        METRICS["cache_hits"] += 1
        return {**hit, "cached": True, "instance": INSTANCE}

    remote = _nominatim_search(q, limit=5 - len(local))
    seen = {r["display_name"] for r in local}
    results = local + [r for r in remote if r["display_name"] not in seen]
    out = {"results": results, "source": "local+nominatim" if local else "nominatim"}
    cache_set(key, out)
    return {**out, "cached": False, "instance": INSTANCE}


@app.post("/route-latlon")
def route_latlon(i: RouteLatLonIn):
    """Route between two real-world points (landmarks). Requires a real OSM city loaded."""
    METRICS["requests"] += 1
    if not R.has_latlon():
        raise HTTPException(400, "Landmark routing needs the real city map. "
                                 "Run build_city_graph.py to create city_graph.json, then restart.")
    routes = R.route_latlon_multi(i.lat1, i.lon1, i.lat2, i.lon2, mode=i.mode, want=3)
    if not routes:
        raise HTTPException(400, "no route found")
    primary = _price_route(routes[0], i, endpoint="/route-latlon", log=True)
    primary["alternatives"] = [_price_route(r, i) for r in routes[1:]]
    return primary


# ---- public transit (OpenTripPlanner behind us; see transit/README.md) ----
# A "leave now" plan goes stale as the next bus gets closer, so transit answers
# get a much shorter TTL than the hour we give analytics.
TRANSIT_TTL = int(os.environ.get("TRANSIT_CACHE_TTL", "120"))
TRANSIT_BUCKET_S = 300           # round the query clock to 5 min so repeats hit

# How many requests may be inside OTP at once. FastAPI runs sync endpoints in a
# threadpool of 40, and /transit blocks for up to OTP_TIMEOUT seconds, so without
# a cap a slow OTP parks every thread and takes /health and /quote down with it.
# 8 leaves 32 threads for everything that has nothing to do with transit.
OTP_CONCURRENCY = int(os.environ.get("OTP_CONCURRENCY", "8"))
OTP_FAIL_THRESHOLD = int(os.environ.get("OTP_FAIL_THRESHOLD", "5"))
OTP_BREAKER_RESET_S = float(os.environ.get("OTP_BREAKER_RESET_S", "30"))

# Only TransitUnavailable counts as OTP's fault. urllib raises it for both a
# refused connection and a timeout, which are exactly the two things a breaker
# should react to. A RuntimeError from OTP rejecting a GraphQL query is OUR bug:
# it will fail identically on every retry, and letting it open the circuit would
# mean one malformed request disables transit for everybody.
OTP_GUARD = RES.Guard("opentripplanner", limit=OTP_CONCURRENCY,
                      failure_types=(T.TransitUnavailable,),
                      fail_threshold=OTP_FAIL_THRESHOLD,
                      reset_after=OTP_BREAKER_RESET_S)


def _otp_unavailable(e):
    """Map any "transit is not answering" case onto a 503 the app already handles.

    Retry-After is not decoration. When the breaker is open it knows exactly how
    long it intends to stay that way, and telling the client instead of making it
    guess is the difference between a backoff and a retry storm. Every 503 from
    these endpoints carries one, including a plain unreachable-OTP failure --
    a client that has no idea when to come back will come back immediately.
    """
    retry = 5
    st = OTP_GUARD.breaker.state()
    if isinstance(e, RES.CircuitOpen):
        retry = max(1, int(st.get("retry_in_s") or st["cooldown_s"]))
    elif isinstance(e, RES.Overloaded):
        retry = 2                      # a spike passes; do not send them far away
    elif st["state"] == "open":
        # The call that just tripped the breaker. Nothing will get through until
        # the cooldown elapses, so say so.
        retry = max(1, int(st["cooldown_s"]))
    raise HTTPException(503, str(e), headers={"Retry-After": str(retry)})


@app.get("/transit")
def transit(lat1: float, lon1: float, lat2: float, lon2: float,
            depart: str = "now", arrive_by: bool = False,
            max_walk_m: int = 800, wheelchair: bool = False, want: int = 3):
    """Plan a public-transit trip: walk legs, which route to ride, and the clock.

    Unlike /route-latlon this is time-dependent — the answer depends on when you
    ask, because you have to catch the bus. OpenTripPlanner does the routing over
    Edmonton's GTFS feed; we normalize its itineraries into RideIQ legs.
    """
    METRICS["requests"] += 1
    # Bucket "now" so two riders asking the same trip seconds apart share a result.
    stamp = (int(time.time()) // TRANSIT_BUCKET_S) if depart == "now" else depart
    key = "tr:%.5f,%.5f,%.5f,%.5f,%s,%s,%d,%s,%d" % (
        lat1, lon1, lat2, lon2, stamp, arrive_by, max_walk_m, wheelchair, want)
    hit = cache_get(key)
    if hit:
        METRICS["cache_hits"] += 1
        return {**hit, "cached": True, "instance": INSTANCE}

    try:
        # Guarded, not bare. Everything inside is one blocking network call.
        with OTP_GUARD:
            out = T.plan(lat1, lon1, lat2, lon2, depart=depart, arrive_by=arrive_by,
                         max_walk_m=max_walk_m, wheelchair=wheelchair, want=want)
    except (RES.CircuitOpen, RES.Overloaded, T.TransitUnavailable) as e:
        # The engine is a separate service; if it is down that is a 503, not a
        # broken request, and the app should fall back rather than show an error.
        _otp_unavailable(e)
    except ValueError as e:
        raise HTTPException(400, "bad depart time: %s" % e)
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    if not out["itineraries"]:
        raise HTTPException(404, "no transit itinerary — the trip may be outside "
                                 "the service area, or outside service hours")
    cache_set(key, out, ttl=TRANSIT_TTL)
    return {**out, "cached": False, "instance": INSTANCE}


@app.get("/transit/vehicles")
def transit_vehicles(pattern: str):
    """Where the vehicles on one pattern are right now.

    The app uses this during a ride to answer a question the stop countdown
    cannot: are you actually on the vehicle you planned to be on? Position
    matching against the planned stop sequence alone cannot tell riding the 523
    apart from driving beside it.

    Cached for only 15 s -- shorter than OTP's own 30 s poll, so the app never
    sees data staler than the engine has.
    """
    METRICS["requests"] += 1
    key = "tv:%s" % pattern
    hit = cache_get(key)
    if hit:
        METRICS["cache_hits"] += 1
        return {**hit, "cached": True, "instance": INSTANCE}
    try:
        with OTP_GUARD:
            out = T.vehicles(pattern)
    except (RES.CircuitOpen, RES.Overloaded, T.TransitUnavailable) as e:
        _otp_unavailable(e)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    cache_set(key, out, ttl=15)
    return {**out, "cached": False, "instance": INSTANCE}


@app.get("/resilience")
def resilience():
    """What the guards around OTP are doing right now.

    Deliberately its own endpoint rather than a field on /health. /health is
    polled by the load balancer several times a minute and must stay trivial;
    this is for a human asking why transit is refusing requests.
    """
    return {"opentripplanner": OTP_GUARD.state(), "instance": INSTANCE}


@app.get("/transit/status")
def transit_status():
    """Is the transit engine reachable, and which feeds did it load?

    Kept off /health on purpose: /health has to stay fast for the load balancer,
    and this one talks to another service over the network.
    """
    return {**T.health(), "instance": INSTANCE}


@app.post("/quote")
def quote(i: QuoteIn):
    """The rider-facing flow: one call → ETA, fare, and cancellation risk together."""
    METRICS["requests"] += 1
    eta = M.predict_eta(i.distance, i.hour, i.weather, i.traffic)
    duration = eta["ensemble_min"]
    fare = M.estimate_fare(i.distance, duration, i.surge)
    wait = 1.5 + 3.0 * (i.surge - 1) + 2.0 * i.traffic
    cancel = M.cancellation_risk(wait, i.surge, i.traffic, i.rider_rating)
    out = {"eta_min": duration, "eta_detail": eta,
           "fare_usd": fare["random_forest"], "fare_detail": fare,
           "cancellation_risk": cancel["consensus"], "cancellation_detail": cancel,
           "instance": INSTANCE}
    with db() as c:
        out["quote_id"] = ML.log_prediction(
            c, distance=i.distance, hour=i.hour, weather=i.weather,
            traffic=i.traffic, surge=i.surge, eta=duration,
            fare=fare["random_forest"], cancel_risk=cancel["consensus"],
            instance=INSTANCE, endpoint="/quote")
    SHADOW.submit(out["quote_id"], i.distance, i.traffic, i.weather, i.hour, duration)
    return out


# ---- analytics (cached; return a plot) ----
def _cached_analytic(key, fn):
    METRICS["requests"] += 1
    hit = cache_get(key)
    if hit:
        METRICS["cache_hits"] += 1
        return {**hit, "cached": True, "instance": INSTANCE}
    res = fn()
    cache_set(key, res)
    return {**res, "cached": False, "instance": INSTANCE}


@app.get("/surge-zones")
def surge_zones():
    return _cached_analytic("an:surge", M.surge_zones)


@app.get("/rider-segments")
def rider_segments():
    return _cached_analytic("an:segments", M.rider_segments)


@app.get("/driver-shift")
def driver_shift():
    return _cached_analytic("an:driver", M.driver_shift)


@app.get("/cancellation-causes")
def cancellation_causes():
    return _cached_analytic("an:causes", M.cancellation_causes)


# ---- the feedback loop (see mlloop.py) ------------------------------------
@app.post("/trip-outcome")
def trip_outcome(i: TripOutcomeIn):
    """Report what a trip actually cost in minutes. The only real training signal.

    Every model in this service was fitted to synthetically generated trips, so
    until this endpoint has been called a few hundred times there is no evidence
    anywhere in the system about how long a journey in Edmonton really takes.

    Validated rather than trusted. A phone that slept mid-trip, or a service that
    was killed and restarted, will happily report a nine-hour bus ride, and a
    handful of those would wreck a regression -- so implausible durations are
    rejected here rather than filtered later.
    """
    METRICS["requests"] += 1
    if not (0.5 <= i.actual_min <= 600):
        raise HTTPException(400, "actual_min %.1f is not a plausible trip duration"
                                 % i.actual_min)
    with db() as c:
        oid = ML.log_outcome(c, i.quote_id, i.actual_min, i.actual_fare,
                             i.completed, i.source)
    if oid is None:
        raise HTTPException(404, "unknown quote_id %d -- nothing to attribute this to"
                                 % i.quote_id)
    return {"recorded": True, "outcome_id": oid, "instance": INSTANCE}


@app.get("/training-data")
def training_data():
    """How much real data exists, and whether it is yet enough to retrain on."""
    with db() as c:
        return {**ML.data_status(c), "instance": INSTANCE}


@app.get("/shadow-report")
def shadow_report():
    """Champion versus challenger on live traffic.

    Disagreement is available as soon as a challenger exists. Accuracy needs trip
    outcomes, and until those arrive this deliberately declines to name a winner
    -- two models differing tells you nothing about which one is right.
    """
    with db() as c:
        rep = ML.shadow_report(c)
    return {**rep, "runner": SHADOW.state(), "instance": INSTANCE}
