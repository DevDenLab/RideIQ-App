"""
Tests for the circuit breaker and bulkhead around OpenTripPlanner.

Resilience code has an unpleasant property: when it is broken, everything looks
fine right up until the incident it was supposed to prevent. There is no gentle
signal. So these tests drive the state machine into the corners on purpose --
especially the ones that deadlock rather than crash, because a stuck-half-open
breaker refuses every request forever and nothing about it looks like a failure
from the outside.

The concurrency test at the end is the one that matters most: it asserts the
actual property the bulkhead exists for, which is that the number of callers
inside the guarded call never exceeds the limit no matter how hard it is pushed.
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import resilience as RES  # noqa: E402


class Unreachable(RuntimeError):
    """Stands in for TransitUnavailable: the dependency's fault."""


class BadQuery(RuntimeError):
    """Stands in for OTP rejecting our GraphQL: our fault."""


def guard(limit=4, fail_threshold=3, reset_after=0.05):
    return RES.Guard("test-dep", limit=limit, failure_types=(Unreachable,),
                     fail_threshold=fail_threshold, reset_after=reset_after)


def fail(g, exc=Unreachable):
    """Run one guarded call that raises, swallowing the exception."""
    try:
        with g:
            raise exc("boom")
    except (Unreachable, BadQuery):
        pass


def succeed(g):
    with g:
        pass


class BreakerTest(unittest.TestCase):

    def test_stays_closed_while_calls_succeed(self):
        g = guard()
        for _ in range(20):
            succeed(g)
        self.assertEqual("closed", g.breaker.state()["state"])

    def test_opens_after_the_threshold_of_consecutive_failures(self):
        g = guard(fail_threshold=3)
        fail(g); fail(g)
        self.assertEqual("closed", g.breaker.state()["state"],
                         "must not open before the threshold")
        fail(g)
        self.assertEqual("open", g.breaker.state()["state"])

    def test_a_success_resets_the_failure_run(self):
        # Consecutive is the whole point. Three failures spread over an hour of
        # healthy traffic is not an outage, and treating it as one would open the
        # circuit on a service that is working.
        g = guard(fail_threshold=3)
        fail(g); fail(g)
        succeed(g)
        fail(g); fail(g)
        self.assertEqual("closed", g.breaker.state()["state"])

    def test_an_open_circuit_refuses_without_calling_through(self):
        g = guard(fail_threshold=2, reset_after=60)
        fail(g); fail(g)
        called = []
        with self.assertRaises(RES.CircuitOpen):
            with g:
                called.append(1)
        self.assertEqual([], called, "the body ran despite an open circuit")

    def test_refusal_is_immediate(self):
        g = guard(fail_threshold=1, reset_after=60)
        fail(g)
        t0 = time.perf_counter()
        for _ in range(500):
            try:
                with g:
                    pass
            except RES.CircuitOpen:
                pass
        # The point of the breaker is that these cost microseconds instead of a
        # 20-second timeout each. Loose bound; the real one is 6 orders down.
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_after_cooldown_exactly_one_probe_gets_through(self):
        g = guard(fail_threshold=1, reset_after=0.05)
        fail(g)
        time.sleep(0.08)

        entered = []
        # First caller becomes the probe and is allowed in...
        started = threading.Event()
        release = threading.Event()

        def probe():
            with g:
                entered.append("probe")
                started.set()
                release.wait(2)

        t = threading.Thread(target=probe)
        t.start()
        self.assertTrue(started.wait(2), "probe never entered")

        # ...and while it is in flight, everyone else is still refused.
        with self.assertRaises(RES.CircuitOpen):
            with g:
                entered.append("second")

        release.set()
        t.join(2)
        self.assertEqual(["probe"], entered)

    def test_a_successful_probe_closes_the_circuit(self):
        g = guard(fail_threshold=1, reset_after=0.05)
        fail(g)
        time.sleep(0.08)
        succeed(g)
        self.assertEqual("closed", g.breaker.state()["state"])
        succeed(g)      # and normal traffic flows again

    def test_a_failed_probe_reopens_and_backs_off(self):
        g = guard(fail_threshold=1, reset_after=0.05)
        fail(g)
        # Read the raw cooldown, not state()'s rounded display copy: these test
        # timings are sub-second and rounding would hide the whole effect.
        first = g.breaker._cooldown
        time.sleep(0.08)
        fail(g)                                   # the probe fails
        second = g.breaker._cooldown
        self.assertEqual("open", g.breaker.state()["state"])
        self.assertGreater(second, first,
                           "a dependency that is still down must be probed less often")

    def test_backoff_is_capped(self):
        # Doubling without a ceiling would eventually stop probing altogether, so
        # a dependency that came back would never be noticed.
        g = RES.Guard("dep", limit=2, failure_types=(Unreachable,),
                      fail_threshold=1, reset_after=0.01)
        g.breaker.max_reset = 0.05
        for _ in range(12):
            # Wait out whatever the cooldown has grown to; a fixed sleep would
            # stop being long enough after a couple of doublings and every later
            # call would bounce off the open circuit instead of probing it.
            time.sleep(g.breaker._cooldown + 0.005)
            fail(g)
        self.assertLessEqual(g.breaker._cooldown, 0.05)
        self.assertGreaterEqual(g.breaker._cooldown, 0.04, "backoff never grew")

    def test_a_success_clears_the_backoff(self):
        g = guard(fail_threshold=1, reset_after=0.02)
        fail(g); time.sleep(0.03); fail(g)        # cooldown now doubled
        self.assertGreater(g.breaker._cooldown, 0.02)
        time.sleep(0.06)
        succeed(g)
        self.assertEqual(0.02, g.breaker._cooldown)

    # ── whose fault was it ─────────────────────────────────────────────────
    def test_our_own_bad_request_never_opens_the_circuit(self):
        """A malformed query fails identically on every retry.

        If it counted, one bad request could disable transit for every user --
        the opposite of what a breaker is for.
        """
        g = guard(fail_threshold=2)
        for _ in range(10):
            fail(g, BadQuery)
        self.assertEqual("closed", g.breaker.state()["state"])

    def test_our_own_bad_request_does_not_heal_a_failing_circuit(self):
        # The mirror of the above, and the easier one to get wrong: treating a
        # client error as "the call went fine" would let a stream of bad requests
        # keep resetting the failure count and stop the breaker ever tripping.
        g = guard(fail_threshold=3)
        fail(g); fail(g)
        fail(g, BadQuery)
        fail(g)
        self.assertEqual("open", g.breaker.state()["state"],
                         "a client error reset the failure run")


class BulkheadTest(unittest.TestCase):

    def test_rejects_past_the_limit_instead_of_queueing(self):
        g = guard(limit=2)
        release = threading.Event()
        inside = threading.Semaphore(0)

        def hold():
            try:
                with g:
                    inside.release()
                    release.wait(2)
            except RES.Overloaded:
                pass

        threads = [threading.Thread(target=hold) for _ in range(2)]
        for t in threads:
            t.start()
        for _ in range(2):
            self.assertTrue(inside.acquire(timeout=2))

        t0 = time.perf_counter()
        with self.assertRaises(RES.Overloaded):
            with g:
                pass
        # Rejected, not queued: it must come back immediately, not when a slot
        # frees. That distinction is the whole feature.
        self.assertLess(time.perf_counter() - t0, 0.5)

        release.set()
        for t in threads:
            t.join(2)
        succeed(g)                       # slots were returned

    def test_slots_are_returned_even_when_the_body_raises(self):
        # High threshold: this is a bulkhead test, and letting the breaker trip
        # partway through would be testing something else.
        g = guard(limit=1, fail_threshold=10_000)
        for _ in range(5):
            fail(g)
        self.assertEqual(0, g.bulkhead.state()["in_flight"])
        succeed(g)

    def test_shedding_load_is_not_the_dependency_failing(self):
        """A traffic spike must not open the circuit.

        Rejecting for capacity says nothing about OTP's health. Counting it as a
        failure would turn a busy minute into a self-inflicted outage.
        """
        g = guard(limit=1, fail_threshold=2)
        release = threading.Event()
        entered = threading.Event()

        def hold():
            with g:
                entered.set()
                release.wait(2)

        t = threading.Thread(target=hold)
        t.start()
        self.assertTrue(entered.wait(2))
        for _ in range(10):
            try:
                with g:
                    pass
            except RES.Overloaded:
                pass
        release.set()
        t.join(2)
        self.assertEqual("closed", g.breaker.state()["state"])

    def test_a_shed_probe_does_not_deadlock_the_circuit(self):
        """The bug this test exists for is invisible from outside.

        A caller can claim the half-open probe and then be rejected by the
        bulkhead. If the probe flag is not handed back, the circuit sits in
        half_open refusing everyone forever, waiting for a probe that already
        went away. Nothing crashes; transit is simply down until a restart.
        """
        g = guard(limit=1, fail_threshold=1, reset_after=0.05)
        fail(g)
        time.sleep(0.08)

        # Occupy the single bulkhead slot with a call the breaker has already
        # let through, so the next caller claims the probe and is then shed.
        release = threading.Event()
        entered = threading.Event()

        def hold():
            with g:
                entered.set()
                release.wait(2)

        t = threading.Thread(target=hold)
        t.start()
        self.assertTrue(entered.wait(2))

        with self.assertRaises((RES.Overloaded, RES.CircuitOpen)):
            with g:
                pass

        release.set()
        t.join(2)
        # The circuit must still be usable.
        succeed(g)
        self.assertEqual("closed", g.breaker.state()["state"])


class ConcurrencyTest(unittest.TestCase):

    def test_the_limit_actually_holds_under_load(self):
        """The property the bulkhead exists for, asserted directly.

        Everything else in this file tests the state machine. This tests the
        thing that keeps FastAPI's threadpool alive: however many threads pile
        in, only `limit` of them are ever inside the guarded call at once.
        """
        limit = 5
        g = guard(limit=limit, fail_threshold=10_000)
        inside = [0]
        peak = [0]
        lock = threading.Lock()
        shed = [0]

        def worker():
            for _ in range(60):
                try:
                    with g:
                        with lock:
                            inside[0] += 1
                            peak[0] = max(peak[0], inside[0])
                        time.sleep(0.001)
                        with lock:
                            inside[0] -= 1
                except RES.Overloaded:
                    with lock:
                        shed[0] += 1

        threads = [threading.Thread(target=worker) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)

        self.assertLessEqual(peak[0], limit,
                             "%d callers were inside a limit of %d" % (peak[0], limit))
        self.assertGreater(shed[0], 0, "40 threads never once hit the limit")
        self.assertEqual(0, g.bulkhead.state()["in_flight"], "leaked a slot")
        self.assertEqual(peak[0], g.bulkhead.state()["peak_in_flight"])


if __name__ == "__main__":
    unittest.main()
