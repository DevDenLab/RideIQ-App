# Diagram assets for RideIQ

(Folder is named `Diagram` — your note wrote "Dairgam".)

## What's here

**Start with [`00-index.json`](00-index.json)** — the manifest for the current set.

### Current set (v2, generated 2026-08-25)

Seventeen standalone files. Each carries its own `image_prompt` plus a `spec` you paste
underneath as the exact structure to follow.

| Group | Files |
|---|---|
| **Architecture** | `01-system-architecture` (hero), `02-deployment-topology`, `03-cicd-pipeline`, `04-transit-data-pipeline` |
| **Sequence / flow** | `05-seq-ride-quote`, `06-seq-drive-route`, `07-seq-transit-plan`, `08-seq-turn-by-turn-nav`, `09-seq-bluegreen-deploy` |
| **Algorithms** | `10-algo-astar`, `11-algo-alternative-routes`, `12-algo-raptor-vs-astar`, `13-algo-ml-map`, `14-algo-fare-transfer`, `15-algo-polyline-encoding`, `16-algo-realtime-fusion` |

If you only make one, make `01`. If you only understand one, understand `12` — why transit
needs a different algorithm than driving is the idea the whole transit engine rests on.

Every number in the set was read from the running system or the source, not estimated.

### Superseded

- **`architecture.json`**, **`system-architecture.json`** — predate the transit engine and
  the current CI/CD. Kept for history; use `01-system-architecture.json` instead.
- **`algorithms/`** — per-feature ML JSONs, still accurate. `13-algo-ml-map.json` indexes them.

## Older notes


- **`architecture.json`** — the full system + deployment architecture, structured for **Gemini / Nano Banana Pro**.
  Use the `image_prompt` field as your main instruction, and the `spec` (zones, nodes, connections) as the exact
  structure to draw.
- **`algorithms/`** — one JSON per feature group, each with inputs, the algorithms used, how their outputs combine,
  the request flow, and its own `image_prompt` for a per-feature flowchart:
  `eta`, `fare`, `cancellation-risk`, `fraud-check`, `surge-zones`, `rider-segments`, `driver-shift`,
  `cancellation-causes`, `routing`, `_quote-orchestration`.

## How to use these with Gemini Nano Banana Pro

1. Open the JSON you want (e.g. `architecture.json`).
2. Paste the `image_prompt` string as your prompt.
3. Paste the rest of the JSON below it as "here is the exact structure — follow node/connection labels precisely."
4. Ask for 16:9, flat modern style. Regenerate a couple of times; image models drift on text, so pick the
   cleanest one and, if needed, do a second pass asking it to fix mislabeled arrows.

Tip: image generators are unreliable with lots of small text. For anything that must be **exactly right**
(the blue-green swap, the ML pipeline), also make a **Mermaid** version (deterministic, perfect labels) and use
the Gemini image only for the "hero" architecture picture.

## Recommended diagrams to make (beyond the hero architecture)

| Diagram | Best tool | Why it's worth it |
|---|---|---|
| **Deployment topology** (EC2 box, nginx, blue/green, redis, sqlite, ghcr) | Nano Banana (hero) + Mermaid | your headline picture; shows the whole system at a glance |
| **Blue-green swap sequence** | Mermaid `sequenceDiagram` | shows health-gate → nginx reload → drain old color; the core CI/CD idea |
| **CI/CD pipeline flow** | Mermaid `flowchart` | push → build → push image → SSH → deploy → (rollback loop) |
| **Request sequence** (phone → nginx → app → ML/redis/sqlite → response) | Mermaid `sequenceDiagram` | how one `/quote` actually flows end-to-end |
| **ML pipeline / algorithm map** | Mermaid `flowchart` | the 15 models grouped into 8 features + routing (from `algorithms/`) |
| **A\* routing flow** | Mermaid `flowchart` | graph → snap nodes → A\* → chain ETA+fare → polyline |
| **Data / ER model** | Mermaid `erDiagram` | once you add Postgres (roadmap commit #7) |
| **State machine: driver/trip** | Mermaid `stateDiagram` | idle → en-route → on-trip (mirrors the HMM feature) |
| **Rollback decision flow** | Mermaid `flowchart` | when/how you trigger `rollback_sha` |

I can generate any of these as ready-to-render Mermaid (`.mmd`) files — just say which.

## Your question: "Is the app without the backend?"

**No — the RideIQ Android app is not standalone; it needs the backend for its core features.**
`ApiService.java` calls `/quote`, `/route-latlon`, `/landmarks`, `/surge-zones`, etc., and every ML result
(ETA, fare, cancellation risk, routing, analytics plots) is computed server-side in `rideiq-backend/app.py`.

What the app can do **without** the backend:
- draw the OpenStreetMap map and let you drop pins (osmdroid tiles load directly from OSM/CARTO/Esri).

What **breaks** without the backend:
- getting a quote, computing a route/ETA/fare, loading landmarks, and all analytics screens — they show
  "Can't reach server."

That's exactly why Phase 1 of the deployment plan is "get the backend on a public IP and point `BASE_URL` at it"
before adding any new features.
