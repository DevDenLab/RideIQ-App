"""
build_city_graph.py — turn a REAL city's roads into RideIQ's routing graph.

Uses OpenStreetMap data via the free `osmnx` library (no API key, no Google Maps).
Run this ONCE on a machine with internet; it writes `city_graph.json`, which routing.py
then loads automatically. It also saves a preview image `city_preview.png`.

Install (one time):
    pip install osmnx matplotlib

Run:
    python build_city_graph.py                 # default: downtown Edmonton, 4 km radius
    python build_city_graph.py "Calgary, Canada" 5000
    python build_city_graph.py "Edmonton, Canada"     # whole city (large, slower)

Notes:
  * A smaller radius = fewer intersections = a lighter, snappier map in the app.
  * The whole-city option can be tens of thousands of nodes; fine for routing, heavy to draw.
"""
import sys
import json

import osmnx as ox
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Downtown Edmonton by default (lat, lon). Change or pass a place name as arg 1.
DEFAULT_CENTER = (53.5444, -113.4909)
OUT_JSON = "city_graph.json"
OUT_PNG = "city_preview.png"


def download_graph(arg, radius_m):
    if arg and not arg.replace(".", "").replace("-", "").isdigit():
        print(f"Downloading drivable roads for: {arg}")
        return ox.graph_from_place(arg, network_type="drive"), arg.split(",")[0]
    print(f"Downloading drivable roads around downtown Edmonton ({radius_m} m radius)")
    G = ox.graph_from_point(DEFAULT_CENTER, dist=radius_m, network_type="drive")
    return G, "Edmonton (downtown)"


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    radius = int(sys.argv[2]) if len(sys.argv) > 2 else 4000
    G, name = download_graph(arg, radius)

    nodes = list(G.nodes)
    idx = {osmid: i for i, osmid in enumerate(nodes)}
    lats = [G.nodes[n]["y"] for n in nodes]
    lons = [G.nodes[n]["x"] for n in nodes]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)

    def norm(lat, lon):
        x = (lon - min_lon) / (max_lon - min_lon)
        y = (lat - min_lat) / (max_lat - min_lat)
        return round(x, 5), round(y, 5)

    out_nodes = []
    for n in nodes:
        la, lo = G.nodes[n]["y"], G.nodes[n]["x"]
        x, y = norm(la, lo)
        out_nodes.append({"id": idx[n], "x": x, "y": y, "lat": la, "lon": lo})

    seen, out_edges = set(), []
    for u, v, d in G.edges(data=True):
        a, b = idx[u], idx[v]
        key = (min(a, b), max(a, b))
        if key in seen:
            continue
        seen.add(key)
        length_km = d.get("length", 0.0) / 1000.0     # OSM length is metres
        # capture the street's real shape (curve), not just its endpoints
        geom = d.get("geometry", None)
        if geom is not None:
            shape = [[round(y, 6), round(x, 6)] for x, y in geom.coords]  # (lon,lat)->[lat,lon]
        else:
            shape = [[round(G.nodes[u]["y"], 6), round(G.nodes[u]["x"], 6)],
                     [round(G.nodes[v]["y"], 6), round(G.nodes[v]["x"], 6)]]
        # street name (OSM 'name'); can be a list if the way carries several names
        name = d.get("name")
        if isinstance(name, list):
            name = name[0] if name else None
        out_edges.append([a, b, round(length_km, 4), shape, name])

    json.dump({"name": name, "nodes": out_nodes, "edges": out_edges},
              open(OUT_JSON, "w", encoding="utf-8"))
    print(f"Wrote {OUT_JSON}: {len(out_nodes)} intersections, {len(out_edges)} streets")

    # preview
    fig, ax = plt.subplots(figsize=(7, 7))
    pos = {n["id"]: (n["x"], n["y"]) for n in out_nodes}
    for e in out_edges:
        a, b = e[0], e[1]
        (x1, y1), (x2, y2) = pos[a], pos[b]
        ax.plot([x1, x2], [y1, y2], color="#5B6470", lw=0.6)
    ax.set_title(f"{name} — OpenStreetMap road graph")
    ax.set_xticks([]); ax.set_yticks([])
    fig.savefig(OUT_PNG, dpi=120, bbox_inches="tight")
    print(f"Wrote {OUT_PNG}")
    print("\nDone. Restart the backend and the map now shows this real city.")


if __name__ == "__main__":
    main()
