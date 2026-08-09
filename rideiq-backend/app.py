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

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "rideiq.db"))
REDIS_URL = os.environ.get("REDIS_URL")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "3600"))
INSTANCE = os.environ.get("INSTANCE_ID", "api-1")

app = FastAPI(title="RideIQ API", version="1.0",
              description="Ride-hailing platform brain — 15 ML algorithms behind real features.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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


def cache_set(k, v):
    if _redis:
        _redis.setex(k, CACHE_TTL, json.dumps(v))
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


class RouteLatLonIn(BaseModel):
    lat1: float; lon1: float; lat2: float; lon2: float
    hour: int = 8; weather: int = 0; traffic: float = 0.5; surge: float = 1.0


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
    rt = R.route(i.ax, i.ay, i.bx, i.by)
    if rt is None:
        raise HTTPException(400, "no route found")
    eta = M.predict_eta(rt["distance_km"], i.hour, i.weather, i.traffic)
    fare = M.estimate_fare(rt["distance_km"], eta["ensemble_min"], i.surge)
    return {**rt, "eta_min": eta["ensemble_min"], "fare_usd": fare["random_forest"],
            "instance": INSTANCE}


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


@app.post("/route-latlon")
def route_latlon(i: RouteLatLonIn):
    """Route between two real-world points (landmarks). Requires a real OSM city loaded."""
    METRICS["requests"] += 1
    if not R.has_latlon():
        raise HTTPException(400, "Landmark routing needs the real city map. "
                                 "Run build_city_graph.py to create city_graph.json, then restart.")
    rt = R.route_latlon(i.lat1, i.lon1, i.lat2, i.lon2)
    if rt is None:
        raise HTTPException(400, "no route found")
    eta = M.predict_eta(rt["distance_km"], i.hour, i.weather, i.traffic)
    fare = M.estimate_fare(rt["distance_km"], eta["ensemble_min"], i.surge)
    return {**rt, "eta_min": eta["ensemble_min"], "fare_usd": fare["random_forest"],
            "instance": INSTANCE}


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
