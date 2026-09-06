"""
mlloop.py -- close the loop: log what was predicted, record what happened, and
score a challenger model against live traffic without serving it.

Where this started
------------------
Sixteen models trained entirely on synthetically generated trips. That is not a
risk of training-serving skew, it is skew by construction: the relationship
between distance and duration that the ETA model learned was invented by
rideiq_data.generate_trips(), and no part of the system has ever compared it to
a real journey.

The stated fix was "you are already logging every quote to SQLite, nothing reads
it back". Half of that turned out to be wrong, and the wrong half matters. The
quotes table is written by /quote -- an endpoint the Android app never calls. The
predictions riders actually see come from /route-latlon, which logged nothing at
all. So the loop was not merely unclosed; it had no input.

Worse, the quotes table records the ETA the model PREDICTED. Training on that
would distil the existing model into a new one and reproduce every one of its
errors with a fresh coat of paint. Closing a loop needs outcomes, not
predictions, and outcomes have to be collected -- which is what /trip-outcome
and the `outcomes` table below are for.

What is real today, and what is not
-----------------------------------
Working now, on live traffic:
  * every served prediction is logged with its features and an id
  * a challenger model, if one exists, scores the same traffic OFF the request
    path and its disagreement with the champion is recorded
  * /shadow-report says where the two models differ and by how much

Waiting on data:
  * retraining. retrain.py will not train on fewer than MIN_TRAIN_SAMPLES real
    outcomes and does not pretend otherwise. A fresh deployment has zero, and it
    will say so rather than producing a model.

Shadow deployment fits this architecture unusually well. Blue-green already runs
both colours during a deploy, so scoring live traffic through the idle one is
nearly free -- and shadow scoring never touches the response, so a challenger
that is slow or throws cannot hurt a rider.
"""
import os
import queue
import sqlite3
import threading
import time

# How few outcomes is too few to learn anything from. Not a tuned number -- it is
# a floor below which any metric computed is noise, and shipping a model fitted
# to 40 trips would be worse than shipping the synthetic one, because it would
# look empirical.
MIN_TRAIN_SAMPLES = int(os.environ.get("MIN_TRAIN_SAMPLES", "500"))

CHALLENGER_PATH = os.environ.get(
    "CHALLENGER_PATH", os.path.join(os.path.dirname(__file__), "challenger.joblib"))

# Bounded on purpose. If shadow scoring cannot keep up, the right behaviour is to
# lose shadow rows, never to slow down or block a rider's request.
SHADOW_QUEUE_MAX = int(os.environ.get("SHADOW_QUEUE_MAX", "2000"))

SHADOW_ENABLED = os.environ.get("SHADOW_ENABLED", "1") not in ("0", "false", "no")


# ── schema ─────────────────────────────────────────────────────────────────
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS quotes(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
        distance REAL, hour INT, eta REAL, fare REAL, cancel_risk REAL,
        instance TEXT)""",
    """CREATE TABLE IF NOT EXISTS outcomes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quote_id INTEGER, ts REAL,
        actual_min REAL, actual_fare REAL, completed INT, source TEXT)""",
    """CREATE TABLE IF NOT EXISTS shadow(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
        quote_id INTEGER, model_version TEXT,
        champion_eta REAL, challenger_eta REAL)""",
    "CREATE INDEX IF NOT EXISTS ix_outcomes_quote ON outcomes(quote_id)",
    "CREATE INDEX IF NOT EXISTS ix_shadow_quote ON shadow(quote_id)",
]

# Columns the original quotes table lacked. Without traffic, weather and surge a
# logged row cannot reproduce the prediction, which makes it useless for training
# and useless for debugging a bad quote after the fact.
QUOTE_COLUMNS = [
    ("traffic", "REAL"), ("weather", "REAL"), ("surge", "REAL"),
    ("mode", "TEXT"), ("endpoint", "TEXT"), ("model_version", "TEXT"),
]


def ensure_schema(conn):
    """Create the tables and add any missing columns. Safe to call every boot.

    Also switches the database to WAL. That matters now in a way it did not
    before: the shadow runner writes from a background thread while request
    threads are writing quote rows, and in SQLite's default rollback-journal mode
    a single writer blocks readers outright. Under WAL, readers and one writer
    proceed concurrently, so shadow scoring cannot stall a rider's request on a
    lock. The setting is persistent -- it is stored in the database file, so this
    runs once and stays.
    """
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        # Rather than failing instantly on contention, wait. Five seconds is far
        # longer than any write here takes and turns a rare lost row into a
        # slightly slower one.
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error:
        # A database on a filesystem that cannot do WAL (some network mounts)
        # still works, just with the old locking. Not worth refusing to boot.
        pass
    for stmt in SCHEMA:
        conn.execute(stmt)
    have = {r[1] for r in conn.execute("PRAGMA table_info(quotes)")}
    for name, kind in QUOTE_COLUMNS:
        if name not in have:
            # ALTER TABLE ADD COLUMN is the one schema change SQLite does cheaply
            # and without rewriting the table, so an existing production database
            # upgrades in place on the next boot.
            conn.execute("ALTER TABLE quotes ADD COLUMN %s %s" % (name, kind))


# ── the champion's own predictions, logged ─────────────────────────────────
def log_prediction(conn, *, distance, hour, weather, traffic, surge, eta, fare,
                   cancel_risk=None, instance=None, mode="drive", endpoint=None,
                   model_version="synthetic-v1"):
    """Record one served prediction and return its id.

    The id is handed back to the client so it can later say what actually
    happened. Without that link an outcome is unattributable: you know a trip
    took 23 minutes but not which prediction to score it against.
    """
    cur = conn.execute(
        """INSERT INTO quotes(ts, distance, hour, eta, fare, cancel_risk, instance,
                              traffic, weather, surge, mode, endpoint, model_version)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (time.time(), distance, hour, eta, fare, cancel_risk, instance,
         traffic, weather, surge, mode, endpoint, model_version))
    return cur.lastrowid


def log_outcome(conn, quote_id, actual_min, actual_fare=None, completed=True,
                source="app"):
    """Record what actually happened. The only training signal that is not synthetic."""
    row = conn.execute("SELECT id FROM quotes WHERE id=?", (quote_id,)).fetchone()
    if row is None:
        return None
    cur = conn.execute(
        """INSERT INTO outcomes(quote_id, ts, actual_min, actual_fare, completed, source)
           VALUES(?,?,?,?,?,?)""",
        (quote_id, time.time(), actual_min, actual_fare, 1 if completed else 0, source))
    return cur.lastrowid


def training_rows(conn, min_actual=0.5, max_actual=600.0):
    """Every (features, actual duration) pair we have. The training set, such as it is.

    Only completed trips, and only durations inside a sane band -- a trip logged
    as four seconds or eleven hours is a client bug or a phone that slept, and
    a handful of those would drag a regression badly.
    """
    q = """SELECT q.id, q.distance, q.traffic, q.weather, q.hour, q.eta,
                  o.actual_min
             FROM quotes q JOIN outcomes o ON o.quote_id = q.id
            WHERE o.completed = 1
              AND o.actual_min BETWEEN ? AND ?
              AND q.distance IS NOT NULL AND q.traffic IS NOT NULL
              AND q.weather IS NOT NULL AND q.hour IS NOT NULL"""
    return [dict(r) for r in conn.execute(q, (min_actual, max_actual))]


def data_status(conn):
    """How much real data exists, and whether it is enough to train on."""
    n_q = conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
    n_o = conn.execute("SELECT COUNT(*) FROM outcomes WHERE completed=1").fetchone()[0]
    usable = len(training_rows(conn))
    return {"predictions_logged": n_q, "outcomes_recorded": n_o,
            "usable_for_training": usable,
            "needed": MIN_TRAIN_SAMPLES,
            "ready_to_train": usable >= MIN_TRAIN_SAMPLES}


# ── the challenger ─────────────────────────────────────────────────────────
class Challenger:
    """A candidate ETA model loaded from disk, scored but never served.

    Absent by design on a fresh deployment: there is nothing to train one on
    until outcomes exist. `loaded` being False is the normal state, not an error.
    """

    def __init__(self, path=CHALLENGER_PATH):
        self.path = path
        self.model = None
        self.meta = {}
        self.loaded = False
        self.error = None
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            self.error = "no challenger at %s" % os.path.basename(self.path)
            return
        try:
            import joblib
            bundle = joblib.load(self.path)
            self.model = bundle["model"]
            self.meta = bundle.get("meta", {})
            self.loaded = True
        except Exception as e:
            # A broken challenger must never stop the API booting. It is, by
            # definition, the model we are not serving.
            self.error = "%s: %s" % (type(e).__name__, e)

    def predict_eta(self, distance, traffic, weather, hour):
        if not self.loaded:
            return None
        try:
            return float(self.model.predict([[distance, traffic, weather, hour]])[0])
        except Exception:
            return None

    def version(self):
        return self.meta.get("version", "challenger") if self.loaded else None


# ── shadow scoring, off the request path ───────────────────────────────────
class ShadowRunner:
    """Scores the challenger on live traffic in a background thread.

    Everything about this is arranged so it cannot affect a rider. The request
    thread does one non-blocking put on a bounded queue and moves on; a daemon
    thread does the prediction and the write. If the queue is full the row is
    dropped and counted. If the challenger throws, the row is dropped and
    counted. A shadow deployment that can degrade the thing it is shadowing is
    not a shadow deployment.
    """

    def __init__(self, challenger, db_factory, enabled=SHADOW_ENABLED):
        self.challenger = challenger
        self._db = db_factory
        self.enabled = enabled and challenger.loaded
        self._q = queue.Queue(maxsize=SHADOW_QUEUE_MAX)
        self.stats = {"scored": 0, "dropped_full": 0, "errors": 0}
        self._thread = None
        if self.enabled:
            self._thread = threading.Thread(target=self._run, name="shadow",
                                            daemon=True)
            self._thread.start()

    def submit(self, quote_id, distance, traffic, weather, hour, champion_eta):
        if not self.enabled:
            return
        try:
            self._q.put_nowait((quote_id, distance, traffic, weather, hour,
                                champion_eta))
        except queue.Full:
            self.stats["dropped_full"] += 1

    def _run(self):
        while True:
            item = self._q.get()
            quote_id, distance, traffic, weather, hour, champ = item
            try:
                pred = self.challenger.predict_eta(distance, traffic, weather, hour)
                if pred is None:
                    self.stats["errors"] += 1
                    continue
                with self._db() as conn:
                    conn.execute(
                        """INSERT INTO shadow(ts, quote_id, model_version,
                                              champion_eta, challenger_eta)
                           VALUES(?,?,?,?,?)""",
                        (time.time(), quote_id, self.challenger.version(),
                         champ, round(pred, 2)))
                self.stats["scored"] += 1
            except Exception:
                self.stats["errors"] += 1

    def state(self):
        return {"enabled": self.enabled, "queued": self._q.qsize(),
                "challenger": self.challenger.version(),
                "challenger_error": self.challenger.error, **self.stats}


# ── reporting ──────────────────────────────────────────────────────────────
def shadow_report(conn, limit=5000):
    """How the challenger compares to the champion.

    Two sections, and the difference between them is the whole point.

    `disagreement` needs no outcomes: it says how far apart the two models are on
    real traffic. Useful immediately, and it answers "is the challenger sane"
    before anyone considers promoting it.

    `accuracy` needs outcomes and is the only section that can say which model is
    BETTER. Until trips are being reported it is empty, and reporting a winner
    from disagreement alone would be exactly the kind of unfounded claim this
    module exists to stop.
    """
    rows = [dict(r) for r in conn.execute(
        """SELECT s.champion_eta, s.challenger_eta, s.model_version, o.actual_min
             FROM shadow s LEFT JOIN outcomes o
               ON o.quote_id = s.quote_id AND o.completed = 1
            ORDER BY s.id DESC LIMIT ?""", (limit,))]
    if not rows:
        return {"samples": 0, "disagreement": None, "accuracy": None,
                "note": "no shadow rows yet"}

    diffs = [abs(r["champion_eta"] - r["challenger_eta"]) for r in rows
             if r["champion_eta"] is not None and r["challenger_eta"] is not None]
    disagreement = None
    if diffs:
        diffs_sorted = sorted(diffs)
        disagreement = {
            "n": len(diffs),
            "mean_abs_min": round(sum(diffs) / len(diffs), 2),
            "median_abs_min": round(diffs_sorted[len(diffs_sorted) // 2], 2),
            "p95_abs_min": round(diffs_sorted[int(len(diffs_sorted) * 0.95)], 2),
            "max_abs_min": round(max(diffs), 2),
        }

    scored = [r for r in rows if r["actual_min"] is not None]
    accuracy = None
    if scored:
        champ = sum(abs(r["champion_eta"] - r["actual_min"]) for r in scored) / len(scored)
        chal = sum(abs(r["challenger_eta"] - r["actual_min"]) for r in scored) / len(scored)
        accuracy = {
            "n": len(scored),
            "champion_mae_min": round(champ, 2),
            "challenger_mae_min": round(chal, 2),
            "challenger_better_by_min": round(champ - chal, 2),
            "verdict": ("challenger wins" if chal < champ * 0.95 else
                        "champion wins" if champ < chal * 0.95 else
                        "too close to call"),
        }

    return {"samples": len(rows), "model_version": rows[0]["model_version"],
            "disagreement": disagreement, "accuracy": accuracy,
            "note": None if scored else
                    "no trip outcomes reported yet -- disagreement only, "
                    "which cannot say which model is better"}
