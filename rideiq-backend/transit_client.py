"""
transit_client.py — talk to OpenTripPlanner and hand back RideIQ-shaped legs.

TRANSIT_PLAN.md Phase 2. OTP owns the hard part (RAPTOR over the ETS timetable);
this module owns the translation, so the Android app never learns what OTP is:

    app  ->  FastAPI /transit  ->  this module  ->  OTP GraphQL /otp/gtfs/v1

OTP's itinerary is verbose and epoch-millis-flavoured. The app already knows how
to draw a `polyline_latlon` and list steps, so we flatten each OTP leg into the
same vocabulary the driving routes use, and add only what transit genuinely needs:
which route you board, its headsign, and the clock times.

Set OTP_URL to point at the engine (default http://localhost:8081, which is what
`transit/otp.py serve` gives you).
"""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta

OTP_URL = os.environ.get("OTP_URL", "http://localhost:8081").rstrip("/")
OTP_TIMEOUT = float(os.environ.get("OTP_TIMEOUT", "20"))
# Every agency in the ETS feed publishes America/Edmonton, and OTP interprets the
# date/time we send in the feed's own zone — so we must build it in that zone too,
# not in whatever the server host happens to be set to.
TZ_NAME = os.environ.get("TRANSIT_TZ", "America/Edmonton")

# Modes OTP reports that mean "you are riding something", vs. walking to it.
RIDE_MODES = {"BUS", "TRAM", "SUBWAY", "RAIL", "FERRY", "CABLE_CAR",
              "GONDOLA", "FUNICULAR", "TROLLEYBUS", "MONORAIL", "COACH"}

# What a rider calls the thing. ETS tags the Capital/Metro/Valley lines as TRAM
# in GTFS, but nobody in Edmonton boards a "tram" — they board the LRT.
MODE_LABEL = {"BUS": "Bus", "TRAM": "LRT", "SUBWAY": "LRT", "RAIL": "Train",
              "FERRY": "Ferry", "COACH": "Coach", "TROLLEYBUS": "Trolleybus",
              "WALK": "Walk"}


class TransitUnavailable(RuntimeError):
    """OTP is not reachable — the caller should degrade, not 500."""


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TZ_NAME)
    except Exception:
        # No tzdata on the host: fall back to the server clock. Times stay
        # self-consistent, they just may not be Edmonton's.
        return None


# ── encoded polyline ────────────────────────────────────────────────────────
def decode_polyline(encoded, precision=5):
    """Google/OTP encoded polyline -> [[lat, lon], ...]."""
    coords, index, lat, lon = [], 0, 0, 0
    factor = float(10 ** precision)
    n = len(encoded)
    while index < n:
        for axis in range(2):
            shift, result = 0, 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else (result >> 1)
            if axis == 0:
                lat += delta
            else:
                lon += delta
        coords.append([round(lat / factor, 6), round(lon / factor, 6)])
    return coords


# ── GraphQL ────────────────────────────────────────────────────────────────
PLAN_QUERY = """
query Plan($from: InputCoordinates!, $to: InputCoordinates!, $date: String!,
           $time: String!, $arriveBy: Boolean!, $n: Int!, $wheelchair: Boolean!,
           $walkReluctance: Float!) {
  plan(from: $from, to: $to, date: $date, time: $time, arriveBy: $arriveBy,
       numItineraries: $n, wheelchair: $wheelchair, walkReluctance: $walkReluctance,
       transportModes: [{mode: WALK}, {mode: TRANSIT}]) {
    itineraries {
      duration
      startTime
      endTime
      walkDistance
      legs {
        mode
        duration
        distance
        startTime
        endTime
        realTime
        realtimeState
        headsign
        from {
          name lat lon stop { code platformCode }
          departure { estimated { delay } }
        }
        to {
          name lat lon stop { code platformCode }
          arrival { estimated { delay } }
        }
        route { shortName longName mode color textColor }
        trip { tripHeadsign }
        legGeometry { points }
        alerts { alertHeaderText alertDescriptionText alertSeverityLevel alertEffect }
        fareProducts {
          id
          product {
            name
            medium { name }
            riderCategory { name }
            ... on DefaultFareProduct { price { amount currency { code } } }
          }
        }
      }
    }
  }
}
"""


def _post(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(OTP_URL + "/otp/gtfs/v1", body,
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=OTP_TIMEOUT) as r:
            data = json.loads(r.read())
    except urllib.error.URLError as e:
        raise TransitUnavailable("OTP at %s is not answering (%s)" % (OTP_URL, e))
    if data.get("errors"):
        raise RuntimeError(data["errors"][0].get("message", "OTP rejected the query"))
    return data["data"]


def health():
    """Cheap reachability probe for /health, so ops can see OTP separately."""
    try:
        req = urllib.request.Request(OTP_URL + "/otp/gtfs/v1",
                                     json.dumps({"query": "{feeds{feedId}}"}).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            feeds = json.loads(r.read()).get("data", {}).get("feeds") or []
        return {"ok": True, "url": OTP_URL, "feeds": [f["feedId"] for f in feeds]}
    except Exception as e:
        return {"ok": False, "url": OTP_URL, "error": str(e)}


# ── realtime ───────────────────────────────────────────────────────────────
def _duration_seconds(value):
    """OTP's Duration scalar is ISO-8601 ('PT3M30S', '-PT45S'), not a number."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().upper()
    sign = -1 if text.startswith("-") else 1
    text = text.lstrip("+-")
    if not text.startswith("PT"):
        return None
    total, number = 0, ""
    for ch in text[2:]:
        if ch.isdigit() or ch == ".":
            number += ch
        else:
            unit = {"H": 3600, "M": 60, "S": 1}.get(ch)
            if unit and number:
                total += float(number) * unit
            number = ""
    return int(sign * total)


def _delay(place, key):
    est = ((place or {}).get(key) or {}).get("estimated") or {}
    return _duration_seconds(est.get("delay"))


def _delay_text(seconds):
    """Rider-facing punctuality. Under a minute either way is just 'on time'."""
    if seconds is None:
        return None
    minutes = int(round(seconds / 60.0))
    if minutes == 0:
        return "on time"
    if minutes > 0:
        return "%d min late" % minutes
    return "%d min early" % abs(minutes)


def _alerts(raw_alerts):
    out = []
    for a in raw_alerts or []:
        header = (a.get("alertHeaderText") or "").strip()
        body = (a.get("alertDescriptionText") or "").strip()
        if not (header or body):
            continue
        out.append({"header": header or body,
                    "description": body,
                    "severity": a.get("alertSeverityLevel"),
                    "effect": a.get("alertEffect")})
    return out


# ── fares (GTFS Fares V2) ──────────────────────────────────────────────────
def _fare(legs_raw):
    """What the trip actually costs, per payment method.

    The subtlety is transfers. ETS publishes fare_transfer_rules, so riding three
    buses inside the transfer window is one fare, not three. OTP expresses that by
    giving every leg the same *fare product use id* when one purchase covers them
    all -- so the total is a sum over distinct use ids, never over legs. Summing
    per leg would overcharge exactly the trips transfers exist to protect.
    """
    by_medium, seen = {}, {}
    for raw in legs_raw:
        for use in raw.get("fareProducts") or []:
            product = use.get("product") or {}
            price = product.get("price") or {}
            amount = price.get("amount")
            if amount is None:
                continue
            medium = ((product.get("medium") or {}).get("name")
                      or product.get("name") or "Fare")
            key = (medium, use.get("id"))
            if key in seen:
                continue                      # already paid for by an earlier leg
            seen[key] = True
            entry = by_medium.setdefault(medium, {
                "medium": medium,
                "name": product.get("name") or medium,
                "amount": 0.0,
                "currency": (price.get("currency") or {}).get("code") or "CAD",
                "rider_category": (product.get("riderCategory") or {}).get("name"),
            })
            entry["amount"] += float(amount)

    options = sorted(by_medium.values(), key=lambda o: o["amount"])
    for o in options:
        o["amount"] = round(o["amount"], 2)
    if not options:
        return None
    cheapest = options[0]
    return {
        "amount": cheapest["amount"],
        "currency": cheapest["currency"],
        "medium": cheapest["medium"],
        "text": "%s %.2f (%s)" % (cheapest["currency"], cheapest["amount"],
                                  cheapest["medium"]),
        "options": options,
    }


# ── normalisation ──────────────────────────────────────────────────────────
def _clock(ms, tz):
    dt = datetime.fromtimestamp(ms / 1000.0, tz)
    return dt.strftime("%H:%M"), dt.isoformat(timespec="seconds")


def _leg(raw, tz):
    route = raw.get("route") or {}
    trip = raw.get("trip") or {}
    geom = (raw.get("legGeometry") or {}).get("points") or ""
    depart_hm, depart_iso = _clock(raw["startTime"], tz)
    arrive_hm, arrive_iso = _clock(raw["endTime"], tz)
    leg = {
        "mode": raw["mode"],
        "from": (raw.get("from") or {}).get("name") or "Start",
        "to": (raw.get("to") or {}).get("name") or "Destination",
        "distance_m": round(raw.get("distance") or 0),
        "duration_min": round((raw.get("duration") or 0) / 60),
        "depart": depart_hm,
        "arrive": arrive_hm,
        "depart_iso": depart_iso,
        "arrive_iso": arrive_iso,
        "polyline_latlon": decode_polyline(geom) if geom else [],
    }
    if raw["mode"] in RIDE_MODES:
        leg["mode_label"] = MODE_LABEL.get(raw["mode"], raw["mode"].title())
        leg["route"] = route.get("shortName") or route.get("longName") or ""
        leg["route_name"] = route.get("longName") or ""
        leg["headsign"] = trip.get("tripHeadsign") or raw.get("headsign") or ""
        # ETS mostly leaves route_color empty for buses and sets it for LRT; the
        # app falls back to its own palette when this is None.
        leg["color"] = ("#" + route["color"]) if route.get("color") else None
        # startTime/endTime above are already realtime-adjusted when OTP has a
        # trip update, so delay is the extra bit a rider wants: not just "5:48"
        # but "5:48, running 3 min late".
        leg["realtime"] = bool(raw.get("realTime"))
        leg["realtime_state"] = raw.get("realtimeState")
        delay = _delay(raw.get("from"), "departure")
        if delay is None:
            delay = _delay(raw.get("to"), "arrival")
        leg["delay_s"] = delay
        leg["status"] = _delay_text(delay) if leg["realtime"] else None
        leg["alerts"] = _alerts(raw.get("alerts"))
        for end in ("from", "to"):
            stop = (raw.get(end) or {}).get("stop") or {}
            code = stop.get("code") or stop.get("platformCode")
            if code:
                leg[end + "_stop_code"] = code
    return leg


def _instructions(legs):
    """The leg-by-leg text the app's existing directions list renders."""
    out = []
    for leg in legs:
        if leg["mode"] == "WALK":
            out.append("Walk %d min (%d m) to %s"
                       % (leg["duration_min"], leg["distance_m"], leg["to"]))
        else:
            head = (" toward " + leg["headsign"]) if leg.get("headsign") else ""
            # Only say "3 min late" when a live feed actually told us so; inventing
            # punctuality from the timetable would be worse than saying nothing.
            when = leg["depart"] + ((" (%s)" % leg["status"]) if leg.get("status") else "")
            out.append("Board %s %s%s at %s, %s — ride %d min, get off at %s"
                       % (leg.get("mode_label", leg["mode"]), leg.get("route", ""), head,
                          leg["from"], when, leg["duration_min"], leg["to"]))
    if out:
        out.append("You have arrived at your destination")
    return out


def _itinerary(raw, tz):
    legs = [_leg(l, tz) for l in raw["legs"]]
    rides = [l for l in legs if l["mode"] in RIDE_MODES]
    depart_hm, depart_iso = _clock(raw["startTime"], tz)
    arrive_hm, arrive_iso = _clock(raw["endTime"], tz)
    # One de-duplicated alert list for the whole trip: the same "elevator out at
    # Churchill" rides along on every leg that touches the station.
    alerts, seen = [], set()
    for leg in legs:
        for a in leg.get("alerts") or []:
            if a["header"] not in seen:
                seen.add(a["header"])
                alerts.append(a)
    return {
        "duration_min": round(raw["duration"] / 60),
        "depart": depart_iso,
        "arrive": arrive_iso,
        "depart_time": depart_hm,
        "arrive_time": arrive_hm,
        # Transfers are boardings minus one; a walk-only plan has no transfers.
        "transfers": max(0, len(rides) - 1),
        "walk_distance_m": round(raw.get("walkDistance") or 0),
        "routes": [l.get("route") for l in rides if l.get("route")],
        # True only when a live feed backed at least one ride on this trip.
        "realtime": any(l.get("realtime") for l in rides),
        "status": next((l["status"] for l in rides if l.get("status")), None),
        "alerts": alerts,
        "fare": _fare(raw["legs"]),
        "legs": legs,
        "instructions": _instructions(legs),
    }


def _walk_metres(itin):
    return max([l["distance_m"] for l in itin["legs"] if l["mode"] == "WALK"] or [0])


def plan(lat1, lon1, lat2, lon2, depart="now", arrive_by=False,
         max_walk_m=800, wheelchair=False, want=3):
    """Plan a transit trip and return itineraries in RideIQ's leg format."""
    tz = _tz()
    when = datetime.now(tz) if depart in (None, "", "now") else _parse_when(depart, tz)

    # OTP 2 dropped the hard maxWalkDistance knob; walkReluctance is the supported
    # lever. Nudge it up when the caller wants a short walk so OTP prefers plans
    # that ride further, then filter what still comes back too long.
    reluctance = 2.0 if max_walk_m >= 800 else 4.0

    data = _post(PLAN_QUERY, {
        "from": {"lat": lat1, "lon": lon1},
        "to": {"lat": lat2, "lon": lon2},
        "date": when.strftime("%Y-%m-%d"),
        "time": when.strftime("%H:%M:%S"),
        "arriveBy": bool(arrive_by),
        "n": max(1, int(want)),
        "wheelchair": bool(wheelchair),
        "walkReluctance": reluctance,
    })
    raw = (data.get("plan") or {}).get("itineraries") or []
    itineraries = [_itinerary(i, tz) for i in raw]

    # Keep the walk cap honest, but never answer "no route" purely because every
    # option walks 50 m too far — return the least-walking one instead.
    within = [i for i in itineraries if _walk_metres(i) <= max_walk_m]
    if itineraries and not within:
        within = [min(itineraries, key=_walk_metres)]
    return {
        "itineraries": within,
        "query": {"depart": when.isoformat(timespec="seconds"),
                  "arrive_by": bool(arrive_by), "max_walk_m": max_walk_m,
                  "wheelchair": bool(wheelchair)},
    }


def _parse_when(value, tz):
    """Accept an ISO timestamp, or a relative '+15m' for 'in a quarter hour'."""
    value = value.strip()
    if value.startswith("+") and value[-1] in "mh":
        n = float(value[1:-1])
        return datetime.now(tz) + timedelta(hours=n if value[-1] == "h" else 0,
                                            minutes=0 if value[-1] == "h" else n)
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)
