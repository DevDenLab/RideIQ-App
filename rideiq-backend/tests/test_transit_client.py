"""
The first tests in this repo.

They cover transit_client's pure functions -- the translation layer between
OpenTripPlanner and the app. That is deliberate: it is the code most likely to be
silently wrong, because a mistake here does not crash, it just quotes the wrong
price or the wrong time and looks entirely plausible on screen.

No OTP and no network: every case feeds a hand-built response shaped like the
real one. Run with `python -m unittest discover -s tests`.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import transit_client as T


class Post(unittest.TestCase):
    """_post()'s job is to turn "OTP did not answer" into TransitUnavailable,
    which is the ONLY exception type app.py's circuit breaker is configured
    to count as the dependency's fault (see resilience.py's Guard).

    A read timeout after the connection succeeds -- OTP present but stalled,
    exactly the scenario the breaker exists for -- raises a bare TimeoutError
    from deep inside http.client, past the point urllib wraps anything as
    URLError. The original except clause only caught URLError, so this class
    of failure propagated as an unhandled 500 AND was invisible to the
    breaker: confirmed live against a real stalled server, ten consecutive
    calls, all failing, breaker state stayed closed with zero failures
    recorded. Both cases are pinned here so neither regresses silently.
    """

    def test_a_connection_refusal_is_transit_unavailable(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=T.urllib.error.URLError("refused")):
            with self.assertRaises(T.TransitUnavailable):
                T._post("{}", {})

    def test_a_stalled_response_is_ALSO_transit_unavailable(self):
        """The bug this test exists for: a bare TimeoutError, not URLError."""
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(T.TransitUnavailable):
                T._post("{}", {})

    def test_a_genuine_bug_in_our_own_query_still_raises_plainly(self):
        # Not every failure is the dependency's fault. OTP rejecting a
        # malformed GraphQL query is ours, and must stay a RuntimeError, not
        # get relabelled TransitUnavailable -- doing that would let a broken
        # query silently open the circuit for every caller.
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"errors": [{"message": "bad query"}]}'
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse()):
            with self.assertRaises(RuntimeError):
                T._post("{}", {})


class DecodePolyline(unittest.TestCase):
    def test_reference_polyline(self):
        """The example from Google's own encoded-polyline spec."""
        pts = T.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
        self.assertEqual(pts, [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]])

    def test_empty(self):
        self.assertEqual(T.decode_polyline(""), [])


class DurationScalar(unittest.TestCase):
    """OTP sends ISO-8601 durations, not numbers -- 'PT3M30S', not 210."""

    def test_forms(self):
        self.assertEqual(T._duration_seconds("PT3M30S"), 210)
        self.assertEqual(T._duration_seconds("PT1H"), 3600)
        self.assertEqual(T._duration_seconds("PT45S"), 45)
        self.assertEqual(T._duration_seconds("-PT45S"), -45)

    def test_passthrough_and_junk(self):
        self.assertEqual(T._duration_seconds(210), 210)
        self.assertIsNone(T._duration_seconds(None))
        self.assertIsNone(T._duration_seconds("not-a-duration"))


class DelayText(unittest.TestCase):
    def test_rounds_to_the_nearest_minute(self):
        self.assertEqual(T._delay_text(0), "on time")
        self.assertEqual(T._delay_text(20), "on time")      # under a minute
        self.assertEqual(T._delay_text(-20), "on time")
        self.assertEqual(T._delay_text(180), "3 min late")
        self.assertEqual(T._delay_text(-120), "2 min early")

    def test_no_data_says_nothing(self):
        """Absent realtime must stay absent, never become a cheerful 'on time'."""
        self.assertIsNone(T._delay_text(None))


def _priced(use_id, medium, amount):
    return {"id": use_id,
            "product": {"name": "Edmonton %s Fare" % medium,
                        "medium": {"name": medium},
                        "riderCategory": None,
                        "price": {"amount": amount, "currency": {"code": "CAD"}}}}


class Fares(unittest.TestCase):
    """The subtlest code in the project, and the most expensive to get wrong."""

    def test_transfer_is_one_fare_not_three(self):
        # Three bus legs covered by a single purchase: OTP gives every leg the
        # same fare-product use id. Summing per leg would charge $9 for a $3 trip.
        legs = [{"fareProducts": [_priced("use-1", "Arc", 3.00)]} for _ in range(3)]
        fare = T._fare(legs)
        self.assertEqual(fare["amount"], 3.00)
        self.assertEqual(fare["medium"], "Arc")

    def test_separate_purchases_do_add_up(self):
        # Distinct use ids mean the transfer window lapsed: two real fares.
        legs = [{"fareProducts": [_priced("use-1", "Arc", 3.00)]},
                {"fareProducts": [_priced("use-2", "Arc", 3.00)]}]
        self.assertEqual(T._fare(legs)["amount"], 6.00)

    def test_cheapest_medium_wins_and_others_are_offered(self):
        legs = [{"fareProducts": [_priced("u", "Cash", 3.75),
                                  _priced("u", "Arc", 3.00)]}]
        fare = T._fare(legs)
        self.assertEqual(fare["medium"], "Arc")
        self.assertEqual(fare["amount"], 3.00)
        self.assertEqual([o["medium"] for o in fare["options"]], ["Arc", "Cash"])
        self.assertEqual(fare["text"], "CAD 3.00 (Arc)")

    def test_unpriced_trip_is_none_not_zero(self):
        """A feed that prices nothing must not read as a free trip."""
        self.assertIsNone(T._fare([{"fareProducts": []}, {}]))


class Instructions(unittest.TestCase):
    def test_walk_and_ride_lines(self):
        legs = [
            {"mode": "WALK", "to": "Churchill Station", "duration_min": 2,
             "distance_m": 174},
            {"mode": "BUS", "mode_label": "Bus", "route": "8",
             "headsign": "Abbottsfield", "from": "Churchill Station",
             "to": "Coliseum", "depart": "17:48", "duration_min": 17,
             "status": None},
        ]
        lines = T._instructions(legs)
        self.assertEqual(lines[0], "Walk 2 min (174 m) to Churchill Station")
        self.assertIn("Board Bus 8 toward Abbottsfield at Churchill Station, 17:48",
                      lines[1])
        self.assertEqual(lines[-1], "You have arrived at your destination")

    def test_live_status_appears_only_when_known(self):
        ride = {"mode": "BUS", "mode_label": "Bus", "route": "8", "headsign": "",
                "from": "A", "to": "B", "depart": "17:48", "duration_min": 5}
        self.assertNotIn("(", T._instructions([dict(ride, status=None)])[0])
        self.assertIn("(3 min late)",
                      T._instructions([dict(ride, status="3 min late")])[0])


class WalkFilter(unittest.TestCase):
    def test_longest_walk_leg_is_what_counts(self):
        itin = {"legs": [{"mode": "WALK", "distance_m": 200},
                         {"mode": "BUS", "distance_m": 5000},
                         {"mode": "WALK", "distance_m": 850}]}
        self.assertEqual(T._walk_metres(itin), 850)

    def test_no_walk_legs(self):
        self.assertEqual(T._walk_metres({"legs": [{"mode": "BUS",
                                                   "distance_m": 900}]}), 0)


class Alerts(unittest.TestCase):
    def test_blank_alerts_are_dropped(self):
        raw = [{"alertHeaderText": "", "alertDescriptionText": ""},
               {"alertHeaderText": "Detour on 104 St",
                "alertDescriptionText": "Construction until June",
                "alertSeverityLevel": "WARNING", "alertEffect": "DETOUR"}]
        out = T._alerts(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["header"], "Detour on 104 St")

    def test_description_only_alert_still_shows(self):
        out = T._alerts([{"alertHeaderText": "", "alertDescriptionText": "Elevator out"}])
        self.assertEqual(out[0]["header"], "Elevator out")


if __name__ == "__main__":
    unittest.main()
