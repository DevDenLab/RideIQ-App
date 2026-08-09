# RideIQ — Deployment Plan (FastAPI backend → AWS EC2, blue-green, public IP)

This plan applies the **exact same deployment pattern** as your `CICD Pipeline Project/.github/workflows/linux-deploy.yml`
(build in the cloud → push image to `ghcr.io` → SSH into EC2 → blue-green swap behind nginx → rollback via `workflow_dispatch`)
to the **real RideIQ backend** (`rideiq-backend/app.py`), so your exported `.apk` reaches it over a public IP.

The philosophy is unchanged: **get one thing working end-to-end first, then add features commit by commit.**

---

## 0. What we are actually deploying (and the key difference from the POC)

| | CICD POC (`linux-deploy.yml`) | RideIQ (this plan) |
|---|---|---|
| App | toy `/items` CRUD, in-memory | `app:app` — 15 ML models, `/quote`, `/route`, `/landmarks`, `/health` |
| Container port | 8000 | 8000 (same) |
| Extra services | none | **Redis** (cache) — must run alongside the app |
| State | resets on restart (fine) | **SQLite `rideiq.db`** logs quotes → needs a host volume to survive swaps |
| Image weight | tiny | heavy: sklearn, scipy, matplotlib, hmmlearn, pgmpy, osmnx graph (`city_graph.json` ~8 MB) |
| Startup | instant | **slow** — trains all 15 models at import; needs RAM |

**Vital consequence:** the backend trains every model on boot. On a 1 GB `t2.micro` this can OOM or take
too long, and the health-check loop will time out and roll back. **Use a `t3.small` (2 GB) minimum,
`t3.medium` (4 GB) recommended.** This is the single most likely thing to bite you — size the box first.

**Your Android app is already wired to this backend.** `ApiService.java` calls `/quote`, `/route`,
`/landmarks`, `/surge-zones`, etc. — all of which exist in `rideiq-backend/app.py`. The only thing standing
between your `.apk` and a working app is: (a) the backend running on a public IP, and (b) one line in
`ApiClient.java` pointing at that IP.

---

## 1. Target architecture (identical spirit to your POC)

```
GitHub push (main)
   │
   ├─ BUILD job (GitHub cloud runner): docker build rideiq-backend → push ghcr.io:sha-<commit> + :latest
   │
   └─ DEPLOY job (GitHub cloud runner SSHes into EC2):
          on the EC2 box, over the private docker network "appnet":
             redis           (shared, started once, never swapped)
             rideiq-blue  ┐  ← only ONE color live at a time
             rideiq-green ┘
             cicd-edge (nginx :80)  → routes to the live color
          rollback = re-run workflow with an old commit SHA (skips build, redeploys that image)

Android .apk  ──HTTP──►  http://<ELASTIC_IP>/  (nginx :80)  ──►  live color :8000
```

Everything the POC gave you carries over: **zero-downtime swap**, **health-gated cutover**,
**one-click rollback**, **public entrypoint via Elastic IP**.

---

## 2. Phase 0 — one-time AWS setup (do this once, by hand)

1. **Launch an EC2 instance** — Ubuntu 22.04 LTS, **t3.small or t3.medium**, 20 GB disk.
2. **Allocate an Elastic IP** and associate it with the instance. This is your permanent public IP —
   the value that goes into the Android app and into the `EC2_HOST` secret.
3. **Security group inbound rules:** allow `22` (SSH, ideally your IP only) and `80` (HTTP, open).
   Do *not* expose 8000 or 6379 publicly — nginx is the only public door.
4. **Install Docker on the box:**
   ```bash
   sudo apt-get update && sudo apt-get install -y docker.io
   sudo usermod -aG docker ubuntu   # log out/in after this
   ```
5. **Create the SSH deploy key** (`deploy.pem` when you launched the instance). You'll paste its contents
   into a GitHub secret.
6. **Add GitHub repo secrets** (Settings → Secrets and variables → Actions) — same names as your POC:
   - `EC2_HOST` = the Elastic IP
   - `EC2_USER` = `ubuntu`
   - `EC2_SSH_KEY` = full contents of `deploy.pem`
7. **Make the ghcr.io image public** (Packages → package settings → visibility: public), so the VM pulls
   with no login — exactly like your POC comment says.

---

## 3. Phase 1 — first deploy (the milestone: `.apk` works over public IP)

The goal of Phase 1 is **not** to add features. It is to prove the full loop:
`push → build → blue-green deploy → nginx :80 → phone app gets a real quote.` Nothing else.

**Step A — Dockerfile is ready.** `rideiq-backend/Dockerfile` already builds the whole app and installs
`libgomp1` + fonts (needed by sklearn/matplotlib). No change needed for Phase 1.

**Step B — add the deploy workflow** (see Section 4). Commit it under
`rideiq-backend/.github/workflows/` *or* at your repo root depending on how you host the backend repo.

**Step C — point the Android app at the public IP.** In `ApiClient.java`:
```java
// BEFORE (emulator + local nginx :8080)
public static final String BASE_URL = "http://10.0.2.2:8080/";
// AFTER (real device hitting EC2 nginx :80)
public static final String BASE_URL = "http://<ELASTIC_IP>/";
```
`AndroidManifest.xml` already has `android:usesCleartextTraffic="true"`, so plain HTTP is allowed
(fine for Phase 1; we add HTTPS later).

**Step D — export the `.apk`.** Android Studio → Build → Build Bundle(s)/APK(s) → Build APK(s),
or `./gradlew assembleRelease`. Install on a phone that has internet. Open the app, tap **Get quote** —
it should hit `http://<ELASTIC_IP>/quote` and return an ETA/fare.

**Definition of done for Phase 1:** you sideload the APK on a phone (not the emulator), it reaches the
EC2 box over the public IP, and you get a live quote and a route on the map. Commit and stop here.

---

## 4. The deploy workflow (adapted from your `linux-deploy.yml`)

Same three ideas as your file — compute image ref, SSH in, blue-green swap — with three RideIQ additions
marked **[NEW]**: (1) start a shared **redis**, (2) mount a **volume for the SQLite DB**, (3) longer health
timeout because model training is slow.

```yaml
name: deploy-rideiq

on:
  push:
    branches: [ "main" ]
  workflow_dispatch:
    inputs:
      rollback_sha:
        description: "Full commit SHA to redeploy (blank = latest build)"
        required: false
        default: ""

permissions:
  contents: read
  packages: write

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}

jobs:
  build-and-push:
    if: ${{ github.event.inputs.rollback_sha == '' }}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - id: img
        run: echo "name=${IMAGE_NAME,,}" >> "$GITHUB_OUTPUT"
      - uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.REGISTRY }}/${{ steps.img.outputs.name }}
          tags: |
            type=sha,format=long
            type=raw,value=latest
      - uses: docker/build-push-action@v6
        with:
          context: ./rideiq-backend        # <-- build the RideIQ backend, not the POC
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

  deploy:
    needs: build-and-push
    if: ${{ always() && (needs.build-and-push.result == 'success' || github.event.inputs.rollback_sha != '') }}
    runs-on: ubuntu-latest
    concurrency: { group: deploy-rideiq, cancel-in-progress: false }
    steps:
      - id: ref
        run: |
          IMAGE="${GITHUB_REPOSITORY,,}"
          SHA="${{ github.event.inputs.rollback_sha }}"
          if [ -z "$SHA" ]; then SHA="$GITHUB_SHA"; fi
          echo "ref=${REGISTRY}/${IMAGE}:sha-${SHA}" >> "$GITHUB_OUTPUT"

      - name: Deploy to EC2 over SSH
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.EC2_HOST }}
          username: ${{ secrets.EC2_USER }}
          key: ${{ secrets.EC2_SSH_KEY }}
          script: |
            set -e
            REF="${{ steps.ref.outputs.ref }}"
            docker pull "$REF"
            docker network inspect appnet >/dev/null 2>&1 || docker network create appnet

            # [NEW] shared redis — started once, NEVER swapped.
            docker ps -q -f "name=^rideiq-redis$" | grep -q . || \
              docker run -d --name rideiq-redis --network appnet --restart unless-stopped redis:7-alpine

            # pick the idle color
            if docker ps -q -f "name=^rideiq-blue$" | grep -q .; then OLD=blue; NEW=green
            elif docker ps -q -f "name=^rideiq-green$" | grep -q .; then OLD=green; NEW=blue
            else OLD=""; NEW=blue; fi
            NEW_NAME="rideiq-$NEW"

            docker ps -aq -f "name=^${NEW_NAME}$" | grep -q . && docker rm -f "$NEW_NAME" || true
            # [NEW] REDIS_URL + a host volume so rideiq.db survives swaps
            docker run -d --name "$NEW_NAME" --network appnet --restart unless-stopped \
              -e REDIS_URL=redis://rideiq-redis:6379/0 \
              -e DB_PATH=/data/rideiq.db \
              -e INSTANCE_ID="$NEW" \
              -v rideiq_data:/data \
              "$REF"

            # [NEW] health check — up to ~60s because 15 models train on boot
            HEALTHY=0
            for i in $(seq 1 30); do
              if docker exec "$NEW_NAME" python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)" >/dev/null 2>&1; then
                HEALTHY=1; break
              fi
              echo "waiting for $NEW_NAME... ($i)"; sleep 2
            done
            if [ "$HEALTHY" -ne 1 ]; then
              echo "NEW color never healthy — aborting WITHOUT switching."
              docker logs "$NEW_NAME" || true; docker rm -f "$NEW_NAME" || true; exit 1
            fi

            # point nginx at the new color
            cat > /tmp/default.conf <<EOF
            upstream app { server ${NEW_NAME}:8000; }
            server {
              listen 80;
              location / { proxy_pass http://app; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; }
            }
            EOF
            if ! docker ps -aq -f "name=^cicd-edge$" | grep -q .; then
              docker run -d --name cicd-edge --network appnet --restart unless-stopped -p 80:80 nginx:1.27-alpine
              sleep 2
            fi
            docker cp /tmp/default.conf cicd-edge:/etc/nginx/conf.d/default.conf
            docker exec cicd-edge nginx -s reload

            # drain + retire old color
            if [ -n "$OLD" ]; then sleep 30; docker rm -f "rideiq-$OLD" || true; fi
            echo "Deployed $REF (active: $NEW)."
```

---

## 5. How blue-green + rollback behave (same as your POC)

- **Zero downtime:** the new color must pass `/health` *before* nginx is repointed. If it never boots
  (bad import, OOM, missing dep), we never switch — the old color keeps serving. This is exactly your POC's
  guarantee, and it's why the commented-out `# import this_module_does_not_exist` line in the POC was a safe
  demo of a failed deploy.
- **Rollback:** Actions → *Run workflow* → paste an old commit SHA into `rollback_sha`. Build is skipped;
  the deploy job pulls `ghcr.io/...:sha-<that>` and swaps to it. Rolling back is just a normal deploy of an
  older image.
- **`concurrency`** prevents two deploys colliding on the box.

---

## 6. Commit-by-commit roadmap (features come AFTER Phase 1 is green)

Each row is one focused PR/commit. Deploy, verify on the phone, then take the next one. Never batch these.

| # | Commit | Backend change | Android change | Why this order |
|---|---|---|---|---|
| 1 | **Public-IP baseline** | deploy workflow only | `BASE_URL` → Elastic IP | prove the loop end-to-end |
| 2 | **HTTPS + domain** | Caddy/nginx TLS, DNS name | `BASE_URL` → `https://...`; drop cleartext | can't ship real app on plain HTTP |
| 3 | **Live location** | `POST /driver/location`, `GET /trip/{id}/location` (store last ping) | foreground service + `FusedLocationProvider`, send lat/lon every few sec | first "real-time" feature; small surface |
| 4 | **Live location push** | WebSocket or SSE channel per trip | subscribe, move the marker live | upgrade polling → push once polling works |
| 5 | **Audio transcription** | `POST /transcribe` (Whisper/`faster-whisper`) → text | record audio, upload, show text | heavy dep → its own commit; may need a worker |
| 6 | **Voice navigation** | reuse `/route-latlon` turns | Android TextToSpeech reads turns aloud | your note: "direction shown by text and audio" |
| 7 | **Trip persistence** | move SQLite → Postgres (RDS) | none | multi-instance needs a shared real DB |
| 8 | **Auth** | `/login`, JWT | token header on requests | before any real users |

---

## 7. Later — system-design hardening (once features are in)

Add these one at a time, same discipline. Each is a good "make my CI/CD + system-design understanding solid" milestone:

- **TLS termination** (Caddy auto-HTTPS or nginx + certbot) — first, always.
- **Managed DB** (RDS Postgres) replacing SQLite — enables >1 replica safely.
- **Real load balancing / autoscaling** — swap single-box nginx for an ALB + 2 EC2s, or move to ECS.
- **Redis as more than cache** — rate limiting, live-location fan-out, session store.
- **Background worker + queue** (Celery/RQ + Redis) for transcription so requests don't block.
- **Observability** — `/metrics` (already exists) → Prometheus + Grafana; centralized logs.
- **CI quality gates** — tests, lint, image scan *before* the build job, so bad code never ships.
- **Staging environment** — a second EC2/color set that `main` deploys to before `production`.
- **Secrets manager** — move env vars out of the compose/run command.

---

## 8. Gotchas checklist (the "must-know" list)

- **Instance too small → OOM on model training.** Use ≥ 2 GB RAM. #1 cause of failed first deploys.
- **Health timeout too short.** Training is slow; the workflow above waits ~60s. Keep it generous.
- **SQLite on a volume.** Without `-v rideiq_data:/data`, every swap wipes logged quotes.
- **Redis not started.** The app falls back to a local in-process cache if `REDIS_URL` is unreachable —
  works, but you lose shared caching. The workflow starts a shared redis container.
- **`city_graph.json` must be in the image.** It is (Dockerfile `COPY . .`). If real-city routing says it
  needs the graph, the file didn't get copied — check `.dockerignore`.
- **Security group.** Only 22 + 80 open. Never expose 8000/6379.
- **HTTP is Phase-1 only.** Ship HTTPS (commit #2) before real users; app stores also require it.
```
