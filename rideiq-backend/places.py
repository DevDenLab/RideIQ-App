"""
places.py -- a local trie so autocomplete stops leaving the building.

The problem
-----------
/search proxied every keystroke to OpenStreetMap's Nominatim. Measured at 430 ms
per keystroke uncached, which is the worst latency a rider actually feels in this
app -- the map redraw and the route solve are both an order of magnitude faster.
Worse, it is a ceiling rather than a slow patch: Nominatim's usage policy is one
request per second for the whole application and explicitly not for autocomplete,
so the correct fix was never "cache harder", it was "stop asking".

Everything Edmonton needs is already on disk. Stop names come from the GTFS feed,
street names from the OSM extract the router is built on. build_places.py
compiles them into places.json; this module loads that at import and answers
prefixes from memory.

The structure
-------------
A trie over word prefixes, with the top few results precomputed at every node --
the design the typeahead literature describes, and the reason a lookup is O(len)
with no scanning at all: walk to the node the prefix spells, read the answer off
it.

Every word of a name is indexed, not just the first, because "34 ave" has to find
"58 Street & 34 Avenue". Multi-word queries descend once per word and intersect,
starting from the rarest word so the candidate set is small before any filtering.

Nominatim stays in the loop as the fallback. A gazetteer of stops and street
names cannot resolve "Sherwood Park Freeway near the Rexall" or a specific house
number, and pretending otherwise would make search worse, not faster. What
changes is that it is now the exception rather than every keystroke.
"""
import json
import os
import re
import time

PLACES_FILE = os.environ.get("PLACES_FILE",
                             os.path.join(os.path.dirname(__file__), "places.json"))

# How many results each trie node caches. The API returns 5; the extra headroom
# lets a multi-word query filter a node's cached list and still have something
# left before falling back to walking the subtree.
NODE_TOP_K = 12

# A prefix shorter than this matches thousands of names and nobody means anything
# by it. Matches the existing /search contract, which ignored queries under 3.
MIN_PREFIX = 2

_WORD = re.compile(r"[a-z0-9]+")


def normalise(text):
    """Lowercase, and split on anything that is not a letter or digit.

    Ampersands, slashes and hyphens all appear in ETS stop names ("100 Street &
    Jasper Avenue"), and a rider types none of them.
    """
    return _WORD.findall((text or "").lower())


class _Node:
    """One character of one indexed word.

    `top` is the precomputed answer for a single-word query landing here: read it
    and return, no scanning. `ends` is every entry whose indexed word finishes at
    exactly this node, and unlike `top` it is COMPLETE -- multi-word queries walk
    the subtree collecting `ends`, and if they collected `top` instead they would
    silently miss any entry that never made a node's top-K. That bug is invisible
    on common words and obvious on rare ones: "jasper ave" found five cross
    streets and not Jasper Avenue itself.
    """

    __slots__ = ("kids", "top", "ends", "count")

    def __init__(self):
        self.kids = {}
        self.top = []        # entry ids, best first, capped at NODE_TOP_K
        self.ends = []       # entry ids whose word ends here -- complete
        self.count = 0       # entries anywhere in this subtree


class Gazetteer:
    """Prefix search over a fixed set of named places."""

    def __init__(self, entries):
        self.entries = entries
        self.words = [normalise(e["n"]) for e in entries]
        self._root = _Node()
        self._build()

    # ── build ──────────────────────────────────────────────────────────────
    def _build(self):
        root = self._root
        # Insert in descending weight order so each node's `top` list is already
        # sorted by the time it is full -- no per-node sort, and the cap can be
        # applied as we go rather than by keeping everything and trimming later.
        order = sorted(range(len(self.entries)), key=lambda i: -self.entries[i]["w"])
        for eid in order:
            # A word repeated inside one name ("Street" in "Street & Street")
            # must not be inserted twice or it takes two slots in `top`.
            for word in set(self.words[eid]):
                node = root
                node.count += 1
                # `eid not in top` matters even at the root: one name can contain
                # two different words sharing a prefix ("South" and "Southgate"),
                # and the same entry must not occupy two of the K slots.
                if len(node.top) < NODE_TOP_K and eid not in node.top:
                    node.top.append(eid)
                for ch in word:
                    nxt = node.kids.get(ch)
                    if nxt is None:
                        nxt = node.kids[ch] = _Node()
                    node = nxt
                    node.count += 1
                    if len(node.top) < NODE_TOP_K and eid not in node.top:
                        node.top.append(eid)
                node.ends.append(eid)

    # ── query ──────────────────────────────────────────────────────────────
    def _descend(self, word):
        node = self._root
        for ch in word:
            node = node.kids.get(ch)
            if node is None:
                return None
        return node

    def _collect(self, node, cap):
        """Every entry id under a subtree, via the complete `ends` lists.

        Reached only for multi-word queries, and only from the rarest word the
        rider typed -- so in practice this walks a few dozen nodes, not the tree.
        The cap is a safety rail against someone typing "a b", not a design
        parameter; results are ordered afterwards by _rank either way.
        """
        out, seen, stack = [], set(), [node]
        while stack and len(out) < cap:
            cur = stack.pop()
            for eid in cur.ends:
                if eid not in seen:
                    seen.add(eid)
                    out.append(eid)
            stack.extend(cur.kids.values())
        return out

    def search(self, query, limit=5):
        """Entries whose words all start with the query's words. Best first."""
        tokens = normalise(query)
        if not tokens:
            return []
        # Only the last token is a live prefix -- the rest are words the rider has
        # finished typing. Treating them all as prefixes is still right, and is
        # what lets "u of a" work, so no special case here.
        nodes = []
        for t in tokens:
            node = self._descend(t)
            if node is None:
                return []           # no name contains a word starting like this
            nodes.append((node, t))

        if len(tokens) == 1:
            node, token = nodes[0]
            if len(token) < MIN_PREFIX and node.count > NODE_TOP_K:
                return []
            # Copy: `top` is the node's cached answer, and sorting it below would
            # permanently reorder it for every future query that lands here.
            ids = list(node.top)
        else:
            # Start from the most selective word, so the set to filter is small.
            node, _ = min(nodes, key=lambda nt: nt[0].count)
            ids = [i for i in self._collect(node, 4000) if self._matches_all(i, tokens)]

        ids.sort(key=lambda i: self._rank(i, tokens))
        return [self._render(i) for i in ids[:limit]]

    def _matches_all(self, eid, tokens):
        words = self.words[eid]
        for t in tokens:
            if not any(w.startswith(t) for w in words):
                return False
        return True

    @staticmethod
    def _phrase_at(words, tokens):
        """Index where `tokens` match consecutive words in order, or -1.

        Without this, "34 ave" ranks "34 Street & 118 Avenue" above "34 Avenue",
        because both contain a word starting "34" and a word starting "ave". Both
        are legitimate matches; only one is what was meant. Word order is the
        signal that separates them and it costs almost nothing to check.
        """
        span = len(tokens)
        for j in range(len(words) - span + 1):
            if all(words[j + k].startswith(tokens[k]) for k in range(span)):
                return j
        return -1

    def _rank(self, eid, tokens):
        """Lower sorts first.

        Four keys, most decisive first:

          whole   the query IS the name. "111 Street" must not be buried under
                  the forty stops called "111 Street & something", which outrank
                  it on every other key because a stop carries more weight than a
                  street.
          phrase  the typed words appear together, in order, somewhere in the name
          head    the name *begins* with what was typed -- someone typing "west"
                  means West Edmonton Mall, not "170 Street & West Meadowlark"
          loose   how many completed words were only prefix matches. Every token
                  but the last is a word the rider finished typing, so "34" ought
                  to prefer "34 Avenue" over "34A Avenue" -- both match, but only
                  one is what was typed. The last token is excluded because it is
                  still being typed and is a prefix by definition.
          weight  departures per week for a stop, length for a street
          length  shorter name wins an otherwise exact tie
        """
        words = self.words[eid]
        at = self._phrase_at(words, tokens)
        whole = 0 if words == tokens else 1
        phrase = 0 if at == 0 else (1 if at > 0 else 2)
        head = 0 if words and words[0].startswith(tokens[0]) else 1
        loose = 0
        if at >= 0:
            loose = sum(1 for k in range(len(tokens) - 1) if words[at + k] != tokens[k])
        return (whole, phrase, head, loose, -self.entries[eid]["w"],
                len(self.entries[eid]["n"]))

    def _render(self, eid):
        e = self.entries[eid]
        return {"lat": e["y"], "lon": e["x"], "display_name": e["n"],
                "short": e["n"], "kind": e["k"], "source": "local"}


# ── module-level singleton ─────────────────────────────────────────────────
GAZETTEER = None
LOAD_INFO = {"loaded": False, "entries": 0, "build_ms": 0, "path": PLACES_FILE}


def _load():
    global GAZETTEER
    if not os.path.exists(PLACES_FILE):
        # Not an error. A clone that has never run build_places.py still serves;
        # /search just falls through to Nominatim as it always did.
        LOAD_INFO["error"] = "no places.json -- run build_places.py"
        return
    try:
        t0 = time.perf_counter()
        data = json.load(open(PLACES_FILE, encoding="utf-8"))
        GAZETTEER = Gazetteer(data.get("entries") or [])
        LOAD_INFO.update(loaded=True, entries=len(GAZETTEER.entries),
                         build_ms=round((time.perf_counter() - t0) * 1000),
                         city=data.get("city"))
    except Exception as e:                       # a corrupt file must not stop boot
        LOAD_INFO["error"] = "%s: %s" % (type(e).__name__, e)


_load()


def search(query, limit=5):
    """Prefix search. Returns [] when there is no gazetteer or no match."""
    if GAZETTEER is None:
        return []
    return GAZETTEER.search(query, limit)


def ready():
    return GAZETTEER is not None
