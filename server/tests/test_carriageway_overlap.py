"""Adjacent street pieces are not the two carriageways of a divided road."""
import math
import os
from pathlib import Path
import sys
import unittest

import numpy as np

HERE = Path(__file__).resolve().parent
os.environ.setdefault("BNE_REGIONAL_DIR", str(HERE / "fixtures/regional"))
sys.path[:0] = [str(HERE.parent), str(HERE)]
import fast
import navgraph
import roads
from test_roads import FRAME, features, flat, rules


def street():
    # Relative geometry of the Chicago junction: two short pieces connect long
    # stretches of the same two-way street, with a small bend at each join.
    points = [(-192.79, -8.81), (0., 0.), (7.57, .44), (12.47, .89),
              (19.59, .88), (136.69, 3.51)]
    f = features([[a, b] for a, b in zip(points, points[1:])], ["secondary"] * 5)
    f.attrs["name"] = np.array(["Connected Street"] * 5)
    f.rules["connectors"] = rules([(i, f"joint{i}", 0., 0.) for i in range(5)] +
                                  [(i, f"joint{i + 1}", 1., 1.) for i in range(5)])
    return f


class CarriagewayOverlapTest(unittest.TestCase):
    def side(self, a, b):
        return fast._ef.dual_side(np.asarray(a, np.float64), np.asarray(b, np.float64),
                                  4., 45., math.cos(math.radians(25.)))

    def test_endpoint_only_neighbours_are_not_beside_each_other(self):
        a = [(0., 0.), (7.57, .44)]
        b = [(12.47, .89), (19.59, .88)]
        for x, y in ((a, b), (a[::-1], b), (a, b[::-1]), (b, a)):
            with self.subTest(a=x, b=y):
                self.assertEqual(self.side(x, y), (0., 0.))

    def test_parallel_carriageways_keep_their_overlap(self):
        for reverse in (False, True):
            b = [(0., 12.), (100., 12.)]
            if reverse:
                b.reverse()
            share, side = self.side([(0., 0.), (100., 0.)], b)
            self.assertEqual((share, side), (1., 1.))
        share, side = self.side([(0., 0.), (20., 0.)], [(10., 8.), (40., 8.)])
        self.assertAlmostEqual(share, 2. / 3.)
        self.assertEqual(side, 1.)

    def test_short_connected_street_pieces_keep_two_way_access(self):
        for left_hand in (False, True):
            with self.subTest(left_hand=left_hand):
                specs = roads.road_specs(street(), lambda la, lo: np.ones(np.shape(la), bool),
                                         left_hand=left_hand)
                self.assertTrue(all(sp is not None and not sp.oneway for sp in specs))

    def test_real_divided_streets_keep_their_inferred_directions(self):
        for left_hand in (False, True):
            f = features([[(0., 0.), (100., 0.)], [(100., 12.), (0., 12.)]],
                         ["secondary"] * 2)
            f.attrs["name"] = np.array(["Divided Street"] * 2)
            specs = roads.road_specs(f, lambda la, lo: np.ones(np.shape(la), bool),
                                     left_hand=left_hand)
            self.assertTrue(all(sp.oneway for sp in specs))
            self.assertEqual([sp.dir for sp in specs], [-1, -1] if left_hand else [1, 1])

    def test_graph_can_cross_the_loaded_junction_both_ways(self):
        graph = navgraph.build(FRAME, [street()], flat, (0., 0.),
                               urban_fn=lambda la, lo: np.ones(np.shape(la), bool),
                               signals=[], country="US", window=300.)
        links = {name: set() for name in graph["nodes"]}
        for a, b, one_way, *_ in graph["edges"]:
            links[a].add(b)
            if not one_way:
                links[b].add(a)
        for start, end in (("cjoint0", "cjoint5"), ("cjoint5", "cjoint0")):
            pending, seen = [start], set()
            while pending:
                name = pending.pop()
                if name in seen:
                    continue
                seen.add(name)
                pending.extend(links[name] - seen)
            self.assertIn(end, seen)


if __name__ == "__main__":
    unittest.main()
