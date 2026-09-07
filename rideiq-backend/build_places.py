"""
build_places.py -- compile the local gazetteer that /search autocompletes against.

Reads sources that are NOT in git (the GTFS feed is 22 MB and refreshed every few
weeks; the OSM graphs are large) and emits one small file that IS: places.json.
Same arrangement as build_city_graph.py, and for the same reason -- a clone
should be able to serve without first downloading a feed.

    python build_places.py

Sources, in order of how much a rider cares:

  stops.txt     6,783 transit stops with exact coordinates. The highest-value
                data here by a distance: in a transit app, "which stop" is the
                thing people actually type.
  stop_times    Departures per stop, used as the popularity weight. This is the
                frequency signal the typeahead literature assumes you have and
                most projects fake -- Churchill LRT should outrank a request
                stop on a rural loop, and it does, because it has 400x the
                departures.
  routes.txt    Route short and long names, so "Capital Line" resolves.
  city_graph    Street names from the OSM extract, one entry per name.

A street is a line, not a point, so each street entry is pinned to the graph node
nearest that street's centroid -- guaranteed to be ON the street, though for a
20 km road like 109 Street it is only ever going to be a starting guess. Stops do
not have that problem, which is another reason to rank them first.
"""
import csv
import io
import json
import os
import re
import zipfile
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
GTFS = os.path.join(HERE, "transit", "data", "gtfs.zip")
DRIVE = os.path.join(HERE, "city_graph.json")
WALK = os.path.join(HERE, "walk_graph.json")
OUT = os.path.join(HERE, "places.json")

# Landmarks the app already offers as presets. Listed here so they are also
# reachable by typing, and weighted above everything so they head the list.
LANDMARKS = [
    ("Downtown (Churchill Square)", 53.5445, -113.4909),
    ("University of Alberta", 53.5232, -113.5263),
    ("Edmonton International Airport (YEG)", 53.3097, -113.5797),
    ("West Edmonton Mall", 53.5225, -113.6242),
]


def _rows(zf, name):
    with zf.open(name) as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            yield row


def load_stops():
    """Distinct stop names -> (lat, lon, departures_per_week).

    Stops are deduplicated by name because a transit centre is a dozen numbered
    bays with one name, and offering the rider twelve identical lines called
    "Abbottsfield Transit Centre" is worse than offering one. The kept point is
    the mean of the bays, which lands in the middle of the station.
    """
    if not os.path.exists(GTFS):
        print("  no %s -- skipping stops" % os.path.relpath(GTFS, HERE))
        return []

    with zipfile.ZipFile(GTFS) as zf:
        by_id = {}
        for r in _rows(zf, "stops.txt"):
            try:
                lat, lon = float(r["stop_lat"]), float(r["stop_lon"])
            except (TypeError, ValueError):
                continue
            name = (r.get("stop_name") or "").strip()
            if name:
                by_id[r["stop_id"]] = (name, lat, lon)
        print("  stops.txt        %6d stops" % len(by_id))

        # Departures per stop. This file is the big one -- several million rows --
        # so count as we stream rather than materialising it.
        departures = Counter()
        try:
            for r in _rows(zf, "stop_times.txt"):
                departures[r["stop_id"]] += 1
            print("  stop_times.txt   %6d departures counted" % sum(departures.values()))
        except KeyError:
            print("  stop_times.txt   absent -- every stop weighted equally")

    grouped = defaultdict(list)
    for sid, (name, lat, lon) in by_id.items():
        grouped[name].append((lat, lon, departures.get(sid, 0)))

    out = []
    for name, pts in grouped.items():
        lat = sum(p[0] for p in pts) / len(pts)
        lon = sum(p[1] for p in pts) / len(pts)
        out.append((name, lat, lon, sum(p[2] for p in pts)))
    print("  -> %6d distinct stop names" % len(out))
    return out


def load_routes():
    if not os.path.exists(GTFS):
        return []
    names = {}
    with zipfile.ZipFile(GTFS) as zf:
        try:
            trips = Counter(r["route_id"] for r in _rows(zf, "trips.txt"))
        except KeyError:
            trips = Counter()
        for r in _rows(zf, "routes.txt"):
            long_name = (r.get("route_long_name") or "").strip()
            short = (r.get("route_short_name") or "").strip()
            if not long_name:
                continue
            # ETS names the LRT lines "Valley" / "Valley Line", so blindly
            # gluing short to long yields "Valley Valley Line".
            label = long_name if (not short or
                                  long_name.lower().startswith(short.lower()))                 else ("%s %s" % (short, long_name)).strip()
            names[label] = trips.get(r["route_id"], 1)
    print("  routes.txt       %6d routes" % len(names))
    return names


def load_streets():
    """Street name -> a coordinate on that street.

    Names come from the OSM edge attributes already stored in the routing graphs.
    Both graphs are read because the walk network carries footpaths and named
    trails the drive network does not.
    """
    streets = defaultdict(list)
    for path, label in ((DRIVE, "city_graph"), (WALK, "walk_graph")):
        if not os.path.exists(path):
            print("  %-16s absent -- skipping" % os.path.basename(path))
            continue
        data = json.load(open(path, encoding="utf-8"))
        coords = {n["id"]: (n["lat"], n["lon"]) for n in data["nodes"]}
        before = len(streets)
        for e in data["edges"]:
            name = e[4] if len(e) > 4 else None
            if not name or not isinstance(name, str):
                continue
            a = coords.get(e[0])
            b = coords.get(e[1])
            if a and b:
                streets[name.strip()].append(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
        print("  %-16s %6d street names (+%d new)"
              % (label, len(streets), len(streets) - before))

    out = []
    for name, pts in streets.items():
        clat = sum(p[0] for p in pts) / len(pts)
        clon = sum(p[1] for p in pts) / len(pts)
        # The centroid of a bent street can fall off it, so pin to the segment
        # midpoint nearest the centroid instead. Always on the road.
        best = min(pts, key=lambda p: (p[0] - clat) ** 2 + (p[1] - clon) ** 2)
        # Longer streets are more likely to be what someone means.
        out.append((name, best[0], best[1], len(pts)))
    return out


def main():
    print("Sources:")
    stops = load_stops()
    routes = load_routes()
    streets = load_streets()

    entries = []
    seen = set()

    def add(name, lat, lon, kind, weight):
        # OSM contributes a handful of junk name tags -- one Edmonton way is
        # literally named "\\". Anything with no letter or digit in it is not a
        # place name and cannot be typed for anyway.
        if not name or not re.search(r"[A-Za-z0-9]", name):
            return
        key = (kind, name.lower())
        if key in seen:
            return
        seen.add(key)
        entries.append({"n": name, "y": round(lat, 6), "x": round(lon, 6),
                        "k": kind, "w": int(weight)})

    # Weights are deliberately on very different scales rather than tuned against
    # each other: the ordering between kinds is a product decision, not something
    # to be discovered from departure counts.
    for name, lat, lon in LANDMARKS:
        add(name, lat, lon, "landmark", 10_000_000)
    for name, lat, lon, dep in stops:
        add(name, lat, lon, "stop", 1_000_000 + dep)
    for name, lat, lon, seg in streets:
        add(name, lat, lon, "street", 1_000 + seg)
    # Routes carry no single coordinate; they exist so typing "Capital Line"
    # resolves to its busiest station rather than to nothing.
    stop_by_name = {s[0]: s for s in stops}
    for label, trips in sorted(routes.items(), key=lambda kv: -kv[1])[:400]:
        anchor = None
        for word in ("Transit Centre", "Station"):
            anchor = next((s for n, s in stop_by_name.items() if word in n
                           and any(t.lower() in n.lower()
                                   for t in label.split() if len(t) > 4)), None)
            if anchor:
                break
        if anchor:
            add(label, anchor[1], anchor[2], "route", 500_000 + trips)

    entries.sort(key=lambda e: -e["w"])
    payload = {"city": "Edmonton", "count": len(entries), "entries": entries}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    kinds = Counter(e["k"] for e in entries)
    print("\n%d entries -> %s (%.1f MB)"
          % (len(entries), os.path.basename(OUT), os.path.getsize(OUT) / 1e6))
    for k, n in kinds.most_common():
        print("  %-9s %6d" % (k, n))
    print("\nTop by weight:")
    for e in entries[:8]:
        print("  %-9s %s" % (e["k"], e["n"]))


if __name__ == "__main__":
    main()
