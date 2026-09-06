"""
Tests for the local autocomplete trie.

Two things are being protected here. The first is ordering: a search box that
returns the right answer fifth is a search box people stop using, so most of
these assert on *position*, not on membership. The second is completeness -- the
per-node top-K cache makes it easy to build an index that quietly cannot reach
some entries at all, which is a bug that looks like nothing until someone types
the name of a real street and gets five other streets back.

These run against a small fixture rather than the real places.json, so they stay
meaningful on a clone that has never downloaded the GTFS feed.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import places  # noqa: E402


def entry(name, kind="stop", weight=1, lat=53.5, lon=-113.5):
    return {"n": name, "y": lat, "x": lon, "k": kind, "w": weight}


FIXTURE = [
    entry("West Edmonton Mall", "landmark", 10_000_000, 53.5225, -113.6242),
    entry("West Edmonton Mall Transit Centre", "stop", 1_400_000),
    entry("North West Edmonton Mall TC", "stop", 1_050_000),
    entry("Churchill Station", "stop", 1_300_000, 53.5440, -113.4900),
    entry("Churchill Stop", "stop", 1_200_000),
    entry("Southgate Transit Centre", "stop", 1_350_000),
    entry("111 Street & 19 Avenue", "stop", 1_020_000),
    entry("111 Street & 23 Avenue", "stop", 1_010_000),
    entry("111 Street", "street", 1_400),
    entry("34 Avenue NW", "street", 1_300),
    entry("34A Avenue NW", "street", 1_900),          # deliberately heavier
    entry("34 Street & 118 Avenue", "stop", 1_005_000),
    entry("Jasper Avenue NW", "street", 1_250),
    entry("101 Street & Jasper Avenue", "stop", 1_090_000),
    entry("Whyte Avenue NW", "street", 1_500),
]


class SearchTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.g = places.Gazetteer(FIXTURE)

    def names(self, q, limit=5):
        return [r["display_name"] for r in self.g.search(q, limit=limit)]

    # ── the basics ─────────────────────────────────────────────────────────
    def test_prefix_of_the_first_word(self):
        self.assertEqual("Churchill Station", self.names("church")[0])

    def test_weight_orders_equally_good_matches(self):
        # Both begin "Churchill"; the busier one comes first.
        self.assertEqual(["Churchill Station", "Churchill Stop"], self.names("churchill"))

    def test_matches_a_word_that_is_not_the_first(self):
        # The reason every word is indexed, not just the leading one.
        self.assertIn("101 Street & Jasper Avenue", self.names("jasper"))

    def test_unknown_prefix_returns_nothing_rather_than_guessing(self):
        self.assertEqual([], self.names("qqqz"))

    def test_punctuation_in_the_data_is_not_something_riders_type(self):
        # The stored name has "&"; the query does not.
        self.assertIn("111 Street & 19 Avenue", self.names("111 street 19"))

    # ── ordering, which is most of the value ───────────────────────────────
    def test_word_order_beats_a_scattered_match(self):
        """"34 ave" means 34 Avenue, not 34 Street & 118 Avenue.

        Both entries contain a word starting "34" and a word starting "ave", so
        a matcher that only asks "does every token match some word" ranks them
        equally. Adjacency is what tells them apart.
        """
        got = self.names("34 ave")
        self.assertEqual("34 Avenue NW", got[0])
        self.assertLess(got.index("34 Avenue NW"), got.index("34 Street & 118 Avenue"))

    def test_a_finished_word_prefers_an_exact_hit_over_a_prefix_hit(self):
        # "34A Avenue" carries more weight than "34 Avenue" in the fixture, so
        # only the exactness rule can put "34 Avenue" first.
        self.assertEqual("34 Avenue NW", self.names("34 ave")[0])

    def test_the_whole_name_typed_out_wins_outright(self):
        """A street must not be buried under the stops named after it.

        Every "111 Street & ..." stop outranks the street on weight by three
        orders of magnitude, and matches the query just as well by every other
        rule.
        """
        self.assertEqual("111 Street", self.names("111 street")[0])

    def test_a_leading_match_beats_a_buried_one(self):
        got = self.names("west edm")
        self.assertEqual("West Edmonton Mall", got[0])
        self.assertLess(got.index("West Edmonton Mall Transit Centre"),
                        got.index("North West Edmonton Mall TC"))

    # ── completeness ───────────────────────────────────────────────────────
    def test_every_entry_is_reachable_by_its_own_name(self):
        """The property the per-node top-K cache is most likely to break.

        Node caches are capped, so a multi-word query that gathered candidates
        from those caches would silently lose anything that never made a top-K
        list anywhere. Collecting from the complete terminal lists is what makes
        this hold, and this test is the reason that bug did not ship.
        """
        for e in FIXTURE:
            got = [r["display_name"] for r in self.g.search(e["n"], limit=20)]
            self.assertIn(e["n"], got, "cannot find %r by typing it out" % e["n"])

    def test_multi_word_queries_reach_past_the_cache_cap(self):
        # Enough same-prefixed entries to overflow NODE_TOP_K several times over,
        # with the target deliberately the lightest of them.
        overflow = places.NODE_TOP_K * 4
        many = [entry("Elm Street & %d Avenue" % i, "stop", 2_000_000 - i)
                for i in range(overflow)]
        many.append(entry("Elm Street & Rare Lane", "stop", 1))
        g = places.Gazetteer(many)
        got = [r["display_name"] for r in g.search("elm rare", limit=5)]
        self.assertEqual(["Elm Street & Rare Lane"], got,
                         "%d decoys hid the only real match" % overflow)

    # ── shape of the answer, and the edges ─────────────────────────────────
    def test_result_carries_coordinates_and_marks_its_source(self):
        r = self.g.search("west edmonton mall")[0]
        self.assertAlmostEqual(53.5225, r["lat"])
        self.assertAlmostEqual(-113.6242, r["lon"])
        self.assertEqual("local", r["source"])
        self.assertEqual("landmark", r["kind"])

    def test_limit_is_respected(self):
        self.assertLessEqual(len(self.g.search("1", limit=2)), 2)

    def test_empty_and_punctuation_only_queries_are_not_errors(self):
        for q in ("", "   ", "&&&", "-"):
            self.assertEqual([], self.g.search(q), "query %r" % q)

    def test_an_empty_gazetteer_answers_nothing(self):
        self.assertEqual([], places.Gazetteer([]).search("anything"))

    def test_a_repeated_word_does_not_take_two_result_slots(self):
        g = places.Gazetteer([entry("Street Street Station", "stop", 5),
                              entry("Street Lane", "stop", 4)])
        got = [r["display_name"] for r in g.search("street", limit=5)]
        self.assertEqual(len(got), len(set(got)))

    def test_module_level_search_is_safe_without_a_gazetteer(self):
        saved = places.GAZETTEER
        try:
            places.GAZETTEER = None
            self.assertEqual([], places.search("churchill"))
            self.assertFalse(places.ready())
        finally:
            places.GAZETTEER = saved


class NormaliseTest(unittest.TestCase):

    def test_splits_on_everything_a_rider_would_not_type(self):
        self.assertEqual(["100", "street", "jasper", "avenue"],
                         places.normalise("100 Street & Jasper Avenue"))
        self.assertEqual(["st", "albert", "trail", "nw"],
                         places.normalise("St. Albert Trail NW"))

    def test_none_and_empty_are_not_errors(self):
        self.assertEqual([], places.normalise(None))
        self.assertEqual([], places.normalise(""))


if __name__ == "__main__":
    unittest.main()
