"""
routing.py — a self-contained routing engine (no Google Maps, no paid API).

A road network is just a GRAPH: intersections = nodes, streets = edges weighted by length.
Finding a route = shortest path, solved here with A* (Dijkstra + a straight-line heuristic).

Two transport modes, each with its own graph:
  * DRIVE : city_graph.json   (drivable roads)      — built with network_type="drive"
  * WALK  : walk_graph.json   (footpaths+sidewalks) — built with network_type="walk"

If a graph file is missing we fall back gracefully (walk -> drive, drive -> demo grid).
Both are free and offline at request time. See build_city_graph.py to fetch a real city.
"""
import os
import json
import heapq
import math

import numpy as np

DRIVE_FILE = os.path.join(os.path.dirname(__file__), "city_graph.json")
WALK_FILE = os.path.join(os.path.dirname(__file__), "walk_graph.json")
CITY_KM = 12.0          # demo grid is ~12 km across
GRID = 14


def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── build / load a graph (returned as a self-contained dict) ────────────────
def _build_grid_graph():
    rng = np.random.RandomState(3)
    pos, adj = {}, {}
    for i in range(GRID):
        for j in range(GRID):
            nid = i * GRID + j
            pos[nid] = (i / (GRID - 1) + rng.normal(0, 0.006),
                        j / (GRID - 1) + rng.normal(0, 0.006))
            adj[nid] = {}

    def link(a, b):
        (x1, y1), (x2, y2) = pos[a], pos[b]
        d = math.hypot(x1 - x2, y1 - y2) * CITY_KM
        adj[a][b] = d; adj[b][a] = d

    for i in range(GRID):
        for j in range(GRID):
            nid = i * GRID + j
            if i + 1 < GRID and rng.rand() > 0.10:
                link(nid, (i + 1) * GRID + j)
            if j + 1 < GRID and rng.rand() > 0.10:
                link(nid, i * GRID + (j + 1))
    return {"name": "Demo Grid", "pos": pos, "latlon": None, "adj": adj, "geom": None, "names": None}


def _load_graph(path):
    data = json.load(open(path, encoding="utf-8"))
    pos, latlon, adj, geom, names = {}, {}, {}, {}, {}
    for n in data["nodes"]:
        pos[n["id"]] = (n["x"], n["y"])          # normalized 0..1 for drawing
        latlon[n["id"]] = (n["lat"], n["lon"])   # real coords for true distances
        adj[n["id"]] = {}
    for e in data["edges"]:
        a, b, length = e[0], e[1], e[2]
        adj[a][b] = length; adj[b][a] = length
        shape = e[3] if len(e) > 3 else [[latlon[a][0], latlon[a][1]],
                                         [latlon[b][0], latlon[b][1]]]
        geom[(a, b)] = shape
        geom[(b, a)] = list(reversed(shape))
        nm = e[4] if len(e) > 4 else None
        names[(a, b)] = nm
        names[(b, a)] = nm
    return {"name": data.get("name", "City"), "pos": pos, "latlon": latlon,
            "adj": adj, "geom": geom, "names": names}


def _valid_graph_file(path):
    """A real, non-trivial road network (>= 50 nodes)."""
    if not os.path.exists(path):
        return False
    try:
        return len(json.load(open(path, encoding="utf-8")).get("nodes", [])) >= 50
    except Exception:
        return False


# Load both graphs. Drive is required (falls back to a demo grid); walk is optional.
_drive = _load_graph(DRIVE_FILE) if _valid_graph_file(DRIVE_FILE) else _build_grid_graph()
GRAPHS = {"drive": _drive}
if _valid_graph_file(WALK_FILE):
    GRAPHS["walk"] = _load_graph(WALK_FILE)

# Backward-compatible module globals (point at the drive graph).
CITY_NAME = _drive["name"]
POS, LATLON, ADJ, GEOM, NAMES = _drive["pos"], _drive["latlon"], _drive["adj"], _drive["geom"], _drive["names"]


def _pick(mode):
    """Return (graph, used_mode). Falls back to drive if the requested graph isn't loaded."""
    if mode in GRAPHS:
        return GRAPHS[mode], mode
    return GRAPHS["drive"], "drive"


# ── A* shortest path (all helpers take the graph explicitly) ────────────────
def _heuristic(g, n, goal):
    ll = g["latlon"]
    if ll is not None:
        (la, lo), (lg, log) = ll[n], ll[goal]
        return _haversine_km(la, lo, lg, log)
    (x1, y1), (x2, y2) = g["pos"][n], g["pos"][goal]
    return math.hypot(x1 - x2, y1 - y2) * CITY_KM


def _nearest_node(g, x, y):
    best, bd = None, 1e18
    for n, (px, py) in g["pos"].items():
        d = (px - x) ** 2 + (py - y) ** 2
        if d < bd:
            bd, best = d, n
    return best


def _nearest_by_latlon(g, lat, lon):
    best, bd = None, 1e18
    for n, (la, lo) in g["latlon"].items():
        d = (la - lat) ** 2 + (lo - lon) ** 2
        if d < bd:
            bd, best = d, n
    return best


def _astar(g, start, goal):
    adj = g["adj"]
    openq = [(_heuristic(g, start, goal), 0.0, start)]
    came, gsc = {}, {start: 0.0}
    while openq:
        _, gc, cur = heapq.heappop(openq)
        if cur == goal:
            path = [cur]
            while cur in came:
                cur = came[cur]; path.append(cur)
            return path[::-1], gc
        for nb, w in adj[cur].items():
            ng = gc + w
            if ng < gsc.get(nb, 1e18):
                gsc[nb] = ng; came[nb] = cur
                heapq.heappush(openq, (ng + _heuristic(g, nb, goal), ng, nb))
    return None, None


def _turns(g, path):
    if len(path) < 3:
        return 0
    pos = g["pos"]
    t = 0
    for a, b, c in zip(path, path[1:], path[2:]):
        (ax, ay), (bx, by), (cx, cy) = pos[a], pos[b], pos[c]
        ang = math.degrees(math.atan2(cy - by, cx - bx) - math.atan2(by - ay, bx - ax))
        ang = (ang + 180) % 360 - 180
        if abs(ang) > 30:
            t += 1
    return t


def _poly_latlon(g, path):
    """Route as real [lat, lon] points tracing the actual streets (each street's OSM shape)."""
    ll, geom = g["latlon"], g["geom"]
    if ll is None:
        return None
    if geom is None:
        return [[round(ll[n][0], 6), round(ll[n][1], 6)] for n in path]
    out = []
    for a, b in zip(path, path[1:]):
        seg = geom.get((a, b)) or [[ll[a][0], ll[a][1]], [ll[b][0], ll[b][1]]]
        if out and seg and out[-1] == seg[0]:
            out.extend(seg[1:])
        else:
            out.extend(seg)
    return out


# ── turn-by-turn steps (for voice navigation) ─────────────────────────────
def _bearing(a, b):
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _classify_turn(delta):
    a = abs(delta)
    if a < 25:
        return "straight", "Continue straight"
    side = "right" if delta > 0 else "left"
    if a < 55:
        return "slight_" + side, "Bear " + side
    if a < 125:
        return side, "Turn " + side
    if a < 160:
        return "sharp_" + side, "Sharp " + side
    return "uturn", "Make a U-turn"


def _steps_from_path(g, path):
    ll, adj, names = g["latlon"], g["adj"], g["names"]
    if not path or len(path) < 2 or ll is None:
        return []
    pts = [ll[n] for n in path]
    steps, acc = [], 0.0
    for i in range(1, len(path)):
        acc += adj[path[i - 1]][path[i]] * 1000.0            # edge length km -> m
        if i < len(path) - 1:
            delta = (_bearing(pts[i], pts[i + 1]) - _bearing(pts[i - 1], pts[i]) + 540) % 360 - 180
            maneuver, instr = _classify_turn(delta)
            if maneuver != "straight":
                onto = names.get((path[i], path[i + 1])) if names else None
                text = instr + (" onto " + onto if onto else "")
                steps.append({"lat": round(pts[i][0], 6), "lon": round(pts[i][1], 6),
                              "maneuver": maneuver, "instruction": text, "street": onto,
                              "dist_from_prev_m": round(acc)})
                acc = 0.0
    steps.append({"lat": round(pts[-1][0], 6), "lon": round(pts[-1][1], 6),
                  "maneuver": "arrive", "instruction": "You have arrived at your destination",
                  "dist_from_prev_m": round(acc)})
    return steps


def _result(g, path, dist, used_mode, requested_mode):
    return {"polyline": [[round(g["pos"][n][0], 4), round(g["pos"][n][1], 4)] for n in path],
            "polyline_latlon": _poly_latlon(g, path),
            "distance_km": round(dist, 2), "turns": _turns(g, path), "nodes": len(path),
            "steps": _steps_from_path(g, path),
            "mode": used_mode,
            "mode_fallback": used_mode != requested_mode}


# ── public API ──────────────────────────────────────────────────────────────
def route(ax, ay, bx, by, mode="drive"):
    g, used = _pick(mode)
    s, gnode = _nearest_node(g, ax, ay), _nearest_node(g, bx, by)
    path, dist = _astar(g, s, gnode)
    if path is None:
        return None
    return _result(g, path, dist, used, mode)


def route_latlon(lat1, lon1, lat2, lon2, mode="drive"):
    """Route between two real-world points. Real city only."""
    g, used = _pick(mode)
    if g["latlon"] is None:
        return None
    s, gg = _nearest_by_latlon(g, lat1, lon1), _nearest_by_latlon(g, lat2, lon2)
    path, dist = _astar(g, s, gg)
    if path is None:
        return None
    return _result(g, path, dist, used, mode)


def has_latlon():
    """True when a real OSM city (drive graph) is loaded."""
    return GRAPHS["drive"]["latlon"] is not None


def has_mode(mode):
    """Whether a real graph is loaded for this transport mode."""
    return mode in GRAPHS and GRAPHS[mode]["latlon"] is not None


def graph_json():
    g = GRAPHS["drive"]
    nodes = [{"id": n, "x": round(x, 4), "y": round(y, 4)} for n, (x, y) in g["pos"].items()]
    seen, edges = set(), []
    for a, nbrs in g["adj"].items():
        for b in nbrs:
            key = (min(a, b), max(a, b))
            if key not in seen:
                seen.add(key); edges.append([a, b])
    return {"city": g["name"], "nodes": nodes, "edges": edges}
