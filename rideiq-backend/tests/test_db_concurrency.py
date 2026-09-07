"""
A regression test for one specific, previously-broken guarantee: that a fresh
connection from app.db() can actually wait out a lock instead of failing
instantly.

Found by running a Locust load test with two processes sharing one SQLite
file (the real docker-compose.yml topology) -- /quote failed with
"database is locked" on real, ordinary concurrent traffic. The cause: WAL
mode persists in the database file, but PRAGMA busy_timeout does not -- it is
a per-CONNECTION setting. ensure_schema() set it once, on the one connection
it runs with at boot; db() then handed out a fresh connection per request with
the SQLite default of 0 ms (fail immediately on any lock). Under any real
concurrent write it surfaced as a genuine 500 to whoever's request lost the
race, not the "slightly slower, never lost" write the code claimed.

This does not spin up the API; it tests db() and SQLite's own locking
directly, which is both faster and a more precise test of the actual claim.
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class DbConcurrencyTest(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.environ["DB_PATH"] = self.path
        # app.py imports mlloop, models, routing etc. and trains 16 ML models
        # at import time -- reload is unavoidable but is only paid once per
        # test process, and this file exists specifically to test app.db().
        import importlib
        import app as _app
        importlib.reload(_app)
        self.app = _app

    def tearDown(self):
        del os.environ["DB_PATH"]
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_a_fresh_connection_actually_carries_the_busy_timeout(self):
        conn = self.app.db()
        try:
            timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertGreater(timeout_ms, 0,
                               "a brand new connection from db() has no busy_timeout "
                               "-- it will fail instantly on any lock instead of waiting")
        finally:
            conn.close()

    def test_a_second_writer_waits_instead_of_failing_instantly(self):
        """The property that actually matters, proven directly against SQLite.

        One connection holds a write lock open; a second, independent
        connection from db() tries to write at the same time. Before the fix
        this raised "database is locked" the instant it was attempted -- 0 ms
        of patience. After the fix it waits (up to busy_timeout) and the write
        succeeds once the first connection releases the lock.
        """
        holder = self.app.db()
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO quotes(ts, distance) VALUES (1.0, 5.0)")
        # Deliberately not committed yet -- this holds SQLite's write lock.

        second_result = {}

        def try_write():
            conn = self.app.db()
            try:
                t0 = time.perf_counter()
                conn.execute("INSERT INTO quotes(ts, distance) VALUES (2.0, 6.0)")
                conn.commit()
                second_result["ok"] = True
                second_result["waited_s"] = time.perf_counter() - t0
            except sqlite3.OperationalError as e:
                second_result["ok"] = False
                second_result["error"] = str(e)
            finally:
                conn.close()

        t = threading.Thread(target=try_write)
        t.start()
        time.sleep(0.3)          # let the second writer genuinely start waiting
        holder.commit()          # release the lock
        t.join(timeout=5)

        self.assertTrue(second_result.get("ok"),
                        "second writer failed instead of waiting: %s"
                        % second_result.get("error"))
        self.assertGreater(second_result["waited_s"], 0.2,
                           "the write returned before the lock was even released, "
                           "so it cannot have been the one that waited for it")
        holder.close()


if __name__ == "__main__":
    unittest.main()
