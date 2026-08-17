# RideIQ — Local Setup & Run Guide

RideIQ is a self-hosted navigation + ride-hailing app:

- **Backend** (`rideiq-backend/`) — a Python **FastAPI** service with an A\* routing engine and 15 machine-learning models. Maps/routing use free OpenStreetMap data (no Google Maps, no API key).
- **Frontend** (`rideiq-android/`) — a native **Android** app (Java, osmdroid, Retrofit).

This guide takes you from a clean machine to running **both** locally. Commands are written for **Windows (PowerShell)**; macOS/Linux notes are included where they differ.

> Heads-up: the frontend is a **native Android app**, not a web app — so **Node.js is not required** for RideIQ. It's listed below only because it was asked for; skip it unless you want it for other tooling.

---

## 0. What you'll install

| Tool | Needed for | Notes |
|---|---|---|
| **Git** | getting the code | required |
| **Python 3.11** | running the backend | 3.10–3.12 also fine |
| **Android Studio** | building/running the app | bundles the JDK, Gradle, Android SDK, and emulator |
| **Docker Desktop** | (optional) run backend in containers | only if you want the full nginx + Redis stack |
| **Node.js** | (optional) not used by RideIQ | skip unless you need it elsewhere |

---

## 1. Install the prerequisites

### 1.1 Git
- **Windows:** download from https://git-scm.com/download/win and run the installer (accept defaults). Verify:
  ```powershell
  git --version
  ```
- **macOS:** `brew install git`  •  **Linux:** `sudo apt install git`

### 1.2 Python 3.11
- **Windows:** get it from https://www.python.org/downloads/ (or the Microsoft Store). **Important:** on the first installer screen, tick **"Add Python to PATH."** Verify:
  ```powershell
  python --version
  pip --version
  ```
- **macOS:** `brew install python@3.11`  •  **Linux:** `sudo apt install python3.11 python3.11-venv python3-pip`

### 1.3 Android Studio
1. Download from https://developer.android.com/studio and install (accept defaults — this also installs the JDK, Android SDK, and Gradle).
2. Launch it once and let the **Setup Wizard** finish downloading the SDK components.
3. Create an emulator: **More Actions → Virtual Device Manager → Create Device** → pick e.g. *Pixel 7*, choose a system image (API 34 recommended; matches the app's `targetSdk 34`), Finish.
   - Or use a **physical phone**: enable **Developer options → USB debugging** and plug it in.

### 1.4 (Optional) Docker Desktop
Only if you want to run the backend the same way it runs in production (nginx + Redis + two app replicas). Install from https://www.docker.com/products/docker-desktop and make sure it's running (whale icon).

### 1.5 (Optional) Node.js
Not used by RideIQ. If you want it anyway: https://nodejs.org (LTS), then `node --version`.

---

## 2. Get the code

```powershell
cd C:\Users\tatva\OneDrive\Desktop
git clone https://github.com/DevDenLab/RideIQ-App.git
cd RideIQ-App
```

(If you already have the folder, just `cd` into it.)

---

## 3. Run the BACKEND locally

You have two options. **Option A (no Docker)** is the fastest for development.

### Option A — Run directly with Python (recommended for dev)

```powershell
cd rideiq-backend

# 1) create an isolated environment
python -m venv venv

# 2) activate it
venv\Scripts\Activate.ps1          # Windows PowerShell
# source venv/bin/activate         # macOS/Linux

# 3) install dependencies (this pulls numpy, scikit-learn, scipy, matplotlib, etc. — a few minutes)
pip install --upgrade pip
pip install -r requirements.txt

# 4) start the API
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

Now open **http://localhost:8000/docs** — FastAPI's interactive API page. Try `GET /health` (should return `{"status":"ok"}`), then `POST /route-latlon` or `GET /search?q=Rogers Place`.

Notes:
- **Redis is optional locally.** If `REDIS_URL` isn't set, the app automatically uses an in-process cache — you don't need to install Redis for development.
- Real-city driving routing works out of the box (`city_graph.json` is included). Walking routing needs `walk_graph.json` — see section 6.

### Option B — Run with Docker (full stack)

```powershell
cd rideiq-backend
docker compose up --build
```

This starts Redis, two FastAPI replicas, and nginx. The API is then served through nginx at **http://localhost:8080** (docs at http://localhost:8080/docs). Stop it with `Ctrl+C`, or `docker compose down`.

---

## 4. Run the FRONTEND (Android app)

### 4.1 Open the project
1. In Android Studio: **File → Open** → select the `rideiq-android` folder → **OK**.
2. Wait for **Gradle sync** to finish (bottom status bar). First sync downloads Gradle 9.3 and dependencies — give it a few minutes.

### 4.2 Point the app at your local backend
The app talks to the backend through one line. Open:

`rideiq-android/app/src/main/java/com/example/rideiq/ApiClient.java`

and set `BASE_URL` to your local backend using the table below, then **rebuild**:

| You're running the app on… | Backend started with… | Set `BASE_URL` to |
|---|---|---|
| **Emulator** | Option A (uvicorn :8000) | `http://10.0.2.2:8000/` |
| **Emulator** | Option B (docker compose) | `http://10.0.2.2:8080/` |
| **Physical phone (same Wi‑Fi)** | Option A | `http://<YOUR-PC-LAN-IP>:8000/` |
| **Physical phone (same Wi‑Fi)** | Option B | `http://<YOUR-PC-LAN-IP>:8080/` |

- `10.0.2.2` is a special alias that means "the host PC" **from inside the Android emulator.**
- Find your PC's LAN IP with `ipconfig` (look for IPv4, e.g. `192.168.1.20`). Your phone and PC must be on the same network.
- Plain `http://` is fine — the app already allows cleartext traffic for local dev.

### 4.3 Run it
1. Pick your emulator or device in the toolbar dropdown.
2. Click **Run ▶** (or `Shift+F10`). The app installs and launches on the Map screen.
3. **Emulator location:** the emulator's GPS is fake — set it via the emulator's **Extended controls (···) → Location** (search a place or play a route to simulate movement for navigation).

### 4.4 Build a shareable APK (optional)
**Build → Build Bundle(s)/APK(s) → Build APK(s)** → the `.apk` appears under `app/build/outputs/apk/`.

---

## 5. Verify the whole thing works
1. Backend running (`/health` returns ok at `/docs`).
2. App's `BASE_URL` points at that backend; app rebuilt.
3. In the app: type a destination (autocomplete should suggest addresses), pick a route card, tap **Start trip**. If you see routes and hear/see turns, frontend ↔ backend are connected.

---

## 6. (Optional) Build the walking graph
Driving works out of the box. For accurate **walking** routes you need `walk_graph.json`:

```powershell
cd rideiq-backend
pip install osmnx matplotlib
python build_city_graph.py "Edmonton, Canada" walk
```

Restart the backend and walk mode will use real footpaths. (You can also run it with `drive` to rebuild the driving graph, or `both`.)

---

## 7. Troubleshooting

- **App says "Can't reach server."** `BASE_URL` is wrong, the backend isn't running, or (physical phone) you used `10.0.2.2` instead of your PC's LAN IP. Also confirm your firewall allows inbound connections on port 8000/8080.
- **`pip install` fails on a scientific package.** Upgrade pip first (`pip install --upgrade pip`), and make sure you're on Python 3.10–3.12. On Windows, the prebuilt wheels normally install without a compiler.
- **Gradle sync fails / SDK not found.** In Android Studio: **File → Settings → SDK Manager**, install the **Android 14 (API 34)** SDK platform.
- **Emulator location won't change.** Use the emulator's Extended controls → Location tab; real GPS movement only happens on a physical device.
- **PowerShell blocks `Activate.ps1`.** Run once: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then activate again.

---

## Project layout

```
RideIQ-App/
├─ rideiq-backend/          FastAPI backend (Python)
│  ├─ app.py                API endpoints
│  ├─ routing.py            A* routing (drive + walk graphs)
│  ├─ models.py             15 ML models
│  ├─ build_city_graph.py   build road graphs from OpenStreetMap
│  ├─ requirements.txt
│  ├─ Dockerfile / docker-compose.yml
│  └─ city_graph.json       (walk_graph.json is optional, see §6)
├─ rideiq-android/          Android app (Java)
│  └─ app/src/main/java/com/example/rideiq/…
└─ .github/workflows/       CI/CD (deploy-rideiq, build-city-graph)
```
