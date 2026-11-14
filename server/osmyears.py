"""OpenStreetMap's construction years of buildings (start_date), for the towers' facades by era (buildings.plan:
TOWER_MASONRY_YEAR, TOWER_MIXED_YEAR) and the other era rules (buildings.era).

Overture's buildings have no year (start_date is one of the OSM tags it drops), but each keeps its OSM way or relation
(src_record "w34633854@12"). tools/build_osm_building_years.py cuts the dated buildings of OSM US's Layercake buildings
file (ODbL; 17.4 M of them, 2026-11-14) into zoom-12 tiles in PMTiles parts, as the road tag shards are
(osmtagshards.py: the same reader, YearShards); a tile is a few KB (Manhattan's 1,700 dated buildings: 5 KB).

A road tile asks for the year tiles under it as it starts (Fetch, on a thread); its buildings wait WAIT_S at most and
take the year of their way (key: 2 x id, + 1 for a relation), unless they have one (3DBAG's).
- Not published yet (the manifest a 404), off (BNE_OSMYEARS=off, BNE_RAILTAGS=off), or a manifest this cannot read:
  no years, nothing faulted (the buildings as before).
- A tile or the manifest that could not be had (offline, the host failing): no years and the build faulted
  (revisit.fault: built again later, with them).

    BNE_OSMYEARS_BASE   the dataset's folder elsewhere than <BNE_DATA_PAGES>/osmyears (a staged build)

Data (c) OpenStreetMap contributors, ODbL 1.0 (Layercake: OpenStreetMap US).
"""
import logging
import os
import re
import threading
import time

import numpy as np

import fast
import netfetch
import osmtagshards

log = logging.getLogger("earthd.osmyears")

ON = os.environ.get("BNE_OSMYEARS", "on") != "off"
BASE = os.environ.get("BNE_OSMYEARS_BASE")
NATIVE = 298                    # earthfast.VERSION of oby_decode
WAIT_S = 10.0                   # s a tile's buildings wait for its year tiles at most
CELL_ZOOM = 14                  # (the tile's map cells, each mapped to the shards' zoom)

_EMPTY = (np.zeros(0, np.uint64), np.zeros(0, np.int16))


class YearShards(osmtagshards.TagShards):
    NAME, FORMAT, NATIVE, LABEL = "osmyears", 1, NATIVE, "osm years shards"

    def _on(self):
        return ON

    def _base(self):
        return BASE

    def _valid(self, m):
        return True

    def _decode(self, b, m):
        return fast._ef.oby_decode(b)

    def usable(self):
        """manifest()'s (build, manifest); False: none to read (off, not published: the manifest a 404, or one this
        cannot read); None: not had (unreachable, or a tile failed a moment ago: DOWN_S)."""
        got = self.manifest()
        if got is not None:
            return got
        import railtags
        if not self._on() or self.fetcher is None or not railtags.ON or self.pages.dir is None:
            return False
        if time.monotonic() - self._down < osmtagshards.DOWN_S:
            return None
        if self.pages.version() is None:
            return False if getattr(self.pages, "missing", False) else None
        return False


_SHARDS = {}
_LOCK = threading.Lock()
_POOL = None


def shards(md):
    """The year shards of map data md (one per cache folder and fetcher), None when off or md has no fetcher."""
    fetcher = getattr(md, "fetcher", None)
    cache_dir = getattr(md, "cache_dir", None)
    if not ON or fetcher is None or not isinstance(cache_dir, str) or not cache_dir:
        return None
    k = (cache_dir, id(fetcher), BASE)
    with _LOCK:
        got = _SHARDS.get(k)
        if got is None:
            got = _SHARDS[k] = YearShards(cache_dir, fetcher)
        return got


def key_of(record):
    """The year tiles' key of an Overture source record ("w123@4" -> 246, "r7@1" -> 15), else None."""
    m = re.match(r"^([wr])(\d+)", record or "")
    return 2 * int(m.group(2)) + (1 if m.group(1) == "r" else 0) if m else None


def tiles_of(sh, lat0, lon0, lat1, lon1):
    """(keys ascending, years) of the year tiles over a lat / lon box; None: not had (a fault); _EMPTY: none in use."""
    import mapcells
    got = sh.usable()
    if not got:
        return _EMPTY if got is False else None
    z = got[1]["zoom"]
    sh_ = max(0, CELL_ZOOM - z)
    tiles = sorted({(c.x >> sh_, c.y >> sh_) for c in mapcells.cells_for_bbox(lat0, lon0, lat1, lon1, z=CELL_ZOOM)})
    ks, ys = [], []
    for x, y in tiles:
        try:
            t = sh.tile(mapcells.CellKey(z, x, y))
        except osmtagshards.NotInUse:
            return None
        except Exception as e:      # (offline, limits, the host failing, a damaged tile: built again later)
            log.debug("osm years tile %d/%d/%d: %s: %s", z, x, y, type(e).__name__, e)
            if not isinstance(e, netfetch.Deferred):
                sh.down(e)              # (not asked again for DOWN_S: the tiles meanwhile fault at once, no waits)
            return None
        if t is not None:
            ks.append(np.asarray(t[0], np.uint64))
            ys.append(np.asarray(t[1], np.int16))
    if not ks:
        return _EMPTY
    k, y = np.concatenate(ks), np.concatenate(ys)
    k, first = np.unique(k, return_index=True)          # (a building on a tile edge is in both: one year)
    return k, y[first]


class Fetch:
    """A tile's year tiles asked for on a thread as the tile starts; years() waits WAIT_S at most."""

    def __init__(self, frame, x0, y0, x1, y1, md):
        global _POOL
        self.fut = None
        try:
            sh = shards(md)
        except Exception as e:          # (no native module at 298, a cache folder it cannot use: no years)
            log.warning("osm years: %s: %s", type(e).__name__, e)
            return
        if sh is None:
            return
        la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1], np.float64),
                                         np.array([y0, y0, y1, y1], np.float64), 0.0)
        box = (float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo)))
        with _LOCK:
            if _POOL is None:
                from concurrent.futures import ThreadPoolExecutor
                _POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="osmyears")
        self.fut = _POOL.submit(tiles_of, sh, *box)

    def years(self):
        """Years (keys, years) of the tile's buildings, or None (none in use; one not had faults the tile)."""
        if self.fut is None:
            return None
        try:
            got = self.fut.result(timeout=WAIT_S)
        except Exception as e:
            log.debug("osm years: %s: %s", type(e).__name__, e)
            got = None
        if got is None:
            import revisit
            revisit.fault()             # (a later visit builds it with the years)
            return None
        return got if len(got[0]) else None


def lookup(years, records):
    """The years (int, 0 where none) of src_record strings records, from years (Fetch.years's)."""
    out = np.zeros(len(records), np.int64)
    if years is None or not len(records):
        return out
    keys, ys = years
    if not len(keys):
        return out
    q = np.array([key_of(str(r)) if r is not None else None for r in records], object)
    ok = np.array([v is not None for v in q], bool)
    if not ok.any():
        return out
    qk = np.array([int(v) for v in q[ok]], np.uint64)
    j = np.minimum(np.searchsorted(keys, qk), len(keys) - 1)
    hit = keys[j] == qk
    sub = np.zeros(len(qk), np.int64)
    sub[hit] = ys[j[hit]]
    out[ok] = sub
    return out
