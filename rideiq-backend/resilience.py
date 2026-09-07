"""
resilience.py -- a circuit breaker and a bulkhead, for the one call that can take
the whole API down with it.

The failure mode
----------------
/transit is a synchronous endpoint that makes a blocking call to OpenTripPlanner
with a 20-second timeout. FastAPI runs sync endpoints in a threadpool of 40. So
41 concurrent transit requests against a slow OTP is not "transit is slow" -- it
is every thread in the pool parked on a socket, and /health, /quote and /route
stop answering too. A dependency that is merely degraded takes down endpoints
that do not depend on it at all.

The existing 503 fallback does not cover this. It handles OTP being *absent* --
connection refused, answered immediately. It does nothing about OTP being
*present and slow*, which is both more common and far more damaging.

Two mechanisms, doing different jobs
------------------------------------
A **bulkhead** caps how many requests may be inside the call at once, well below
the threadpool size. Past that it rejects immediately rather than queueing.
Queueing is the wrong instinct here: a rider who waited 20 seconds for a bus plan
has already given up, so serving them slowly costs a thread and helps nobody. The
cap is the load-shedding lever, and it is expressed in concurrency rather than
requests per second because concurrency is what actually exhausts the pool.

A **circuit breaker** notices that the call has been failing and stops making it
at all for a while. Without it, every request keeps paying the full 20-second
timeout to rediscover something we already knew; with it, they fail in
microseconds and the app falls back instantly. The breaker also protects OTP:
hammering a service that is struggling is how a brownout becomes an outage.

The two are complementary, and both are needed. The bulkhead bounds the damage
while OTP is slow-but-working; the breaker ends the damage once it is clearly
broken.

Nothing here is transit-specific -- it guards a callable.
"""
import threading
import time


class CircuitOpen(RuntimeError):
    """The breaker is open: we are not calling the dependency right now."""


class Overloaded(RuntimeError):
    """The bulkhead is full: too many callers already inside."""


class Breaker:
    """Closed -> open after repeated failure -> half-open probe -> closed.

    Thread-safe, because FastAPI's sync endpoints run in a threadpool and every
    one of them shares this object.
    """

    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, name, fail_threshold=5, reset_after=30.0, max_reset=300.0):
        self.name = name
        self.fail_threshold = fail_threshold
        self.reset_after = reset_after
        self.max_reset = max_reset
        self._lock = threading.Lock()
        self._failures = 0
        self._state = self.CLOSED
        self._opened_at = 0.0
        self._cooldown = reset_after
        self._probing = False
        # Counters, for /resilience. Cheap, and the only way to answer "did the
        # breaker actually do anything last night" after the fact.
        self.stats = {"calls": 0, "failures": 0, "rejected": 0, "opened": 0}

    def _allow(self):
        """Claim permission to make one call, or raise."""
        with self._lock:
            self.stats["calls"] += 1
            if self._state == self.CLOSED:
                return
            if self._state == self.OPEN:
                waited = time.monotonic() - self._opened_at
                if waited < self._cooldown:
                    self.stats["rejected"] += 1
                    raise CircuitOpen(
                        "%s is failing; not retrying for another %.0fs"
                        % (self.name, self._cooldown - waited))
                # Cooldown elapsed: let exactly one caller through to find out
                # whether it is back. Everyone else keeps failing fast -- a
                # thundering herd against a service that just came up is how you
                # knock it straight back down.
                self._state = self.HALF_OPEN
                self._probing = True
                return
            # HALF_OPEN
            if self._probing:
                self.stats["rejected"] += 1
                raise CircuitOpen("%s is being probed; try again shortly" % self.name)
            self._probing = True

    def record_success(self):
        with self._lock:
            self._failures = 0
            self._probing = False
            self._cooldown = self.reset_after     # a good call resets the backoff
            self._state = self.CLOSED

    def abandon(self):
        """Release the half-open probe slot without judging the dependency.

        Two cases need this. The bulkhead can reject a caller that has already
        claimed the probe, and the call can fail for a reason that is our fault
        rather than theirs. Either way we learned nothing, so the breaker's state
        must not move -- but the probe flag has to be cleared, or a half-open
        circuit refuses every caller forever waiting for a probe that never runs.
        """
        with self._lock:
            self._probing = False

    def record_failure(self):
        with self._lock:
            self._probing = False
            self._failures += 1
            self.stats["failures"] += 1
            if self._state == self.HALF_OPEN:
                # The probe failed, so it is still down. Back off further rather
                # than probing on the same schedule forever: an OTP that has been
                # dead for an hour should not be poked every 30 seconds.
                self._cooldown = min(self._cooldown * 2, self.max_reset)
                self._state = self.OPEN
                self._opened_at = time.monotonic()
                self.stats["opened"] += 1
            elif self._failures >= self.fail_threshold:
                self._state = self.OPEN
                self._opened_at = time.monotonic()
                self.stats["opened"] += 1

    def state(self):
        with self._lock:
            out = {"state": self._state, "consecutive_failures": self._failures,
                   "cooldown_s": round(self._cooldown, 2), **self.stats}
            if self._state == self.OPEN:
                out["retry_in_s"] = round(
                    max(0.0, self._cooldown - (time.monotonic() - self._opened_at)), 1)
            return out

    def reset(self):
        """Force closed. For tests and for an operator who has fixed the cause."""
        with self._lock:
            self._state = self.CLOSED
            self._failures = 0
            self._probing = False
            self._cooldown = self.reset_after


class Bulkhead:
    """A hard cap on concurrent callers, rejecting rather than queueing."""

    def __init__(self, name, limit):
        self.name = name
        self.limit = limit
        self._sem = threading.BoundedSemaphore(limit)
        self._lock = threading.Lock()
        self._in_flight = 0
        self.stats = {"rejected": 0, "peak_in_flight": 0}

    def acquire(self):
        # blocking=False is the entire design. Waiting for a slot is queueing
        # with extra steps, and the queue is what kills the threadpool.
        if not self._sem.acquire(blocking=False):
            with self._lock:
                self.stats["rejected"] += 1
            raise Overloaded("%s is at its concurrency limit of %d"
                             % (self.name, self.limit))
        with self._lock:
            self._in_flight += 1
            if self._in_flight > self.stats["peak_in_flight"]:
                self.stats["peak_in_flight"] = self._in_flight

    def release(self):
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
        try:
            self._sem.release()
        except ValueError:
            # BoundedSemaphore raises on an unmatched release. That means a bug
            # in the guard, not in the caller, and losing a permit permanently
            # would slowly strangle the endpoint -- so it must never pass silently.
            raise

    def state(self):
        with self._lock:
            return {"limit": self.limit, "in_flight": self._in_flight, **self.stats}


class Guard:
    """A bulkhead and a breaker around one dependency, used as a context manager.

        with OTP_GUARD:
            out = transit_client.plan(...)

    Entering raises Overloaded or CircuitOpen instead of running the body. On
    exit, only the exception types named in `failure_types` count as failures.

    That distinction matters more than it looks. OTP rejecting a malformed
    GraphQL query is OUR bug and will happen identically on every retry -- if it
    tripped the breaker, one bad request could disable transit for everyone.
    Only a dependency that is unreachable or slow should open a circuit.
    """

    def __init__(self, name, limit, failure_types, fail_threshold=5, reset_after=30.0):
        self.name = name
        self.bulkhead = Bulkhead(name, limit)
        self.breaker = Breaker(name, fail_threshold=fail_threshold,
                               reset_after=reset_after)
        self.failure_types = tuple(failure_types)

    def __enter__(self):
        # Breaker first: it is the cheaper check, and when it is open there is no
        # reason to take a bulkhead slot only to hand it straight back.
        self.breaker._allow()
        try:
            self.bulkhead.acquire()
        except Overloaded:
            # Shedding load is not the dependency failing. Counting it as one
            # would let a traffic spike open the circuit and turn a busy minute
            # into a 30-second outage. But if this caller had just claimed the
            # half-open probe, hand it back or the circuit deadlocks.
            self.breaker.abandon()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is not None and issubclass(exc_type, self.failure_types):
                self.breaker.record_failure()
            elif exc_type is None:
                self.breaker.record_success()
            else:
                # An error that is not the dependency's fault -- a malformed
                # query, a bad argument. It says nothing about OTP's health, so
                # it must neither open the circuit NOR heal one that is failing.
                self.breaker.abandon()
        finally:
            self.bulkhead.release()
        return False        # never swallow

    def state(self):
        return {"breaker": self.breaker.state(), "bulkhead": self.bulkhead.state()}
