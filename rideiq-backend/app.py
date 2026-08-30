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

import models as M
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
    _c.execute("""CREATE TABLE IF NOT EXISTS quotes(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
        distance REAL, hour INT, eta REAL, fare REAL, cancel_risk REAL, instance TEXT)""")

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


class RouteLatLonIn(BaseModel):
    lat1: float; lon1: float; lat2: float; lon2: float
    hour: int = 8; weather: int = 0; traffic: float = 0.5; surge: float = 1.0
    mode: str = "drive"          # "drive" or "walk"


WALK_SPEED_KMH = 4.8             # average walking pace, for walk-mode ETA


def _price_route(rt, i):
    """Attach ETA + fare to a route result, mode-aware. Walking has an ETA but no fare."""
    if rt.get("mode") == "walk":
        eta_min = round(rt["distance_km"] / WALK_SPEED_KMH * 60.0, 1)
        return {**rt, "eta_min": eta_min, "fare_usd": 0.0, "instance": INSTANCE}
    eta = M.predict_eta(rt["distance_km"], i.hour, i.weather, i.traffic)
    fare = M.estimate_fare(rt["distance_km"], eta["ensemble_min"], i.surge)
    return {**rt, "eta_min": eta["ensemble_min"], "fare_usd": fare["random_forest"],
            "instance": INSTANCE}


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
    return {"status": "ok", "instance": INSTANCE, "cache": "redis" if _redis else "local"}


@app.get("/metrics")
def metrics():
    hr = METRICS["cache_hits"] / METRICS["requests"] if METRICS["requests"] else 0
    return {**METRICS, "cache_hit_rate": round(hr, 3), "instance": INSTANCE}


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
    primary = _price_route(routes[0], i)
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


@app.get("/search")
def search(q: str):
    """Autocomplete: address/place text -> up to 5 Edmonton matches. Cached."""
    q = q.strip()
    if len(q) < 3:
        return {"results": [], "instance": INSTANCE}
    key = f"se:{q.lower()}"
    hit = cache_get(key)
    if hit:
        return {**hit, "cached": True, "instance": INSTANCE}
    query = q if "edmonton" in q.lower() else f"{q}, Edmonton, Alberta, Canada"
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "jsonv2", "limit": 5, "addressdetails": 1, "countrycodes": "ca"})
    req = urllib.request.Request(url, headers={"User-Agent": "RideIQ/1.0 (student project)"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {"results": [], "instance": INSTANCE}
    results = [{"lat": float(t["lat"]), "lon": float(t["lon"]),
               "display_name": t.get("display_name", ""),
               "short": _short_address(t.get("address", {}), t.get("display_name", ""))}
              for t in data]
    out = {"results": results}
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
    primary = _price_route(routes[0], i)
    primary["alternatives"] = [_price_route(r, i) for r in routes[1:]]
    return primary


# ---- public transit (OpenTripPlanner behind us; see transit/README.md) ----
# A "leave now" plan goes stale as the next bus gets closer, so transit answers
# get a much shorter TTL than the hour we give analytics.
TRANSIT_TTL = int(os.environ.get("TRANSIT_CACHE_TTL", "120"))
TRANSIT_BUCKET_S = 300           # round the query clock to 5 min so repeats hit


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
        out = T.plan(lat1, lon1, lat2, lon2, depart=depart, arrive_by=arrive_by,
                     max_walk_m=max_walk_m, wheelchair=wheelchair, want=want)
    except T.TransitUnavailable as e:
        # The engine is a separate service; if it is down that is a 503, not a
        # broken request, and the app should fall back rather than show an error.
        raise HTTPException(503, str(e))
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
        out = T.vehicles(pattern)
    except T.TransitUnavailable as e:
        raise HTTPException(503, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    cache_set(key, out, ttl=15)
    return {**out, "cached": False, "instance": INSTANCE}


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
        c.execute("INSERT INTO quotes(ts,distance,hour,eta,fare,cancel_risk,instance) VALUES(?,?,?,?,?,?,?)",
                  (time.time(), i.distance, i.hour, duration, fare["random_forest"],
                   cancel["consensus"], INSTANCE))
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
