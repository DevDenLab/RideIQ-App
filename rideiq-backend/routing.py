"""
routing.py — a self-contained routing engine (no Google Maps, no paid API).

A road network is just a GRAPH: intersections = nodes, streets = edges weighted by length.
Finding a route = shortest path, solved here with A* (Dijkstra + a straight-line heuristic).

Two modes, chosen automatically:
  * REAL CITY : if `city_graph.json` exists (produced by build_city_graph.py from free
                OpenStreetMap data — e.g. Edmonton), it is loaded and used.
  * DEMO GRID : otherwise a synthetic city street grid is generated in code.

Both are free and offline at request time. See build_city_graph.py to fetch a real city.
"""
import os
import json
import heapq
import math

import numpy as np

CITY_FILE = os.path.join(os.path.dirname(__file__), "city_graph.json")
CITY_KM = 12.0          # demo grid is ~12 km across
GRID = 14


def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── build / load the graph ────────────────────────────────────────────────
def _build_grid():
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
    return "Demo Grid", pos, None, adj


def _load_city():
    data = json.load(open(CITY_FILE, encoding="utf-8"))
    pos, latlon, adj, geom, names = {}, {}, {}, {}, {}
    for n in data["nodes"]:
        pos[n["id"]] = (n["x"], n["y"])          # normalized 0..1 for drawing
        latlon[n["id"]] = (n["lat"], n["lon"])   # real coords for true distances
        adj[n["id"]] = {}
    for e in data["edges"]:
        a, b, length = e[0], e[1], e[2]
        adj[a][b] = length; adj[b][a] = length
        # per-street shape (list of [lat,lon]); older files may omit it
        shape = e[3] if len(e) > 3 else [[latlon[a][0], latlon[a][1]],
                                         [latlon[b][0], latlon[b][1]]]
        geom[(a, b)] = shape
        geom[(b, a)] = list(reversed(shape))
        # street name (5th field); older graphs won't have it -> stays None
        nm = e[4] if len(e) > 4 else None
        names[(a, b)] = nm
        names[(b, a)] = nm
    return data.get("name", "City"), pos, latlon, adj, geom, names


def _have_real_city():
    """Use city_graph.json only if it's a real, non-trivial road network (>= 50 nodes)."""
    if not os.path.exists(CITY_FILE):
        return False
    try:
        return len(json.load(open(CITY_FILE, encoding="utf-8")).get("nodes", [])) >= 50
    except Exception:
        return False


if _have_real_city():
    CITY_NAME, POS, LATLON, ADJ, GEOM, NAMES = _load_city()
else:
    CITY_NAME, POS, LATLON, ADJ = _build_grid()
    GEOM = None
    NAMES = None


# ── A* shortest path ──────────────────────────────────────────────────────
def _heuristic(n, goal):
    """Admissible lower bound on remaining distance (km)."""
    if LATLON is not None:
        (la, lo), (lg, log) = LATLON[n], LATLON[goal]
        return _haversine_km(la, lo, lg, log)
    (x1, y1), (x2, y2) = POS[n], POS[goal]
    return math.hypot(x1 - x2, y1 - y2) * CITY_KM


def _nearest_node(x, y):
    best, bd = None, 1e18
    for n, (px, py) in POS.items():
        d = (px - x) ** 2 + (py - y) ** 2
        if d < bd:
            bd, best = d, n
    return best


def _astar(start, goal):
    openq = [(_heuristic(start, goal), 0.0, start)]
    came, g = {}, {start: 0.0}
    while openq:
        _, gc, cur = heapq.heappop(openq)
        if cur == goal:
            path = [cur]
            while cur in came:
                cur = came[cur]; path.append(cur)
            return path[::-1], gc
        for nb, w in ADJ[cur].items():
            ng = gc + w
            if ng < g.get(nb, 1e18):
                g[nb] = ng; came[nb] = cur
                heapq.heappush(openq, (ng + _heuristic(nb, goal), ng, nb))
    return None, None


def _turns(path):
    if len(path) < 3:
        return 0
    t = 0
    for a, b, c in zip(path, path[1:], path[2:]):
        (ax, ay), (bx, by), (cx, cy) = POS[a], POS[b], POS[c]
        ang = math.degrees(math.atan2(cy - by, cx - bx) - math.atan2(by - ay, bx - ax))
        ang = (ang + 180) % 360 - 180
        if abs(ang) > 30:
            t += 1
    return t


def _poly_latlon(path):
    """Route as real [lat, lon] points that trace the actual streets (using each
    street's OSM shape), not just straight lines between intersections."""
    if LATLON is None:
        return None
    if GEOM is None:                                  # demo grid: node points only
        return [[round(LATLON[n][0], 6), round(LATLON[n][1], 6)] for n in path]
    out = []
    for a, b in zip(path, path[1:]):
        seg = GEOM.get((a, b)) or [[LATLON[a][0], LATLON[a][1]], [LATLON[b][0], LATLON[b][1]]]
        if out and seg and out[-1] == seg[0]:
            out.extend(seg[1:])                       # avoid duplicating the shared point
        else:
            out.extend(seg)
    return out


# ── turn-by-turn steps (for voice navigation) ─────────────────────────────
def _bearing(a, b):
    """Compass bearing in degrees (0=N, 90=E) from point a to point b (lat,lon)."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _classify_turn(delta):
    """delta = signed heading change in degrees (+ = right, - = left)."""
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


def _edge_name(a, b):
    """Street name for the edge a->b, if the loaded graph has names (else None)."""
    return NAMES.get((a, b)) if NAMES else None


def _steps_from_path(path):
    """Turn-by-turn maneuvers at intersections along the path. Each step is the point
    where you act, the instruction, and how far you travel to reach it from the previous one."""
    if not path or len(path) < 2 or LATLON is None:
        return []
    pts = [LATLON[n] for n in path]
    steps, acc = [], 0.0
    for i in range(1, len(path)):
        acc += ADJ[path[i - 1]][path[i]] * 1000.0            # edge length km -> m
        if i < len(path) - 1:
            delta = (_bearing(pts[i], pts[i + 1]) - _bearing(pts[i - 1], pts[i]) + 540) % 360 - 180
            maneuver, instr = _classify_turn(delta)
            if maneuver != "straight":
                onto = _edge_name(path[i], path[i + 1])      # the street you turn ONTO
                text = instr + (" onto " + onto if onto else "")
                steps.append({"lat": round(pts[i][0], 6), "lon": round(pts[i][1], 6),
                              "maneuver": maneuver, "instruction": text, "street": onto,
                              "dist_from_prev_m": round(acc)})
                acc = 0.0
    steps.append({"lat": round(pts[-1][0], 6), "lon": round(pts[-1][1], 6),
                  "maneuver": "arrive", "instruction": "You have arrived at your destination",
                  "dist_from_prev_m": round(acc)})
    return steps


def route(ax, ay, bx, by):
    s, gnode = _nearest_node(ax, ay), _nearest_node(bx, by)
    path, dist = _astar(s, gnode)
    if path is None:
        return None
    poly = [[round(POS[n][0], 4), round(POS[n][1], 4)] for n in path]
    return {"polyline": poly, "polyline_latlon": _poly_latlon(path),
            "distance_km": round(dist, 2), "turns": _turns(path), "nodes": len(path),
            "steps": _steps_from_path(path)}


def has_latlon():
    """True only when a real OSM city is loaded (needed for landmark routing)."""
    return LATLON is not None


def _nearest_by_latlon(lat, lon):
    best, bd = None, 1e18
    for n, (la, lo) in LATLON.items():
        d = (la - lat) ** 2 + (lo - lon) ** 2
        if d < bd:
            bd, best = d, n
    return best


def route_latlon(lat1, lon1, lat2, lon2):
    """Route between two real-world points (e.g. Edmonton landmarks). Real city only."""
    if LATLON is None:
        return None
    s, g = _nearest_by_latlon(lat1, lon1), _nearest_by_latlon(lat2, lon2)
    path, dist = _astar(s, g)
    if path is None:
        return None
    poly = [[round(POS[n][0], 4), round(POS[n][1], 4)] for n in path]
    return {"polyline": poly, "polyline_latlon": _poly_latlon(path),
            "distance_km": round(dist, 2), "turns": _turns(path), "nodes": len(path),
            "steps": _steps_from_path(path)}


def graph_json():
    nodes = [{"id": n, "x": round(x, 4), "y": round(y, 4)} for n, (x, y) in POS.items()]
    seen, edges = set(), []
    for a, nbrs in ADJ.items():
        for b in nbrs:
            key = (min(a, b), max(a, b))
            if key not in seen:
                seen.add(key); edges.append([a, b])
    return {"city": CITY_NAME, "nodes": nodes, "edges": edges}
