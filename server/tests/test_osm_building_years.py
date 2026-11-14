"""OSM building construction years (tools/build_osm_building_years.py -> PMTiles parts; server/osmyears.py + earthfast
buildyears.rs read a tile): the builder's year parse on the values OSM has most, a small Layercake-like file built
and read back through a fake host, a building on a tile edge, relations' keys, damaged tiles, and when a tile's
buildings fault (unreachable) or not (not published).

No network: the parquet file is written here (DuckDB), the archive served by a fake fetcher."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

HERE = Path(__file__).resolve()
sys.path[:0] = [str(HERE.parents[1]), str(HERE.parents[2] / "tools")]
import build_osm_building_years as by  # noqa: E402
import fast  # noqa: E402
import geodesy  # noqa: E402
import netfetch  # noqa: E402
import osmyears  # noqa: E402
import revisit  # noqa: E402

try:
    import duckdb
except ImportError:
    duckdb = None

BASE = "https://years.test/osmyears"
LAT0, LON0 = 40.7484, -73.9857


class FakeFetcher:
    """Range reads of in-memory files by URL (whole files without a Range), one ETag; fail: raised for every get."""

    def __init__(self, files):
        self.files, self.fail, self.seen = files, None, []

    def get(self, url, *, kind="demand", headers=None, lane=None):
        self.seen.append(url.rsplit("/", 1)[-1])
        if self.fail is not None:
            raise self.fail
        body = self.files.get(url)
        if body is None:
            raise netfetch.NotFound(f"{url}: 404", url, netfetch.host_of(url), status=404)
        if not headers or "Range" not in headers:
            return netfetch.Response(200, body, {})
        a, b = headers["Range"].split("=")[1].split("-")
        a, b = int(a), min(int(b), len(body) - 1)
        return netfetch.Response(206, body[a:b + 1], {"etag": '"y1"'})


def write_parquet(path, rows, group_rows=8):
    """rows [(type, id, start_date, (x0, y0, x1, y1))] as Layercake's buildings file has them."""
    con = duckdb.connect()
    con.execute("CREATE TABLE b (type VARCHAR, id BIGINT, start_date VARCHAR, "
                "bbox STRUCT(xmin FLOAT, ymin FLOAT, xmax FLOAT, ymax FLOAT))")
    for t, i, s, bb in rows:
        con.execute("INSERT INTO b VALUES (?, ?, ?, ?)",
                    [t, i, s, dict(xmin=bb[0], ymin=bb[1], xmax=bb[2], ymax=bb[3])])
    con.execute(f"COPY b TO '{path}' (FORMAT PARQUET, ROW_GROUP_SIZE {group_rows})")
    con.close()


def z12_edge_lon():
    """A z12 tile's west edge near LON0 (a building across it is in both tiles)."""
    n = 1 << by.SHARD_ZOOM
    x = int((LON0 + 180.0) / 360.0 * n)
    return x / n * 360.0 - 180.0


def box(lon, lat, d=0.0003):
    return (lon - d, lat - d, lon + d, lat + d)


EDGE = z12_edge_lon()
ROWS = [
    ("way", 34633854, "1931", box(LON0, LAT0)),                   # the Empire State Building
    ("way", 42500770, "1930", box(LON0 + 0.01, LAT0 + 0.004)),    # Chrysler
    ("relation", 3565284, "c. 1890", box(LON0 - 0.002, LAT0)),    # a relation: its own key
    ("way", 3565284, "2001", box(LON0 - 0.002, LAT0 + 0.001)),    # the same number as a way
    ("way", 5, "1913..1915", (EDGE - 0.001, LAT0, EDGE + 0.001, LAT0 + 0.001)),   # across a tile edge
    ("way", 6, "unknown", box(LON0, LAT0 + 0.002)),               # no year: left out
    ("way", 7, None, box(LON0, LAT0 + 0.003)),                     # no start_date
    ("node", 8, "1950", box(LON0, LAT0 + 0.004)),                 # not a way or relation
    ("way", 9, "late C19", box(LON0 + 0.4, LAT0 + 0.3)),          # far off, another tile
] + [("way", 100 + k, str(1900 + k), box(LON0 + 0.001 * k, LAT0 - 0.003)) for k in range(30)]
WANT = {2 * 34633854: 1931, 2 * 42500770: 1930, 2 * 3565284 + 1: 1890, 2 * 3565284: 2001, 2 * 5: 1913,
        2 * 9: 1885, **{2 * (100 + k): 1900 + k for k in range(30)}}


class YearSqlTest(unittest.TestCase):
    @unittest.skipIf(duckdb is None, "DuckDB not installed")
    def test_values_osm_has_most(self):
        cases = {"1931": 1931, "1930-1931": 1930, "1976..1991": 1976, "~1900": 1900, "July 2023": 2023,
                 "1950s": 1950, "before 1976": 1975, "<2004": 2003, "after 1949": 1950, ">1971": 1972, "C19": 1850,
                 "early C20": 1915, "late C19": 1885, "mid C18": 1750, "2014-11-03": 2014, "c. 1890": 1890,
                 "12345": None, "0950": None, "abc": None, "2200": None, "C9": None, "": None}
        con = duckdb.connect()
        con.execute("CREATE TABLE t (start_date VARCHAR)")
        con.executemany("INSERT INTO t VALUES (?)", [[k] for k in cases])
        got = dict(con.execute(f"SELECT start_date, {by.YEAR_SQL} FROM t").fetchall())
        self.assertEqual(got, cases)

    def test_keys(self):
        self.assertEqual(osmyears.key_of("w34633854@78"), 2 * 34633854)
        self.assertEqual(osmyears.key_of("r3565284@3"), 2 * 3565284 + 1)
        self.assertIsNone(osmyears.key_of("n5@1"))
        self.assertIsNone(osmyears.key_of(""))
        self.assertIsNone(osmyears.key_of(None))
        self.assertEqual(by.key_of("relation", 7), osmyears.key_of("r7@1"))
        self.assertIsNone(by.key_of("node", 7))


class DecodeTest(unittest.TestCase):
    def setUp(self):
        fast.require(osmyears.NATIVE, "oby_decode")

    def test_round_trip_raw_and_deflated(self):
        keys = np.array([4, 10, 2 ** 40], np.uint64)
        years = np.array([1913, 1850, 2024], np.int64)
        blob = by.encode_tile(keys, years)
        for b in (blob, by.ots.deflate(blob)):
            k, y = fast._ef.oby_decode(b)
            self.assertEqual(k.tolist(), keys.tolist())
            self.assertEqual(y.tolist(), years.tolist())

    def test_damaged_tiles_raise(self):
        blob = by.encode_tile(np.array([4, 10], np.uint64), np.array([1913, 1999]))
        for bad in (b"", b"OBY1", blob[:-1], b"XXXX" + blob[4:], by.ots.deflate(blob)[:-3],
                    b"OBY1" + by.ots.uv1(1 << 40)):
            with self.assertRaises(ValueError):
                fast._ef.oby_decode(bad)


@unittest.skipIf(duckdb is None, "DuckDB not installed")
class BuildAndReadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fast.require(osmyears.NATIVE, "oby_decode")
        cls.tmp = tempfile.mkdtemp(prefix="oby-test-")
        cls.parquet = os.path.join(cls.tmp, "buildings.parquet")
        write_parquet(cls.parquet, ROWS)
        cls.out = os.path.join(cls.tmp, "out")
        with mock.patch.object(by.ots, "PART_SLACK", 400):      # (parts of a tile or two: several here)
            cls.man = by.build(cls.parquet, cls.out, workers=1, log=lambda s: None, part_bytes=480)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def files(self):
        out = {}
        for p in self.man["parts"]:
            with open(os.path.join(self.out, p["archive"]), "rb") as f:
                out[f"{BASE}/{p['archive']}"] = f.read()
        with open(os.path.join(self.out, "manifest.json"), "rb") as f:
            out[f"{BASE}/manifest.json"] = f.read()
        return out

    def md(self, fetcher):
        return SimpleNamespace(fetcher=fetcher, cache_dir=tempfile.mkdtemp(dir=self.tmp))

    def test_manifest(self):
        m = self.man
        self.assertEqual((m["format"], m["zoom"]), (1, 12))
        self.assertEqual(m["rows"], sum(len(by.cut(*self._rows())[t][0]) for t in by.cut(*self._rows())))
        self.assertGreaterEqual(len(m["parts"]), 2, "parts of at most 480 bytes")
        self.assertEqual([p["archive"] for p in m["parts"]],
                         [f"osmyears-{i:03d}.pmtiles" for i in range(len(m["parts"]))])

    def _rows(self):
        got = [r for r in ROWS if r[0] in ("way", "relation") and r[2] and r[2] != "unknown"]
        yrs = {"c. 1890": 1890, "1913..1915": 1913, "late C19": 1885}
        return (np.array([by.key_of(t, i) for t, i, _s, _b in got], np.uint64),
                np.array([yrs.get(s, int(s[:4]) if s[:4].isdigit() else 0) for _t, _i, s, _b in got], np.int16),
                np.array([b for *_x, b in got], np.float32))

    def test_every_dated_building_read_back(self):
        f = FakeFetcher(self.files())
        with mock.patch.object(osmyears, "BASE", BASE):
            sh = osmyears.shards(self.md(f))
            got = osmyears.tiles_of(sh, LAT0 - 0.01, LON0 - 0.02, LAT0 + 0.31, LON0 + 0.41)
        self.assertEqual(dict(zip(got[0].tolist(), got[1].tolist())), WANT)
        self.assertEqual(got[0].tolist(), sorted(got[0].tolist()))

    def test_a_building_on_a_tile_edge_is_in_both_tiles(self):
        tiles = by.cut(*self._rows())
        holding = [t for t, (k, _y) in tiles.items() if 2 * 5 in k.tolist()]
        self.assertEqual(len(holding), 2)
        f = FakeFetcher(self.files())
        with mock.patch.object(osmyears, "BASE", BASE):
            sh = osmyears.shards(self.md(f))
            for d in (-0.0005, 0.0005):        # (from either side, one tile each)
                got = osmyears.tiles_of(sh, LAT0, EDGE + d, LAT0 + 0.0001, EDGE + d)
                self.assertEqual(dict(zip(got[0].tolist(), got[1].tolist())).get(10), 1913)

    def test_tile_fetch_years_and_lookup(self):
        f = FakeFetcher(self.files())
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmyears, "BASE", BASE):
            n0 = revisit.faults()
            yrs = osmyears.Fetch(frame, -300, -300, 300, 300, self.md(f)).years()
            self.assertEqual(revisit.faults(), n0)
        self.assertEqual(osmyears.lookup(yrs, ["w34633854@78", "r3565284@1", "w3565284@2", "w6@1", None, "n1@1",
                                               "w999999@1"]).tolist(), [1931, 1890, 2001, 0, 0, 0, 0])

    def test_not_published_no_fault(self):
        f = FakeFetcher({})
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmyears, "BASE", BASE):
            n0 = revisit.faults()
            self.assertIsNone(osmyears.Fetch(frame, -300, -300, 300, 300, self.md(f)).years())
            self.assertEqual(revisit.faults(), n0, "nothing published: the buildings as before, kept")

    def test_unreachable_faults(self):
        f = FakeFetcher(self.files())
        f.fail = netfetch.FetchFailed("offline")
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmyears, "BASE", BASE):
            n0 = revisit.faults()
            self.assertIsNone(osmyears.Fetch(frame, -300, -300, 300, 300, self.md(f)).years())
            self.assertEqual(revisit.faults(), n0 + 1, "built again later, with the years")

    def test_a_tile_that_fails_faults(self):
        files = self.files()
        f = FakeFetcher(files)
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmyears, "BASE", BASE):
            md = self.md(f)
            sh = osmyears.shards(md)
            self.assertTrue(sh.usable())            # (the manifest had)
            for p in self.man["parts"]:             # (then the archive gone)
                files.pop(f"{BASE}/{p['archive']}")
            n0 = revisit.faults()
            self.assertIsNone(osmyears.Fetch(frame, -300, -300, 300, 300, md).years())
            self.assertEqual(revisit.faults(), n0 + 1)

    def test_a_manifest_of_another_format_is_not_read(self):
        files = self.files()
        m = json.loads(files[f"{BASE}/manifest.json"])
        m["format"] = 99
        files[f"{BASE}/manifest.json"] = json.dumps(m).encode()
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmyears, "BASE", BASE):
            n0 = revisit.faults()
            self.assertIsNone(osmyears.Fetch(frame, -300, -300, 300, 300, self.md(FakeFetcher(files))).years())
            self.assertEqual(revisit.faults(), n0, "a newer format: no years, no faults")

    def test_off(self):
        with mock.patch.object(osmyears, "ON", False):
            self.assertIsNone(osmyears.shards(self.md(FakeFetcher(self.files()))))
        import railtags
        frame = geodesy.EnuFrame(LAT0, LON0)
        with mock.patch.object(osmyears, "BASE", BASE), mock.patch.object(railtags, "ON", False):
            n0 = revisit.faults()       # (the Pages datasets off: no years, no faults)
            self.assertIsNone(osmyears.Fetch(frame, -300, -300, 300, 300, self.md(FakeFetcher(self.files()))).years())
            self.assertEqual(revisit.faults(), n0)
        self.assertIsNone(osmyears.shards(SimpleNamespace(fetcher=None, cache_dir=self.tmp)))


if __name__ == "__main__":
    unittest.main()
