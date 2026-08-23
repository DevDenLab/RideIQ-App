"""
clip_osm.py — cut a city-sized OSM extract out of a provincial one.

Geofabrik's smallest Canadian slice that contains Edmonton is all of Alberta
(~350 MB). Handing that whole thing to OpenTripPlanner works, but it inflates
graph-build time and RAM for streets nobody in this app will ever walk on. So we
clip it to a bounding box around the transit network first (~40 MB), which keeps
the OTP graph inside the memory budget in TRANSIT_PLAN.md section 6.

The bounding box defaults to the extent of the GTFS stops plus a margin, so it
tracks the feed instead of being a hard-coded guess: if ETS adds a park-and-ride
in Devon next year, the box grows with it.

Usage:
    python clip_osm.py data/alberta-latest.osm.pbf data/edmonton.osm.pbf \
        --gtfs data/gtfs.zip
    python clip_osm.py in.pbf out.pbf --bbox 53.20,-114.00,53.77,-113.15
"""
import argparse
import csv
import io
import os
import sys
import zipfile

MARGIN_DEG = 0.05          # ~5.5 km of walk-access room around the outermost stop


def bbox_from_gtfs(gtfs_zip, margin=MARGIN_DEG):
    """Extent of every stop in the feed, padded so walk legs can reach them."""
    with zipfile.ZipFile(gtfs_zip) as z:
        rows = csv.DictReader(io.StringIO(z.read("stops.txt").decode("utf-8-sig")))
        lats, lons = [], []
        for r in rows:
            if r.get("stop_lat") and r.get("stop_lon"):
                lats.append(float(r["stop_lat"]))
                lons.append(float(r["stop_lon"]))
    if not lats:
        raise SystemExit("stops.txt had no coordinates")
    return (min(lats) - margin, min(lons) - margin,
            max(lats) + margin, max(lons) + margin)


def clip(src, dst, bbox):
    """Write every way touching the box, plus the nodes those ways reference."""
    import osmium

    min_lat, min_lon, max_lat, max_lon = bbox

    def inside(loc):
        return (loc.valid()
                and min_lat <= loc.lat <= max_lat
                and min_lon <= loc.lon <= max_lon)

    if os.path.exists(dst):
        os.remove(dst)

    kept = 0
    # BackReferenceWriter does the hard part: we hand it the ways we want and it
    # re-reads the source to pull in every node they reference, so the output is
    # a self-contained OSM file rather than a pile of dangling way references.
    # remove_tags=False keeps tags on the pulled-in nodes: OTP reads barriers,
    # crossings, elevators and station entrances off individual nodes, and a
    # tagless node graph quietly degrades the walk legs.
    with osmium.BackReferenceWriter(dst, ref_src=src, overwrite=True,
                                    remove_tags=False) as writer:
        for obj in osmium.FileProcessor(src).with_locations().with_filter(
                osmium.filter.EntityFilter(osmium.osm.WAY)):
            if any(inside(n.location) for n in obj.nodes):
                writer.add(obj)
                kept += 1
                if kept % 100_000 == 0:
                    print(f"  ... {kept:,} ways kept", flush=True)
    return kept


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("src", help="provincial .osm.pbf to read")
    p.add_argument("dst", help="clipped .osm.pbf to write")
    p.add_argument("--gtfs", help="GTFS zip to derive the bounding box from")
    p.add_argument("--bbox", help="explicit min_lat,min_lon,max_lat,max_lon")
    a = p.parse_args()

    if a.bbox:
        bbox = tuple(float(x) for x in a.bbox.split(","))
    elif a.gtfs:
        bbox = bbox_from_gtfs(a.gtfs)
    else:
        p.error("pass --gtfs or --bbox")

    print(f"Clipping {a.src}")
    print(f"  bbox lat {bbox[0]:.4f}..{bbox[2]:.4f}  lon {bbox[1]:.4f}..{bbox[3]:.4f}")
    kept = clip(a.src, a.dst, bbox)
    size_mb = os.path.getsize(a.dst) / 1e6
    print(f"Wrote {a.dst}: {kept:,} ways, {size_mb:.1f} MB")


if __name__ == "__main__":
    sys.exit(main())
