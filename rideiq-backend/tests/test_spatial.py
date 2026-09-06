"""
Tests for the k-d tree that answers "which graph node is nearest to here".

The property that matters is exactness. A spatial index is an optimisation, and
an optimisation that changes answers is a bug -- so almost every test here checks
the tree against an exhaustive scan rather than against a hand-written expected
value. Getting a k-d tree subtly wrong produces a plausible-looking nearest node
most of the time, which is exactly the failure a fixed test case would miss.
"""
import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spatial  # noqa: E402


def brute(points, qx, qy):
    """Exhaustive nearest. The oracle every test below is measured against."""
    return min(points, key=lambda p: (p[0] - qx) ** 2 + (p[1] - qy) ** 2)


class KDTreeTest(unittest.TestCase):

    def test_matches_brute_force_at_many_sizes(self):
        rng = random.Random(7)
        for n in (1, 2, 3, 4, 5, 17, 64, 1000):
            pts = [(rng.uniform(-500, 500), rng.uniform(-500, 500), i) for i in range(n)]
            tree = spatial.KDTree(pts)
            for _ in range(200):
                qx, qy = rng.uniform(-700, 700), rng.uniform(-700, 700)
                got = pts[tree.nearest(qx, qy)]
                want = brute(pts, qx, qy)
                # Ties are legal, so compare distance rather than identity.
                self.assertAlmostEqual(
                    (got[0] - qx) ** 2 + (got[1] - qy) ** 2,
                    (want[0] - qx) ** 2 + (want[1] - qy) ** 2,
                    places=9,
                    msg="n=%d query=(%.2f, %.2f)" % (n, qx, qy))

    def test_empty_tree_returns_none_rather_than_raising(self):
        tree = spatial.KDTree([])
        self.assertIsNone(tree.nearest(0, 0))
        self.assertEqual(0, tree.size)
        self.assertEqual((None, None), tree.nearest_with_distance(0, 0))

    def test_duplicate_points_do_not_break_the_median_split(self):
        # argpartition on a column of identical values is the case most likely to
        # produce a malformed tree, and a road graph really does contain stacked
        # nodes at bridges and interchanges.
        pts = [(5.0, 5.0, i) for i in range(50)] + [(9.0, 9.0, 50)]
        tree = spatial.KDTree(pts)
        self.assertEqual(5.0, pts[tree.nearest(5.1, 5.1)][0])
        self.assertEqual(50, tree.nearest(9.0, 9.0))

    def test_collinear_points_still_index(self):
        # Every point sharing a coordinate means one axis never discriminates.
        pts = [(float(i), 0.0, i) for i in range(200)]
        tree = spatial.KDTree(pts)
        for q in (0, 1, 99.4, 150.6, 199, -20, 500):
            self.assertEqual(brute(pts, q, 0)[2], tree.nearest(q, 0), "q=%s" % q)

    def test_distance_is_reported_in_input_units(self):
        tree = spatial.KDTree([(0.0, 0.0, "a"), (3.0, 4.0, "b")])
        payload, dist = tree.nearest_with_distance(3.0, 4.0)
        self.assertEqual("b", payload)
        self.assertAlmostEqual(0.0, dist)
        payload, dist = tree.nearest_with_distance(0.0, 0.0)
        self.assertEqual("a", payload)
        self.assertAlmostEqual(0.0, dist)
        # A point equidistant-ish, to prove the distance is real and not squared.
        _, dist = tree.nearest_with_distance(6.0, 8.0)
        self.assertAlmostEqual(5.0, dist)


class GeoIndexTest(unittest.TestCase):

    # A handful of real Edmonton coordinates.
    PLACES = [
        (53.5445, -113.4909, "Churchill Square"),
        (53.5232, -113.5263, "University of Alberta"),
        (53.5225, -113.6242, "West Edmonton Mall"),
        (53.3097, -113.5797, "YEG airport"),
        (53.5716, -113.3904, "Abbottsfield"),
    ]

    def test_finds_the_obvious_nearest(self):
        idx = spatial.GeoIndex(self.PLACES)
        self.assertEqual("West Edmonton Mall", idx.nearest(53.5220, -113.6250))
        self.assertEqual("YEG airport", idx.nearest(53.3100, -113.5800))

    def test_distance_is_metres_and_roughly_right(self):
        idx = spatial.GeoIndex(self.PLACES)
        # Churchill Square to the U of A is about 4.3 km on the ground.
        name, metres = idx.nearest_with_metres(53.5232, -113.5263)
        self.assertEqual("University of Alberta", name)
        self.assertLess(metres, 1.0)
        # Stand 1 km due north of Churchill Square. The index models the earth as
        # a sphere of radius 6371 km, so a degree of latitude is 111,195 m -- use
        # that same figure here rather than the equatorial 110,574 m, or the test
        # is measuring the difference between two earth models, not the index.
        m_per_deg_lat = spatial.EARTH_R_M * math.pi / 180.0
        name, metres = idx.nearest_with_metres(53.5445 + 1000 / m_per_deg_lat, -113.4909)
        self.assertEqual("Churchill Square", name)
        self.assertAlmostEqual(1000.0, metres, delta=1.0)

    def test_projects_before_comparing(self):
        """The bug this class exists to fix, pinned as a test.

        At 53.5 degrees north a degree of longitude is 0.595 of a degree of
        latitude on the ground. These two candidates are equidistant in raw
        degrees, so a scan that minimises (dlat^2 + dlon^2) has to call it a tie
        -- but on the ground the eastern one is much closer. Anything that
        compares degrees will fail this.
        """
        d = 0.01
        idx = spatial.GeoIndex([
            (53.5 + d, -113.5, "north"),      # ~1106 m away
            (53.5, -113.5 + d, "east"),       # ~ 662 m away
        ])
        self.assertEqual("east", idx.nearest(53.5, -113.5))
        _, metres = idx.nearest_with_metres(53.5, -113.5)
        self.assertAlmostEqual(662.0, metres, delta=15.0)

    def test_never_returns_a_meaningfully_farther_point_than_haversine(self):
        """The property that actually matters, stated in metres on the ground.

        GeoIndex projects with a single cosine taken at the data's mean latitude,
        which is what makes an equirectangular projection cheap. Over a city that
        introduces a sub-percent scale error east-west, so for two candidates that
        are within a hair of each other the index can pick the "wrong" one.

        Demanding exact agreement with haversine would therefore be testing the
        projection rather than the tree, and would fail for reasons nobody should
        care about. What must hold is that the answer is never meaningfully
        farther. Measured over this fixture the index picks a different point on
        about 0.1% of queries and is never worse by more than about 1.4 m; the cap
        below is set from that, and a regression that broke the tree would blow
        through it by orders of magnitude.
        """
        rng = random.Random(11)
        pts = [(53.40 + rng.random() * 0.30, -113.75 + rng.random() * 0.45, i)
               for i in range(3000)]
        idx = spatial.GeoIndex(pts)

        def haversine_m(lat1, lon1, lat2, lon2):
            p1, p2 = math.radians(lat1), math.radians(lat2)
            dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
            a = (math.sin(dp / 2) ** 2
                 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
            return spatial.EARTH_R_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        worst = 0.0
        for _ in range(400):
            lat = 53.40 + rng.random() * 0.30
            lon = -113.75 + rng.random() * 0.45
            got = pts[idx.nearest(lat, lon)]
            chosen = haversine_m(lat, lon, got[0], got[1])
            truth = min(haversine_m(lat, lon, p[0], p[1]) for p in pts)
            worst = max(worst, chosen - truth)
        self.assertLess(worst, 3.0,
                        "index returned a point %.2f m farther than the true "
                        "nearest; the projection alone cannot explain that" % worst)

    def test_empty_index_is_safe(self):
        idx = spatial.GeoIndex([])
        self.assertEqual(0, idx.size)
        self.assertIsNone(idx.nearest(53.5, -113.5))


if __name__ == "__main__":
    unittest.main()
