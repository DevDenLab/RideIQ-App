"""
spatial.py -- a static k-d tree over the graph nodes.

Why this exists
---------------
Every routing request begins by answering "which graph node is nearest to this
coordinate", twice: once for the origin and once for the destination. Until now
that was a linear scan over all 24,725 drive nodes (and 100k+ walk nodes), which
measured at 3.11 ms per lookup -- 6.2 ms of a 10.6 ms three-route request, or 58%
of the routing CPU spent before any actual routing happens.

A k-d tree turns that scan into O(log n) descent with backtracking. The tree is
built once when the graph loads and never invalidated, because the graph is
static between rebuilds. That is the whole reason this is cheap here and hard at
Uber's scale: their index tracks moving drivers and must handle constant updates,
so the literature agonises over rebuild strategy. Ours is immutable.

It also fixes a correctness bug
-------------------------------
The old scan minimised (dlat^2 + dlon^2), treating a degree of longitude as
equal to a degree of latitude. At Edmonton's 53.5 degrees north a degree of
longitude spans only 0.595 of a degree of latitude on the ground, so the scan
over-weighted east-west distance by 1.68x and could return a node that is
genuinely not the closest. This module projects to metres first, so "nearest"
means nearest.

Implementation notes
--------------------
Plain Python, no new dependency. The tree lives in flat parallel lists rather
than node objects: attribute lookup on a Python object costs more than indexing
a list, and this is the hot path. numpy is used only to pick medians at build
time -- never during a query, because reading a numpy scalar is slower than
reading a float out of a list.
"""
import math

import numpy as np

EARTH_R_M = 6371000.0


class KDTree:
    """A static 2-d tree. Build once, query many times, never mutate.

    Points are (x, y) in whatever units the caller supplies; `nearest` returns
    the payload of the closest point by plain Euclidean distance. Callers that
    hold latitude/longitude should use GeoIndex below, which projects first.
    """

    __slots__ = ("_xs", "_ys", "_payload", "_axis", "_left", "_right", "_root", "size")

    def __init__(self, points):
        """points: iterable of (x, y, payload)."""
        pts = list(points)
        self.size = len(pts)
        n = self.size
        self._xs = [0.0] * n
        self._ys = [0.0] * n
        self._payload = [None] * n
        self._axis = [0] * n
        self._left = [-1] * n
        self._right = [-1] * n
        self._root = -1
        if n == 0:
            return

        xs = np.empty(n, dtype=np.float64)
        ys = np.empty(n, dtype=np.float64)
        payload = [None] * n
        for i, (x, y, p) in enumerate(pts):
            xs[i] = x
            ys[i] = y
            payload[i] = p

        # order[] is permuted in place as we partition; slot i of the tree holds
        # the point order[i]. Building this way means the tree's own arrays end
        # up in traversal order, which keeps the query loop's memory access tidy.
        order = np.arange(n)
        coords = (xs, ys)

        # Explicit stack instead of recursion: the walk graph has over 100k
        # nodes, so a recursive build would need a raised recursion limit to
        # avoid dying on a deep-but-legal tree.
        stack = [(0, n, 0, -1, 0)]      # lo, hi, depth, parent slot, is_right
        while stack:
            lo, hi, depth, parent, is_right = stack.pop()
            if lo >= hi:
                continue
            axis = depth & 1
            mid = (lo + hi) // 2
            seg = order[lo:hi]
            # argpartition puts the median in place and everything smaller to its
            # left -- exactly the invariant a k-d tree needs, at O(k) rather than
            # the O(k log k) a full sort would cost.
            k = mid - lo
            part = np.argpartition(coords[axis][seg], k)
            order[lo:hi] = seg[part]

            slot = mid
            self._axis[slot] = axis
            if parent < 0:
                self._root = slot
            elif is_right:
                self._right[parent] = slot
            else:
                self._left[parent] = slot

            stack.append((mid + 1, hi, depth + 1, slot, 1))
            stack.append((lo, mid, depth + 1, slot, 0))

        # Materialise coordinates in slot order as plain floats.
        for slot in range(n):
            src = int(order[slot])
            self._xs[slot] = float(xs[src])
            self._ys[slot] = float(ys[src])
            self._payload[slot] = payload[src]

    def nearest(self, qx, qy):
        """Payload of the closest point, or None when the tree is empty."""
        if self._root < 0:
            return None
        xs, ys = self._xs, self._ys
        axis, left, right = self._axis, self._left, self._right

        best_slot = -1
        best_d2 = float("inf")
        # Each stack entry carries the squared distance to the split plane that
        # sent us there, and it is re-tested on POP rather than on push. That is
        # the whole point of the backtracking: by the time the far subtree comes
        # up, the near side has usually tightened best_d2 enough to discard it.
        # Testing at push time would be correct but would prune almost nothing.
        stack = [(self._root, 0.0)]
        while stack:
            slot, plane2 = stack.pop()
            if slot < 0 or plane2 >= best_d2:
                continue
            dx = xs[slot] - qx
            dy = ys[slot] - qy
            d2 = dx * dx + dy * dy
            if d2 < best_d2:
                best_d2 = d2
                best_slot = slot

            delta = dx if axis[slot] == 0 else dy
            # delta < 0 means the query sits on the "right" side of this split.
            near, far = (right[slot], left[slot]) if delta < 0 else (left[slot], right[slot])
            # Far pushed first so near is popped first.
            stack.append((far, delta * delta))
            stack.append((near, 0.0))
        return self._payload[best_slot]

    def nearest_with_distance(self, qx, qy):
        """(payload, distance) in the tree's own units. Distance is None if empty."""
        if self._root < 0:
            return None, None
        # Kept separate from nearest() rather than sharing a helper: nearest() is
        # called twice per routing request and the extra tuple allocation is not
        # free at that rate.
        xs, ys = self._xs, self._ys
        axis, left, right = self._axis, self._left, self._right
        best_slot, best_d2 = -1, float("inf")
        stack = [(self._root, 0.0)]
        while stack:
            slot, plane2 = stack.pop()
            if slot < 0 or plane2 >= best_d2:
                continue
            dx = xs[slot] - qx
            dy = ys[slot] - qy
            d2 = dx * dx + dy * dy
            if d2 < best_d2:
                best_d2, best_slot = d2, slot
            delta = dx if axis[slot] == 0 else dy
            near, far = (right[slot], left[slot]) if delta < 0 else (left[slot], right[slot])
            stack.append((far, delta * delta))
            stack.append((near, 0.0))
        return self._payload[best_slot], math.sqrt(best_d2)


class GeoIndex:
    """A KDTree over latitude/longitude, projected so distances are metres.

    The projection is equirectangular about the data's own mean latitude. Over a
    city it is accurate to well under a metre, and unlike a proper projection it
    costs two multiplications per query. For "which of these 24,725 street
    corners is nearest", that is the right trade.
    """

    __slots__ = ("_tree", "_lat0_cos", "size")

    def __init__(self, items):
        """items: iterable of (lat, lon, payload)."""
        rows = [(float(la), float(lo), p) for la, lo, p in items]
        self.size = len(rows)
        if not rows:
            self._lat0_cos = 1.0
            self._tree = KDTree([])
            return
        lat0 = sum(r[0] for r in rows) / len(rows)
        self._lat0_cos = math.cos(math.radians(lat0))
        c = self._lat0_cos
        self._tree = KDTree(
            (EARTH_R_M * math.radians(lo) * c, EARTH_R_M * math.radians(la), p)
            for la, lo, p in rows)

    def _project(self, lat, lon):
        return (EARTH_R_M * math.radians(lon) * self._lat0_cos,
                EARTH_R_M * math.radians(lat))

    def nearest(self, lat, lon):
        x, y = self._project(lat, lon)
        return self._tree.nearest(x, y)

    def nearest_with_metres(self, lat, lon):
        x, y = self._project(lat, lon)
        return self._tree.nearest_with_distance(x, y)
