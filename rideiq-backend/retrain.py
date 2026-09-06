"""
retrain.py -- fit a challenger ETA model on logged trip outcomes.

    python retrain.py              # train if there is enough real data
    python retrain.py --status     # just say how much data exists
    python retrain.py --force      # train anyway, below the minimum (for testing)

What it will not do
-------------------
It will not train on predictions. The quotes table stores what the model said the
ETA would be, and a model fitted to that is a copy of the current model with new
error bars -- it would score beautifully against its own teacher and no better
than today against reality. Only rows joined to a real outcome are used.

It will not train on too little. Below MIN_TRAIN_SAMPLES it prints what is
missing and exits without writing a model. A regression fitted to eighty trips
would produce a confident-looking MAE that means nothing, and an empirical-
looking number is more dangerous than an obviously synthetic one.

It will not promote anything. Success writes challenger.joblib, which the API
scores in shadow and never serves. Promotion is a human decision made after
reading /shadow-report, and deliberately not automated -- a model that wins on
last week's traffic can still be the wrong thing to put in front of riders.

Honest accounting of the comparison
-----------------------------------
The champion's predictions are already logged, so both models can be scored on
the SAME held-out trips: the challenger by predicting them, the champion by
reading back what it actually said at the time. That is a fair fight and does not
require re-running the synthetic model.
"""
import argparse
import os
import sqlite3
import sys
import time

import numpy as np

import mlloop

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "rideiq.db"))


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _mae(a, b):
    return float(np.mean(np.abs(np.asarray(a) - np.asarray(b))))


def train(conn, force=False, test_fraction=0.25, out_path=mlloop.CHALLENGER_PATH,
          seed=42, quiet=False):
    """Fit and evaluate a challenger. Returns a result dict; writes only on success."""
    def say(*a):
        if not quiet:
            print(*a)

    rows = mlloop.training_rows(conn)
    status = mlloop.data_status(conn)
    say("Logged predictions   : %d" % status["predictions_logged"])
    say("Trip outcomes        : %d" % status["outcomes_recorded"])
    say("Usable for training  : %d  (need %d)"
        % (status["usable_for_training"], mlloop.MIN_TRAIN_SAMPLES))

    if len(rows) < mlloop.MIN_TRAIN_SAMPLES and not force:
        say("\nNot training. %d more real outcomes needed."
            % (mlloop.MIN_TRAIN_SAMPLES - len(rows)))
        say("Outcomes arrive via POST /trip-outcome when a trip finishes.")
        return {"trained": False, "reason": "insufficient_data",
                "have": len(rows), "need": mlloop.MIN_TRAIN_SAMPLES}

    if len(rows) < 20:
        # Even --force has a floor: a train/test split of twelve rows is not an
        # experiment, and sklearn will happily fit it and report a great score.
        say("\nRefusing even with --force: %d rows cannot be split." % len(rows))
        return {"trained": False, "reason": "far_too_few", "have": len(rows)}

    X = np.array([[r["distance"], r["traffic"], r["weather"], r["hour"]] for r in rows],
                 dtype=float)
    y = np.array([r["actual_min"] for r in rows], dtype=float)
    champion_said = np.array([r["eta"] if r["eta"] is not None else np.nan
                              for r in rows], dtype=float)

    # A time-ordered split, not a random one. Trips arrive in time order and a
    # random split lets the model see the future -- yesterday's rush hour in the
    # training set, this morning's in the test set. It flatters every metric.
    n_test = max(10, int(len(rows) * test_fraction))
    split = len(rows) - n_test
    Xtr, Xte = X[:split], X[split:]
    ytr, yte = y[:split], y[split:]
    champ_te = champion_said[split:]

    from sklearn.ensemble import RandomForestRegressor
    model = RandomForestRegressor(n_estimators=200, min_samples_leaf=2,
                                  random_state=seed, n_jobs=-1)
    t0 = time.perf_counter()
    model.fit(Xtr, ytr)
    fit_s = time.perf_counter() - t0

    chal_mae = _mae(model.predict(Xte), yte)
    have_champ = ~np.isnan(champ_te)
    champ_mae = _mae(champ_te[have_champ], yte[have_champ]) if have_champ.any() else None

    # The dumbest useful baseline. If a random forest on four features cannot beat
    # "assume every trip takes the average", the features are not carrying signal
    # and a fancier model is not the answer.
    naive_mae = _mae(np.full_like(yte, ytr.mean()), yte)

    say("\nTrained on %d, tested on %d (time-ordered split), fit in %.1fs"
        % (len(Xtr), len(Xte), fit_s))
    say("  challenger MAE : %6.2f min" % chal_mae)
    if champ_mae is not None:
        say("  champion MAE   : %6.2f min   (what it actually predicted at the time)"
            % champ_mae)
    say("  naive mean MAE : %6.2f min" % naive_mae)

    beats_champ = champ_mae is None or chal_mae < champ_mae
    beats_naive = chal_mae < naive_mae

    result = {"trained": False, "challenger_mae": round(chal_mae, 3),
              "champion_mae": None if champ_mae is None else round(champ_mae, 3),
              "naive_mae": round(naive_mae, 3), "n_train": len(Xtr),
              "n_test": len(Xte)}

    if not beats_naive:
        say("\nNot writing. The challenger does not beat predicting the mean,")
        say("so it has learned nothing worth shipping.")
        result["reason"] = "worse_than_naive"
        return result
    if not beats_champ:
        say("\nNot writing. The champion is more accurate on this data.")
        result["reason"] = "worse_than_champion"
        return result

    import joblib
    meta = {"version": "outcomes-%s" % time.strftime("%Y%m%d-%H%M"),
            "trained_at": time.time(), "n_train": len(Xtr), "n_test": len(Xte),
            "features": ["distance", "traffic", "weather", "hour"],
            "target": "actual_min",
            "challenger_mae": chal_mae, "champion_mae": champ_mae,
            "naive_mae": naive_mae}
    joblib.dump({"model": model, "meta": meta}, out_path)
    say("\nWrote %s (version %s)" % (os.path.basename(out_path), meta["version"]))
    say("It will be SCORED IN SHADOW on the next API restart, not served.")
    say("Read /shadow-report before promoting anything.")
    result.update(trained=True, path=out_path, version=meta["version"])
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--status", action="store_true", help="report data volume only")
    ap.add_argument("--force", action="store_true",
                    help="train below the minimum sample count (testing only)")
    ap.add_argument("--out", default=mlloop.CHALLENGER_PATH)
    args = ap.parse_args()

    with db() as conn:
        mlloop.ensure_schema(conn)
        if args.status:
            st = mlloop.data_status(conn)
            width = max(len(k) for k in st)
            for k, v in st.items():
                print("%-*s : %s" % (width, k, v))
            return 0
        res = train(conn, force=args.force, out_path=args.out)
    return 0 if res.get("trained") or res.get("reason") == "insufficient_data" else 1


if __name__ == "__main__":
    sys.exit(main())
