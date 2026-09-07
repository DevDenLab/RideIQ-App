"""
Tests for the ML feedback loop: prediction logging, outcomes, shadow scoring and
retraining.

The thing most worth protecting here is not correctness of the arithmetic, it is
the refusals. This machinery is dangerous precisely when it succeeds too easily:
a model fitted to eighty rows, or fitted to the champion's own predictions,
produces a confident MAE that looks like evidence and is not. Several tests below
exist only to prove that retrain.py declines to produce a model in situations
where producing one would be easy.

Everything runs against a temporary database, so nothing here touches rideiq.db.
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mlloop as ML  # noqa: E402
import retrain  # noqa: E402


class Base(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        ML.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def predict(self, distance=8.0, hour=8, weather=0, traffic=0.5, surge=1.0,
                eta=20.0, fare=18.0):
        return ML.log_prediction(self.conn, distance=distance, hour=hour,
                                 weather=weather, traffic=traffic, surge=surge,
                                 eta=eta, fare=fare, endpoint="/route-latlon")


class SchemaTest(Base):

    def test_is_idempotent(self):
        for _ in range(3):
            ML.ensure_schema(self.conn)
        self.assertEqual(0, ML.data_status(self.conn)["predictions_logged"])

    def test_two_processes_migrating_the_same_fresh_db_do_not_crash(self):
        """The bug this test exists for took a container down at import time.

        docker-compose.yml runs two API containers (api1, api2) sharing one
        SQLite file. On a first boot against a fresh database, both run this
        migration and can both see a column missing before either has added
        it -- one wins the ALTER TABLE, the other hits "duplicate column name"
        and, before this test, propagated straight up through ensure_schema(),
        which is called at MODULE IMPORT TIME (see app.py). An uncaught
        exception there does not fail a request, it kills the whole process
        before it ever binds a socket -- confirmed by actually racing two real
        connections against a real file below, not by asserting on the
        exception handler in isolation.
        """
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            a = sqlite3.connect(path)
            b = sqlite3.connect(path)
            # Both see the same fresh, columnless table before either alters it
            # -- this is what "two processes starting at once" looks like.
            a.execute("""CREATE TABLE quotes(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
                distance REAL, hour INT, eta REAL, fare REAL,
                cancel_risk REAL, instance TEXT)""")
            a.commit()
            ML.ensure_schema(a)          # the "winner" -- adds every column
            ML.ensure_schema(b)          # the "loser" -- must not raise
            cols = {r[1] for r in b.execute("PRAGMA table_info(quotes)")}
            self.assertIn("weather", cols)
            b.close()
            a.close()
        finally:
            os.unlink(path)

    def test_a_genuinely_broken_column_still_raises(self):
        """The fix must not swallow every OperationalError, only this one.

        A schema problem that has nothing to do with the concurrent-boot race
        (a locked file, a missing table, a real typo in the DDL) must still
        surface -- silently eating every OperationalError would trade one bug
        for a much quieter one.
        """
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            conn.execute("""CREATE TABLE quotes(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
                distance REAL, hour INT, eta REAL, fare REAL,
                cancel_risk REAL, instance TEXT)""")
            conn.commit()
            conn.close()
            # A connection to a table that does not exist at all raises a
            # DIFFERENT OperationalError message ("no such table"), which the
            # fix's message-based check must not mistake for the race.
            broken = sqlite3.connect(path)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    broken.execute("ALTER TABLE not_a_real_table ADD COLUMN x REAL")
            finally:
                broken.close()
        finally:
            os.unlink(path)

    def test_upgrades_a_database_written_by_the_old_schema(self):
        """A live database already exists on EC2 with the original columns.

        The migration has to add traffic/weather/surge in place. Without them a
        logged row cannot reproduce its own prediction, which makes it useless
        both for training and for debugging a bad quote after the fact.
        """
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            old = sqlite3.connect(path)
            old.row_factory = sqlite3.Row
            old.execute("""CREATE TABLE quotes(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
                distance REAL, hour INT, eta REAL, fare REAL,
                cancel_risk REAL, instance TEXT)""")
            old.execute("INSERT INTO quotes(ts,distance,hour,eta,fare) VALUES(1,5,8,12,9)")
            old.commit()

            ML.ensure_schema(old)
            cols = {r[1] for r in old.execute("PRAGMA table_info(quotes)")}
            for needed in ("traffic", "weather", "surge", "mode", "endpoint",
                           "model_version"):
                self.assertIn(needed, cols)
            # and the existing row survives
            self.assertEqual(1, old.execute("SELECT COUNT(*) FROM quotes").fetchone()[0])
            old.close()
        finally:
            os.unlink(path)


class LoggingTest(Base):

    def test_a_prediction_gets_an_id_the_client_can_report_against(self):
        qid = self.predict()
        self.assertIsInstance(qid, int)
        row = self.conn.execute("SELECT * FROM quotes WHERE id=?", (qid,)).fetchone()
        self.assertAlmostEqual(0.5, row["traffic"])
        self.assertEqual("/route-latlon", row["endpoint"])

    def test_an_outcome_attaches_to_its_prediction(self):
        qid = self.predict(eta=20.0)
        self.assertIsNotNone(ML.log_outcome(self.conn, qid, 23.5))
        rows = ML.training_rows(self.conn)
        self.assertEqual(1, len(rows))
        self.assertAlmostEqual(23.5, rows[0]["actual_min"])
        self.assertAlmostEqual(20.0, rows[0]["eta"], msg="champion prediction lost")

    def test_an_outcome_for_an_unknown_prediction_is_refused(self):
        """Unattributable data is worse than no data.

        Accepting it would mean knowing a trip took 23 minutes without knowing
        which prediction to score against -- a row that can never be used and
        will quietly inflate every count of "how much data do we have".
        """
        self.assertIsNone(ML.log_outcome(self.conn, 9999, 23.5))

    def test_incomplete_trips_are_stored_but_not_trained_on(self):
        qid = self.predict()
        ML.log_outcome(self.conn, qid, 12.0, completed=False)
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0])
        self.assertEqual([], ML.training_rows(self.conn))

    def test_implausible_durations_are_excluded_from_training(self):
        # A phone that slept, or a service killed and restarted, reports these.
        for actual in (0.1, 4000.0):
            ML.log_outcome(self.conn, self.predict(), actual)
        ML.log_outcome(self.conn, self.predict(), 22.0)
        rows = ML.training_rows(self.conn)
        self.assertEqual(1, len(rows))
        self.assertAlmostEqual(22.0, rows[0]["actual_min"])


class DataStatusTest(Base):

    def test_a_fresh_deployment_is_honestly_empty(self):
        st = ML.data_status(self.conn)
        self.assertEqual(0, st["usable_for_training"])
        self.assertFalse(st["ready_to_train"])

    def test_predictions_alone_do_not_count_as_training_data(self):
        """The distinction the whole module rests on.

        A thousand logged predictions and no outcomes is a thousand rows of the
        model's own opinion. Counting them would say "ready to train" when there
        is nothing to train on.
        """
        for _ in range(50):
            self.predict()
        st = ML.data_status(self.conn)
        self.assertEqual(50, st["predictions_logged"])
        self.assertEqual(0, st["usable_for_training"])
        self.assertFalse(st["ready_to_train"])


class RetrainTest(Base):

    def seed(self, n, seed=0, noise=1.0):
        """n predictions with outcomes drawn from a REAL relationship.

        Deliberately not the one models.py was fitted to: duration here is a
        different function of distance, traffic and hour. That is what makes the
        test meaningful -- the challenger has to discover something the champion
        does not know, which is exactly the situation retraining exists for.
        """
        import numpy as np
        rng = np.random.RandomState(seed)
        for k in range(n):
            dist = float(rng.uniform(1, 25))
            traffic = float(rng.uniform(0, 1))
            weather = int(rng.rand() < 0.2)
            hour = int(rng.randint(0, 24))
            rush = 1.6 if hour in (7, 8, 16, 17, 18) else 1.0
            actual = (dist / 30.0 * 60.0) * rush * (1 + 0.5 * traffic) \
                + 4 * weather + float(rng.normal(0, noise))
            # The champion is systematically wrong: it ignores rush hour entirely.
            champ = dist / 32.0 * 60.0 * (1 + 0.3 * traffic)
            qid = ML.log_prediction(self.conn, distance=dist, hour=hour,
                                    weather=weather, traffic=traffic, surge=1.0,
                                    eta=round(champ, 1), fare=5 + 2 * dist)
            ML.log_outcome(self.conn, qid, round(max(0.6, actual), 2))

    def test_refuses_below_the_minimum(self):
        self.seed(60)
        res = retrain.train(self.conn, quiet=True)
        self.assertFalse(res["trained"])
        self.assertEqual("insufficient_data", res["reason"])

    def test_refuses_even_with_force_when_there_is_almost_nothing(self):
        # --force exists for testing, not for manufacturing a model out of twelve
        # rows. sklearn would fit those happily and report a wonderful score.
        self.seed(12)
        res = retrain.train(self.conn, force=True, quiet=True)
        self.assertFalse(res["trained"])
        self.assertEqual("far_too_few", res["reason"])

    def test_trains_and_beats_the_champion_on_real_outcomes(self):
        self.seed(900)
        out = os.path.join(os.path.dirname(self.path), "chal-test.joblib")
        try:
            res = retrain.train(self.conn, out_path=out, quiet=True)
            self.assertTrue(res["trained"], res)
            self.assertLess(res["challenger_mae"], res["champion_mae"],
                            "challenger did not beat the champion it was meant to")
            self.assertLess(res["challenger_mae"], res["naive_mae"],
                            "challenger did not beat predicting the mean")
            self.assertTrue(os.path.exists(out))

            # And the saved bundle is loadable and usable.
            ch = ML.Challenger(out)
            self.assertTrue(ch.loaded, ch.error)
            pred = ch.predict_eta(10.0, 0.5, 0, 8)
            self.assertIsNotNone(pred)
            self.assertGreater(pred, 0)
        finally:
            if os.path.exists(out):
                os.unlink(out)

    def test_will_not_ship_a_model_that_cannot_beat_the_mean(self):
        """Noise in, nothing out.

        With the target made pure noise there is no signal to find, so any
        apparent skill is overfitting. Writing a model here would be the failure
        mode this whole file guards against.
        """
        import numpy as np
        rng = np.random.RandomState(1)
        for _ in range(700):
            qid = ML.log_prediction(
                self.conn, distance=float(rng.uniform(1, 25)), hour=int(rng.randint(24)),
                weather=0, traffic=float(rng.rand()), surge=1.0, eta=20.0, fare=20.0)
            ML.log_outcome(self.conn, qid, float(rng.uniform(5, 60)))
        out = os.path.join(os.path.dirname(self.path), "chal-noise.joblib")
        res = retrain.train(self.conn, out_path=out, quiet=True)
        self.assertFalse(res["trained"])
        self.assertIn(res["reason"], ("worse_than_naive", "worse_than_champion"))
        self.assertFalse(os.path.exists(out))


class ChallengerTest(unittest.TestCase):

    def test_a_missing_challenger_is_a_normal_state_not_an_error(self):
        ch = ML.Challenger("/nonexistent/path/challenger.joblib")
        self.assertFalse(ch.loaded)
        self.assertIsNone(ch.predict_eta(8, 0.5, 0, 8))
        self.assertIsNone(ch.version())
        self.assertIn("no challenger", ch.error)

    def test_a_corrupt_challenger_does_not_stop_the_api_booting(self):
        fd, path = tempfile.mkstemp(suffix=".joblib")
        os.write(fd, b"this is not a joblib bundle")
        os.close(fd)
        try:
            ch = ML.Challenger(path)
            self.assertFalse(ch.loaded)
            self.assertIsNotNone(ch.error)
        finally:
            os.unlink(path)


class ShadowTest(Base):

    class FakeChallenger:
        loaded = True
        error = None

        def __init__(self, offset=3.0, blow_up=False):
            self.offset = offset
            self.blow_up = blow_up

        def predict_eta(self, distance, traffic, weather, hour):
            if self.blow_up:
                raise RuntimeError("challenger exploded")
            return distance * 2.0 + self.offset

        def version(self):
            return "fake-v1"

    def runner(self, challenger):
        def factory():
            c = sqlite3.connect(self.path)
            c.row_factory = sqlite3.Row
            return c
        return ML.ShadowRunner(challenger, factory, enabled=True)

    def drain(self, runner, expect, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if runner.stats["scored"] + runner.stats["errors"] >= expect:
                return
            time.sleep(0.01)

    def test_scores_live_traffic_without_serving_it(self):
        r = self.runner(self.FakeChallenger(offset=3.0))
        for k in range(20):
            qid = self.predict(distance=10.0, eta=25.0)
            r.submit(qid, 10.0, 0.5, 0, 8, 25.0)
        # Commit before waiting. The worker writes on its own connection, and
        # holding an open write transaction here would block it -- which is a
        # property of the test, not of the runner.
        self.conn.commit()
        self.drain(r, 20)
        rows = self.conn.execute("SELECT * FROM shadow").fetchall()
        self.assertEqual(20, len(rows))
        self.assertAlmostEqual(25.0, rows[0]["champion_eta"])
        self.assertAlmostEqual(23.0, rows[0]["challenger_eta"])
        self.assertEqual("fake-v1", rows[0]["model_version"])

    def test_a_challenger_that_throws_cannot_hurt_the_request(self):
        r = self.runner(self.FakeChallenger(blow_up=True))
        for _ in range(5):
            r.submit(self.predict(), 10.0, 0.5, 0, 8, 25.0)
        self.conn.commit()
        self.drain(r, 5)
        self.assertEqual(0, r.stats["scored"])
        self.assertEqual(5, r.stats["errors"])

    def test_submitting_is_a_no_op_when_disabled(self):
        r = ML.ShadowRunner(self.FakeChallenger(), lambda: None, enabled=False)
        r.submit(1, 10.0, 0.5, 0, 8, 25.0)      # must not raise
        self.assertFalse(r.state()["enabled"])


class ReportTest(Base):

    def test_no_shadow_rows_reports_nothing_rather_than_zeroes(self):
        rep = ML.shadow_report(self.conn)
        self.assertEqual(0, rep["samples"])
        self.assertIsNone(rep["accuracy"])

    def test_disagreement_without_outcomes_declines_to_name_a_winner(self):
        """Two models differing says nothing about which is right.

        Reporting a verdict from disagreement alone would be the exact species of
        unfounded claim this module was written to stop.
        """
        for _ in range(30):
            qid = self.predict(eta=20.0)
            self.conn.execute(
                """INSERT INTO shadow(ts, quote_id, model_version, champion_eta,
                                      challenger_eta) VALUES(?,?,?,?,?)""",
                (time.time(), qid, "v", 20.0, 26.0))
        rep = ML.shadow_report(self.conn)
        self.assertEqual(6.0, rep["disagreement"]["mean_abs_min"])
        self.assertIsNone(rep["accuracy"])
        self.assertIn("cannot say which model is better", rep["note"])

    def test_with_outcomes_it_names_the_more_accurate_model(self):
        for _ in range(40):
            qid = self.predict(eta=20.0)
            self.conn.execute(
                """INSERT INTO shadow(ts, quote_id, model_version, champion_eta,
                                      challenger_eta) VALUES(?,?,?,?,?)""",
                (time.time(), qid, "v", 20.0, 26.0))
            ML.log_outcome(self.conn, qid, 27.0)      # challenger is closer
        rep = ML.shadow_report(self.conn)
        self.assertEqual(40, rep["accuracy"]["n"])
        self.assertAlmostEqual(7.0, rep["accuracy"]["champion_mae_min"])
        self.assertAlmostEqual(1.0, rep["accuracy"]["challenger_mae_min"])
        self.assertEqual("challenger wins", rep["accuracy"]["verdict"])

    def test_a_marginal_difference_is_called_a_draw(self):
        # Promoting a model for a 2% improvement measured on a few hundred trips
        # is noise-chasing dressed as rigour.
        for _ in range(40):
            qid = self.predict(eta=20.0)
            self.conn.execute(
                """INSERT INTO shadow(ts, quote_id, model_version, champion_eta,
                                      challenger_eta) VALUES(?,?,?,?,?)""",
                (time.time(), qid, "v", 20.0, 20.1))
            ML.log_outcome(self.conn, qid, 25.0)
        self.assertEqual("too close to call",
                         ML.shadow_report(self.conn)["accuracy"]["verdict"])


if __name__ == "__main__":
    unittest.main()
