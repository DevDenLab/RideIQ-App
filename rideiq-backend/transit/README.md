# RideIQ Transit Engine (OpenTripPlanner)

Phase 1 of [TRANSIT_PLAN.md](../../TRANSIT_PLAN.md): a real public-transit router for
Edmonton, running locally, on free open data and no API key.

## Why a separate engine

The A\* in [routing.py](../routing.py) searches a graph whose edge weights never change.
Transit is *time-dependent* — you have to reach the stop before the bus leaves, and
after that the timetable, not the geometry, decides where you can be and when. That is a
different algorithm (RAPTOR), and OpenTripPlanner already implements it over GTFS plus
the walk-to-stop stitching. We run OTP and translate its answer instead of writing our own.

## What the data is

| Input | Source | Size | Licence |
|---|---|---|---|
| `gtfs.zip` | City of Edmonton, `gtfs.edmonton.ca/TMGTFSRealTimeWebService/GTFS/gtfs.zip` | ~22 MB | Open data, no key |
| `alberta-latest.osm.pbf` | Geofabrik | ~350 MB | ODbL |
| `edmonton.osm.pbf` | clipped from the above by `clip_osm.py` | ~40 MB | ODbL |
| `otp-shaded-2.9.0.jar` | OpenTripPlanner GitHub releases | ~183 MB | LGPL |
| `graph.obj` | built from GTFS + OSM | ~hundreds of MB | — |

The ETS feed covers **7 agencies**, not just Edmonton: ETS, St. Albert, Strathcona County,
Spruce Grove, Fort Saskatchewan, Beaumont, and Leduc — about 6,800 stops across the metro.
`clip_osm.py` derives its bounding box from the feed's own stop extent, so the street
extract automatically covers wherever those agencies actually run.

None of this is committed — see [.gitignore](.gitignore). It is all reproducible from
`python otp.py fetch`.

## Running it

```bash
pip install osmium          # only needed for the clip step
python otp.py all           # fetch + clip + build  (downloads ~550 MB, takes a while)
python otp.py serve         # http://localhost:8081
```

Then, in a second shell:

```bash
python otp.py plan
```

which asks for a downtown → University of Alberta trip and prints the itineraries. If you
get legs back with real route numbers, Phase 1 is done.

Individual steps are available too: `fetch`, `clip`, `build`, `serve`, `plan`.

## Requirements

- **Java 21+** — OTP 2.9 will not start on anything older.
- **RAM** — the build wants a large heap. The default is `-Xmx6G`; override with
  `OTP_HEAP=4G` if your machine is tighter, but the build gets slower and can fail
  outright below about 4 GB.
- **Port 8081** — override with `OTP_PORT`, or `--port` on any subcommand.

## Talking to it

OTP 2.9 serves a GraphQL API at `/otp/gtfs/v1` (the old REST `plan` endpoint is gone).
There is a query browser at `http://localhost:8081/graphiql` for poking at it by hand.
`otp.py plan` shows the minimal query shape that Phase 2's `/transit` endpoint builds on.

## Live data (GTFS-realtime)

The static graph answers "what is the plan". Three GTFS-realtime feeds, configured
as OTP updaters in [router-config.json](otp-config/router-config.json), answer "is it
actually running on time":

| Feed | Polled | Gives us |
|---|---|---|
| TripUpdates | 45 s | Delays — every leg's clock time is delay-adjusted |
| VehiclePositions | 30 s | Where the buses actually are |
| Alerts | 2 min | Detours, closures, elevator outages |

Measured coverage, so nobody is surprised later:

- **Vehicle positions: ~98%** applied.
- **Trip updates: ~61%** applied. The rest are not a config problem. ETS identifies
  those trips using GTFS-realtime's newer `modified_trip` / trip-modifications
  extension instead of a plain `trip_id`, and OTP 2.9 does not read it for
  stop-time updates. `fuzzyTripMatching` does not rescue them. Those trips fall
  back to their scheduled times — correct, just not live.
- In practice **buses usually carry realtime and the LRT lines usually do not.**

So `/transit` reports `realtime` and `status` per itinerary, and the app shows a
"● Live · 2 min late" badge **only** when a feed actually said so. An itinerary
with no live data shows no badge, rather than "on time" — which would be a guess
dressed up as a fact.

If OTP runs somewhere with no outbound internet, the updaters log fetch errors and
the engine keeps serving the timetable. Realtime degrades; routing does not break.

## Deploying it

Two GitHub Actions workflows, deliberately separate from the app's deploy:

| Workflow | When | What it does |
|---|---|---|
| [build-transit-graph](../../.github/workflows/build-transit-graph.yml) | manual, plus the 1st and 15th | Rebuilds `graph.obj` in the cloud and publishes it as the rolling `transit-graph` release asset |
| [deploy-transit](../../.github/workflows/deploy-transit.yml) | manual, or triggered by the above | Builds the OTP image, copies the graph to EC2, restarts `rideiq-otp` |

The engine listens on the private `appnet` docker network only — nginx never
proxies to it, and no port is published. The only thing that talks to OTP is
FastAPI.

This is **not** blue-green. Two JVMs holding the graph will not fit on the 8 GB
host next to two API replicas and redis, so a graph swap costs about a minute of
`/transit` returning 503. That is the one endpoint affected; driving and walking
routes never touch OTP, and the app falls back to the Maps handoff meanwhile.

Locally the same thing runs as a compose profile:

```bash
docker compose --profile transit up --build
```

## Keeping schedules current

`feed_info.txt` in the ETS feed carries `feed_start_date` / `feed_end_date` — usually a
window of a few weeks. Once the graph is past `feed_end_date`, OTP returns no itineraries
at all rather than wrong ones. Re-run `fetch` + `build` before then; Phase 3 automates
this as a scheduled GitHub Actions job.
