# RideIQ — Native Public Transit Plan

Goal: add **in-app public transit routing** (bus + LRT) for Edmonton — "leave now, get from A to B by transit," with walk-to-stop legs, which route to ride, transfers, and departure/arrival times — **without** a paid Google/Mapbox API, matching RideIQ's free-and-self-hosted approach.

Honest framing: this is a **real backend project**, not a small app tweak. Transit routing is fundamentally different from the driving/walking A\* we already have (it's *time-dependent* — the answer depends on the clock and the schedule). The practical way to do it well is to run a proven engine (**OpenTripPlanner**) fed by Edmonton's open **GTFS** data, and have our FastAPI backend proxy to it. The current "Transit" button (handoff to Google Maps) stays as the fallback until this ships.

---

## 1. Why transit routing is different (the core idea)

Our A\* finds the shortest path on a fixed graph — distance doesn't change with time. Transit is **time-dependent**: to ride bus 8 you must be at the stop *before* it departs, then you're constrained by the schedule until you alight, maybe transfer, and walk to the destination. The classic algorithms are:

- **RAPTOR** (Round-bAsed Public Transit Optimized Router) — rounds = number of transfers; fast, no heavy preprocessing.
- **Connection Scan Algorithm (CSA)** — scans timetable "connections" in departure-time order.
- **Time-dependent Dijkstra** — simplest, slowest.

You do **not** want to hand-roll these. **OpenTripPlanner (OTP2)** already implements RAPTOR over GTFS + a street graph and does the walk↔transit stitching for us.

---

## 2. The data: GTFS

Transit routing runs on **GTFS** (General Transit Feed Specification) — a standard zip of CSVs: `stops.txt`, `routes.txt`, `trips.txt`, `stop_times.txt`, `calendar.txt`, `shapes.txt`, etc.

- **Static GTFS** (schedules) — Edmonton Transit Service (ETS) publishes this on the **City of Edmonton Open Data Portal** (data.edmonton.ca); it's also mirrored on the Mobility Database / transit.land. Grab the current ETS GTFS zip URL from there (feeds move, so pull the live link rather than hard-coding an old one).
- **GTFS-realtime** (optional, Phase 2) — live vehicle positions, trip delays, and service alerts as Protobuf feeds. Layer this on later for "bus is 3 min late" accuracy.

License: ETS/City of Edmonton open data — free to use. No API key.

---

## 3. Architecture (adds one service)

```
Android app  ──"transit" mode──►  FastAPI  ──HTTP──►  OpenTripPlanner (OTP2)
                                    │                    │
                                    │                    ├─ GTFS (ETS schedules)
                                    │                    └─ OSM street graph (walk legs)
                                    └─ normalizes OTP's itinerary → RideIQ leg format → app
```

- **OTP runs as its own container** alongside the app (a sidecar), on the same EC2 host or a separate one. It's a Java service; it builds a graph from GTFS + OSM once at startup, then answers `/otp/routers/default/plan` requests in milliseconds.
- **FastAPI gains a `/transit` endpoint** that calls OTP, then reshapes OTP's verbose itinerary into RideIQ's simpler shape (legs the app already understands).
- The app keeps drawing everything on osmdroid — OTP returns leg geometries (encoded polylines) we decode and render.

Why proxy through FastAPI instead of calling OTP from the app directly: one public surface, caching in Redis, response normalization, and OTP stays private (only FastAPI reaches it).

---

## 4. API design

New backend endpoint:

```
GET /transit?lat1=&lon1=&lat2=&lon2=&depart=now|<ISO time>&max_walk_m=800
```

Returns 1–3 itineraries, each a list of **legs** in a shape the app can render with almost no new logic:

```json
{
  "itineraries": [{
    "duration_min": 34,
    "depart": "2026-08-17T17:42:00",
    "arrive": "2026-08-17T18:16:00",
    "transfers": 1,
    "legs": [
      {"mode": "WALK",  "from": "Pickup",        "to": "102 St & Jasper Ave", "distance_m": 260, "polyline_latlon": [[..]]},
      {"mode": "BUS",   "route": "8",  "headsign": "Abbottsfield", "from": "102 St & Jasper Ave", "to": "Coliseum Transit Ctr", "depart": "17:48", "arrive": "18:05", "polyline_latlon": [[..]]},
      {"mode": "WALK",  "from": "Coliseum Transit Ctr", "to": "Destination", "distance_m": 180, "polyline_latlon": [[..]]}
    ]
  }]
}
```

FastAPI builds this by translating OTP's GraphQL/REST `plan` response. Cache identical queries (rounded time) in Redis.

---

## 5. App changes

- **Mode selector becomes three-way:** `🚗 Drive | 🚶 Walk | 🚍 Transit` (extend the segmented pill we already built).
- **Transit rendering:** draw each leg in its own color — walk legs dashed grey, each transit leg colored (bus = a route color, LRT = its line color), with stop markers at boardings/alightings.
- **Route cards** show transit itineraries: total time, transfers, and "depart 5:48 → arrive 6:16," plus a per-leg breakdown ("Walk 3 min → Bus 8, 17 min → Walk 2 min").
- **Directions list** already exists — reuse it to show the leg-by-leg plan ("Board Bus 8 toward Abbottsfield at 102 St & Jasper Ave, 5:48 PM").
- No voice-nav for transit in v1 (schedule-based, not turn-based).

---

## 6. Deployment

- **OTP container** added to the stack. OTP needs memory: graph build for a city the size of Edmonton is roughly **1.5–3 GB RAM**; run it on the current 8 GB EC2 (fine) or a dedicated small instance.
- **Graph build workflow:** a new `build-transit-graph` GitHub Actions job (mirror of `build-city-graph`) downloads the current ETS GTFS + the Edmonton OSM extract, runs OTP's graph builder, and produces `graph.obj`. Commit/store it (it's large — consider an S3 bucket or a release asset rather than git), and the OTP container loads it on boot.
- **Blue-green:** OTP is stateless at request time, so it fits the same pattern; the app containers point at OTP over the private docker network.
- **Refresh cadence:** ETS updates GTFS periodically (service changes). Re-run the transit-graph build on a schedule (e.g., monthly, or via a scheduled workflow) so schedules stay current.

---

## 7. Phased roadmap (commit by commit)

**Status: all six phases are implemented.** See
[rideiq-backend/transit/README.md](rideiq-backend/transit/README.md) for how to run it.

| Phase | What ships | Status |
|---|---|---|
| **0** | Keep the Google-Maps transit handoff | Still there, now the fallback when the engine is down |
| **1** | Stand up OTP locally with ETS GTFS + Edmonton OSM | Done — `transit/otp.py all` |
| **2** | `GET /transit` in FastAPI proxying OTP + Redis cache | Done — plus `/transit/status` |
| **3** | OTP container in the deploy stack + `build-transit-graph` workflow | Done — plus `deploy-transit` |
| **4** | App: add 🚍 Transit mode + itinerary cards + colored legs | Done |
| **5** | GTFS-realtime (live delays + alerts) | Done — all three ETS feeds |
| **6** | Polish: fare info, accessibility (wheelchair) filters, "arrive by" | Done |

### What the build actually looked like

Several estimates in this plan turned out to be wrong in the useful direction, and
a few things bit that the plan did not anticipate:

- **The graph is far smaller than budgeted.** 291k vertices, 845k edges, 6,573
  stops, 623 patterns — a **96 MB** `graph.obj` that builds in **37 seconds**, not
  the hundreds of MB and "several minutes" assumed in §6. It serves comfortably in
  a 3 GB heap, so the 8 GB EC2 has more headroom than §6 feared.
- **The feed is metro-wide, not just Edmonton.** Seven agencies ride along in the
  same GTFS: ETS, St. Albert, Strathcona County, Spruce Grove, Fort Saskatchewan,
  Beaumont and Leduc. The OSM clip box is derived from the feed's own stop extent
  so it follows them.
- **OTP 2.9 needs Java 25, and refuses to start on 24.** The jar is class-file
  version 69. `otp.py` detects this and fetches a private Temurin JDK rather than
  leaving it as a prerequisite you discover from a stack trace.
- **The REST `plan` endpoint in §3 is gone.** OTP 2.9 serves GraphQL at
  `/otp/gtfs/v1`; that is what `transit_client.py` speaks.
- **Realtime coverage is partial and that is a feed property, not a bug.** ~98% of
  vehicle positions apply, but only ~61% of trip updates: ETS identifies the rest
  with GTFS-RT's newer `modified_trip` extension, which OTP 2.9 does not read for
  stop-time updates. Buses generally carry live data; the LRT lines generally do
  not. So the app shows a live badge *only* when a feed actually said something.
- **Fares needed a build-time feature flag** (`FaresV2`) and a total summed over
  fare-product *use ids*, not legs — otherwise a three-bus trip inside the transfer
  window prices at $9.00 instead of the $3.00 it actually costs.

## 8. Honest risks & trade-offs

- **Resource cost:** OTP is heavier than our Python backend (JVM + in-memory graph). Budget RAM and watch EC2 memory.
- **Data freshness:** schedules drift; you must rebuild the graph when ETS updates GTFS, or riders get wrong times.
- **Realtime is a second project:** static schedules answer "what's the plan"; GTFS-realtime answers "is it actually on time." Phase 5, not day one.
- **Coverage = whatever ETS publishes:** if a feed is missing a route or a service exception, OTP can't invent it.
- **Alternative if OTP is too heavy:** a Python RAPTOR/CSA implementation over GTFS (e.g., via `gtfs_kit` + a RAPTOR library) keeps everything in the FastAPI process — lighter, but you own more of the routing logic and the walk-access stitching. OTP is the safer, more complete choice; roll-your-own only if OTP's footprint is a problem.

---

## 9. What this buys the product

Real, self-hosted, multi-modal trip planning — drive, walk, **and** transit — in one app, on free open data. Combined with the ML fare/ETA brain, RideIQ could even compare modes head-to-head ("Transit: 34 min, $3.50 · Drive: 18 min, $12.40 · Walk: 1 h 5 min") — a genuinely useful, differentiated feature that Google gives away but few self-hosted apps offer.
