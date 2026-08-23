"""
otp.py — stand up OpenTripPlanner for Edmonton (TRANSIT_PLAN.md, Phase 1).

Everything the transit engine needs is free and keyless:

  GTFS   Edmonton Transit Service + 6 regional agencies, published by the City
         of Edmonton   https://gtfs.edmonton.ca/TMGTFSRealTimeWebService/GTFS/gtfs.zip
  OSM    Geofabrik's Alberta extract, clipped to the transit area by clip_osm.py
  OTP    OpenTripPlanner 2.x shaded jar from the project's GitHub releases

Why OTP instead of extending our own A*: driving distance doesn't change with the
clock, but transit does — you must reach the stop before the bus leaves, and the
schedule constrains you until you alight. That is the time-dependent shortest-path
problem, and OTP already implements RAPTOR over GTFS plus the walk/transit
stitching. See TRANSIT_PLAN.md section 1.

Usage:
    python otp.py fetch        # download GTFS + OSM + the OTP jar
    python otp.py clip         # cut Alberta down to the Edmonton transit area
    python otp.py build        # build graph.obj  (slow: minutes, GBs of RAM)
    python otp.py serve        # run the routing server on :8081
    python otp.py plan         # smoke test: a real downtown -> university trip
    python otp.py all          # fetch + clip + build, then tells you to serve

Requires Java 21+ (for OTP 2.9) and `pip install osmium` for the clip step.
"""
import argparse
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CONFIG = os.path.join(HERE, "otp-config")

OTP_VERSION = "2.9.0"
OTP_JAR = os.path.join(DATA, "otp-shaded-%s.jar" % OTP_VERSION)
OTP_URL = ("https://github.com/opentripplanner/OpenTripPlanner/releases/download/"
           "v%s/otp-shaded-%s.jar" % (OTP_VERSION, OTP_VERSION))

# The City of Edmonton's live GTFS endpoint. Feeds move; if this 404s, pull the
# current link from data.edmonton.ca ("ETS Bus Schedule GTFS Data Feed Link for
# Developers") instead of pinning a stale mirror.
GTFS_URL = "https://gtfs.edmonton.ca/TMGTFSRealTimeWebService/GTFS/gtfs.zip"
GTFS_ZIP = os.path.join(DATA, "gtfs.zip")

# Geofabrik's Alberta extract is the smallest published slice containing Edmonton.
OSM_URL = "https://download.geofabrik.de/north-america/canada/alberta-latest.osm.pbf"
OSM_PROVINCE = os.path.join(DATA, "alberta-latest.osm.pbf")
OSM_CITY = os.path.join(DATA, "edmonton.osm.pbf")

GRAPH = os.path.join(DATA, "graph.obj")
PORT = int(os.environ.get("OTP_PORT", "8081"))
# The graph holds every stop, trip and street segment in memory at once, so the
# heap size is the setting that decides whether a build finishes or dies.
HEAP = os.environ.get("OTP_HEAP", "6G")

# OTP 2.9's jar is compiled at class-file version 69, i.e. it refuses to run on
# anything below Java 25 — including Java 24, which is new enough that it is easy
# to assume it will do. Rather than making that a manual prerequisite, we fetch a
# private JDK next to the jar when the system one is too old.
JAVA_MIN = 25
JDK_DIR = os.path.join(DATA, "jdk")
ADOPTIUM = ("https://api.adoptium.net/v3/binary/latest/%d/ga/%s/%s/jdk/hotspot/"
            "normal/eclipse")


def _download(url, dst, label):
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        print("  %s: already have %s (%.1f MB)"
              % (label, os.path.basename(dst), os.path.getsize(dst) / 1e6))
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    print("  %s: downloading %s" % (label, url))
    tmp = dst + ".part"
    # GitHub's release CDN (which Adoptium and OTP both redirect to) 403s the
    # default urllib user agent, so identify ourselves as something ordinary.
    req = urllib.request.Request(url, headers={"User-Agent": "RideIQ-transit-setup"})
    with urllib.request.urlopen(req) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    os.replace(tmp, dst)
    print("  %s: wrote %s (%.1f MB)" % (label, dst, os.path.getsize(dst) / 1e6))


def cmd_fetch(_a):
    """Download the GTFS feed, the OSM extract and the OTP jar."""
    print("Fetching transit inputs")
    _download(GTFS_URL, GTFS_ZIP, "GTFS")
    _download(OSM_URL, OSM_PROVINCE, "OSM ")
    _download(OTP_URL, OTP_JAR, "OTP ")


def cmd_clip(_a):
    """Clip the provincial OSM extract down to the transit service area."""
    if not (os.path.exists(GTFS_ZIP) and os.path.exists(OSM_PROVINCE)):
        raise SystemExit("run `python otp.py fetch` first")
    subprocess.check_call([sys.executable, os.path.join(HERE, "clip_osm.py"),
                           OSM_PROVINCE, OSM_CITY, "--gtfs", GTFS_ZIP])


def _java_major(exe):
    """Major version of a java binary, or 0 if it will not run at all."""
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True).stderr
    except OSError:
        return 0
    m = re.search(r'version "(\d+)', out)
    return int(m.group(1)) if m else 0


def _fetch_jdk():
    """Download a Temurin JDK into data/jdk when the system java is too old."""
    system = {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}.get(
        platform.system())
    arch = {"AMD64": "x64", "x86_64": "x64",
            "arm64": "aarch64", "aarch64": "aarch64"}.get(platform.machine())
    if not (system and arch):
        raise SystemExit("no Temurin build known for %s/%s — install Java %d yourself"
                         % (platform.system(), platform.machine(), JAVA_MIN))
    url = ADOPTIUM % (JAVA_MIN, system, arch)
    suffix = ".zip" if system == "windows" else ".tar.gz"
    archive = os.path.join(DATA, "jdk" + suffix)
    _download(url, archive, "JDK ")
    print("  JDK : unpacking")
    os.makedirs(JDK_DIR, exist_ok=True)
    if suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            z.extractall(JDK_DIR)
    else:
        import tarfile
        with tarfile.open(archive) as t:
            t.extractall(JDK_DIR)
    os.remove(archive)


def _java_bin():
    """Pick a java that can actually run the OTP jar, fetching one if need be."""
    if os.environ.get("JAVA_BIN"):
        return os.environ["JAVA_BIN"]
    private = sorted(glob.glob(os.path.join(JDK_DIR, "*", "bin", "java*")))
    private = [p for p in private if not p.endswith((".dll", "w.exe"))]
    if private:
        return private[0]
    if _java_major("java") >= JAVA_MIN:
        return "java"
    print("System java is too old for OTP %s (needs Java %d+); fetching one"
          % (OTP_VERSION, JAVA_MIN))
    _fetch_jdk()
    return _java_bin()


def _java(*args, **kw):
    heap = kw.get("heap") or HEAP
    return [_java_bin(), "-Xmx" + heap, "-jar", OTP_JAR] + list(args)


def _stage(name, files):
    """OTP reads every input from one directory, so stage copies into one."""
    d = os.path.join(DATA, name)
    os.makedirs(d, exist_ok=True)
    for src, dst_name in files:
        dst = os.path.join(d, dst_name)
        if not os.path.exists(dst) or os.path.getmtime(src) > os.path.getmtime(dst):
            shutil.copyfile(src, dst)
    return d


def cmd_build(_a):
    """Build graph.obj from the GTFS feed and the street network."""
    if not os.path.exists(OTP_JAR):
        raise SystemExit("run `python otp.py fetch` first")
    osm = OSM_CITY if os.path.exists(OSM_CITY) else OSM_PROVINCE
    build = _stage("otp-build", [
        (GTFS_ZIP, "gtfs.zip"),
        (osm, "edmonton.osm.pbf"),
        (os.path.join(CONFIG, "build-config.json"), "build-config.json"),
        (os.path.join(CONFIG, "router-config.json"), "router-config.json"),
    ])
    print("Building graph in %s (heap %s) — this takes several minutes" % (build, HEAP))
    subprocess.check_call(_java("--build", "--save", build))
    shutil.move(os.path.join(build, "graph.obj"), GRAPH)
    print("Wrote %s (%.1f MB)" % (GRAPH, os.path.getsize(GRAPH) / 1e6))


def cmd_serve(a):
    """Load graph.obj and answer routing requests."""
    if not os.path.exists(GRAPH):
        raise SystemExit("no graph.obj — run `python otp.py build` first")
    serve = _stage("otp-serve", [
        (GRAPH, "graph.obj"),
        (os.path.join(CONFIG, "router-config.json"), "router-config.json"),
    ])
    print("Serving OTP on http://localhost:%d  (GraphQL at /otp/gtfs/v1)" % a.port)
    subprocess.check_call(_java("--load", "--port", str(a.port), serve))


# A downtown -> University of Alberta trip: two well-served points that should
# always return an itinerary if the graph built correctly.
SMOKE_FROM = (53.5444, -113.4909)   # Churchill Square, downtown
SMOKE_TO = (53.5232, -113.5263)     # University of Alberta

PLAN_QUERY = """
query Plan($from: InputCoordinates!, $to: InputCoordinates!) {
  plan(from: $from, to: $to, numItineraries: 3,
       transportModes: [{mode: WALK}, {mode: TRANSIT}]) {
    itineraries {
      duration
      startTime
      endTime
      legs {
        mode
        duration
        distance
        from { name }
        to { name }
        route { shortName }
        trip { tripHeadsign }
      }
    }
  }
}
"""


def cmd_plan(a):
    """Smoke test the running server — the Phase 1 acceptance check."""
    body = json.dumps({"query": PLAN_QUERY, "variables": {
        "from": {"lat": SMOKE_FROM[0], "lon": SMOKE_FROM[1]},
        "to": {"lat": SMOKE_TO[0], "lon": SMOKE_TO[1]}}}).encode()
    req = urllib.request.Request("http://localhost:%d/otp/gtfs/v1" % a.port, body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    if data.get("errors"):
        print(json.dumps(data["errors"], indent=2))
        raise SystemExit(1)
    its = data["data"]["plan"]["itineraries"]
    if not its:
        raise SystemExit("OTP returned no itineraries — check the feed's service dates")
    for n, it in enumerate(its, 1):
        print("\nItinerary %d: %d min" % (n, it["duration"] // 60))
        for leg in it["legs"]:
            route = (leg.get("route") or {}).get("shortName") or ""
            head = (leg.get("trip") or {}).get("tripHeadsign") or ""
            label = ("%s %s %s" % (leg["mode"], route, head)).strip()
            print("  %-34s %s -> %s  (%d min)"
                  % (label, leg["from"]["name"], leg["to"]["name"],
                     leg["duration"] // 60))
    print("\nOK — %d itineraries. Phase 1 works." % len(its))


def cmd_all(a):
    """fetch + clip + build in one go."""
    cmd_fetch(a)
    cmd_clip(a)
    cmd_build(a)
    print("\nNow run:  python otp.py serve"
          "      (then, in another shell, python otp.py plan)")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("fetch", cmd_fetch), ("clip", cmd_clip), ("build", cmd_build),
                     ("serve", cmd_serve), ("plan", cmd_plan), ("all", cmd_all)):
        s = sub.add_parser(name, help=(fn.__doc__ or name).strip())
        s.add_argument("--port", type=int, default=PORT)
        s.set_defaults(fn=fn)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
