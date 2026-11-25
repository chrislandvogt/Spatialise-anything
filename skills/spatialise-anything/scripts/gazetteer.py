#!/usr/bin/env python3
"""Offline lookup of Australian localities, postcodes and LGAs.

Backed by data/gazetteer.sqlite, built from ABS ASGS Edition 3 boundaries
(CC BY 4.0) by tools/build_gazetteer.py. Every query returns candidates,
never a single answer: the skill decides, using the ranking signals and
reasons each candidate carries.

Ranking signals (see references/disambiguation.md):
  name   exact match 1.0; close spelling 0.6-0.8
  state  candidate in the document's state x1.0, elsewhere x0.05
  near   distance to places already resolved: exp(-d / 100 km), floor 0.05
  kind   locality 1.0, LGA 0.9 unless the text says council/shire/city of
  share  a candidate's score over the sum of all scores
  right_place_hint  min(top share, top name score): a starting value for the
         right-place half of confidence; the skill adjusts it with judgement

Usage:
    gazetteer.py search "Kensington" [--state VIC] [--near 144.92,-37.79 ...] [--kind locality] [--geometry polygon]
    gazetteer.py postcode 3350 [--geometry polygon]
    gazetteer.py lga "City of Ballarat"
    gazetteer.py reverse 143.8503 -37.5622
    gazetteer.py batch queries.json      # {"frame": {...}, "queries": [{"id": .., "text": .., "kind": ..}]}
    gazetteer.py info
"""

from __future__ import annotations

import argparse
import difflib
import json
import math
import re
import sqlite3
import sys
import zlib
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import geodesy as G  # noqa: E402

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "gazetteer.sqlite"

STATES = {
    "NSW": "New South Wales", "VIC": "Victoria", "QLD": "Queensland", "SA": "South Australia",
    "WA": "Western Australia", "TAS": "Tasmania", "NT": "Northern Territory", "ACT": "Australian Capital Territory",
    "OT": "Other Territories",
}
STATE_BY_NAME = {v.lower(): k for k, v in STATES.items()}
_STATE_TOKENS = {
    "nsw": "NSW", "new south wales": "NSW", "vic": "VIC", "vic.": "VIC", "victoria": "VIC", "qld": "QLD",
    "queensland": "QLD", "sa": "SA", "south australia": "SA", "wa": "WA", "western australia": "WA",
    "tas": "TAS", "tas.": "TAS", "tasmania": "TAS", "nt": "NT", "northern territory": "NT", "act": "ACT",
    "australian capital territory": "ACT",
}

_ABBREV = [
    (r"\bmt\b\.?", "mount"), (r"\bpt\b\.?", "point"), (r"\bnth\b\.?", "north"), (r"\bsth\b\.?", "south"),
    (r"^st\b\.?", "saint"), (r"\bck\b\.?", "creek"), (r"\bhts\b\.?", "heights"), (r"\bstn\b\.?", "station"),
]
_LGA_WORDS = re.compile(
    r"^(?:the\s+)?(?:city|shire|rural city|borough|municipality|town|region|district|council)\s+of\s+|"
    r"\s+(?:city|shire|regional|rural city|municipal|town|borough)?\s*council$|\s+(?:shire|city|region)$", re.I)
_LGA_HINT = re.compile(r"\b(?:council|shire|municipality|borough|lga|city of|rural city)\b", re.I)


def normalise(name: str) -> str:
    """Lowercase, expand common abbreviations, drop punctuation. Used for
    both the stored names and every query, so they always agree."""
    t = name.lower().strip()
    t = re.sub(r"\s*\(.*?\)\s*", " ", t)  # ABS qualifiers like "(Vic.)"
    t = t.replace("&", " and ").replace("’", "'")
    t = re.sub(r"['`]", "", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    for pat, rep in _ABBREV:
        t = re.sub(pat, rep, t)
    return re.sub(r"\s+", " ", t).strip()


def split_state(text: str) -> tuple[str, str | None]:
    """'Richmond, VIC 3121' -> ('Richmond', 'VIC'). Trailing postcodes go too."""
    t = re.sub(r"[\s,]+\d{4}\s*$", "", text.strip())
    m = re.search(r"(?:,\s*|\s+)(new south wales|western australia|south australia|northern territory|"
                  r"australian capital territory|victoria|queensland|tasmania|nsw|vic\.?|qld|sa|wa|tas\.?|nt|act)\s*$",
                  t, re.I)
    if m:
        return t[:m.start()].strip(" ,"), _STATE_TOKENS[m.group(1).lower()]
    return t, None


# ----------------------------------------------------------------- db access

class Gazetteer:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(
                f"{self.path} not found - build it with tools/build_gazetteer.py")
        self.db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        self._names = None

    def meta(self) -> dict:
        return {r["key"]: r["value"] for r in self.db.execute("SELECT key, value FROM meta")}

    def version(self) -> str:
        return self.meta().get("version", "unknown")

    def resolver(self) -> dict:
        return {"name": "bundled-gazetteer", "version": self.version()}

    @property
    def names(self) -> list[str]:
        if self._names is None:
            self._names = [r[0] for r in self.db.execute("SELECT DISTINCT name_norm FROM places")]
        return self._names

    def geometry(self, place_id: int) -> dict | None:
        row = self.db.execute("SELECT geom FROM places WHERE id = ?", (place_id,)).fetchone()
        return decode_geometry(row[0]) if row and row[0] else None

    def _rows(self, where: str, args: tuple) -> list[sqlite3.Row]:
        return list(self.db.execute(f"SELECT id, kind, code, name, qualifier, state, lga, lon, lat, "
                                    f"area_km2, radius_m, minx, miny, maxx, maxy FROM places WHERE {where}", args))

    # -------------------------------------------------------------- search

    def search(self, text: str, kind: str | None = None, state: str | None = None,
               near: list[tuple[float, float]] | None = None, limit: int = 10,
               geometry: str = "point", fuzzy: bool = True) -> dict:
        raw = text
        name, stated_state = split_state(text)
        if stated_state:
            state = stated_state
        lga_hint = bool(_LGA_HINT.search(name))
        q = normalise(_LGA_WORDS.sub("", name) if (kind == "lga" or lga_hint) else name)
        if re.fullmatch(r"\d{4}", q):
            return self.by_code("postcode", q, geometry=geometry)

        kinds = (kind,) if kind else ("locality", "lga")
        rows = self._rows(f"name_norm = ? AND kind IN ({','.join('?' * len(kinds))})", (q, *kinds))
        matches = [(r, 1.0, "exact name") for r in rows]
        if fuzzy and not rows:
            close = difflib.get_close_matches(q, self.names, n=5, cutoff=0.85)
            for c in close:
                ratio = difflib.SequenceMatcher(None, q, c).ratio()
                for r in self._rows(f"name_norm = ? AND kind IN ({','.join('?' * len(kinds))})", (c, *kinds)):
                    matches.append((r, round(0.8 * ratio, 3), f"close spelling of {r['name']!r} ({ratio:.2f})"))

        cands = []
        for r, name_score, why in matches:
            reasons = [why]
            score = name_score
            if state:
                if r["state"] == state:
                    reasons.append(f"in {state}, the document's state")
                else:
                    score *= 0.05
                    reasons.append(f"in {r['state']}, not the document's state {state}")
            dist_km = None
            if near:
                dist_km = min(G.haversine(r["lon"], r["lat"], x, y) for x, y in near) / 1000
                prox = max(0.05, math.exp(-dist_km / 100))
                score *= prox
                reasons.append(f"{dist_km:.0f} km from the nearest place already resolved")
            if r["kind"] == "lga":
                if lga_hint:
                    reasons.append("text names a council or shire")
                else:
                    score *= 0.9
                    reasons.append("LGA of the same name; locality preferred unless the text names a council")
            elif lga_hint:
                score *= 0.5
                reasons.append("text names a council or shire, this is a locality")
            cands.append(self._cand(r, score, reasons, dist_km, geometry))
            cands[-1]["name_score"] = name_score

        # A locality and the LGA of the same name that contains it are one place
        # at two granularities, not rivals: the LGA must not dilute the share.
        localities = [c for c in cands if c["kind"] == "locality"]
        for c in cands:
            if c["kind"] == "lga":
                twin = next((l for l in localities if l["lga"] == c["name"] and l["state"] == c["state"]), None)
                if twin is not None:
                    c["nested_in"] = twin["id"]
                    c["reasons"].append(f"the LGA containing {twin['label']}: same place, wider area")
        total = sum(c["score"] for c in cands if "nested_in" not in c) or 1.0
        for c in cands:
            c["share"] = round(c["score"] / total, 3)
        by_id = {c["id"]: c for c in cands}
        for c in cands:
            if "nested_in" in c:
                c["share"] = by_id[c["nested_in"]]["share"]
            c["score"] = round(c["score"], 4)
        cands.sort(key=lambda c: -c["score"])
        rivals = [c for c in cands if "nested_in" not in c]
        top = rivals[0]["share"] if rivals else 0.0
        second = rivals[1]["share"] if len(rivals) > 1 else 0.0
        hint = round(min(top, rivals[0]["name_score"]), 3) if rivals else 0.0
        return {"query": raw, "normalised": q, "state": state, "candidates": cands[:limit],
                "count": len(cands), "top_share": top, "margin": round(top - second, 3),
                "right_place_hint": hint}

    def by_code(self, kind: str, code: str, geometry: str = "point") -> dict:
        kind = {"poa": "postcode", "postcode": "postcode", "lga": "lga", "sal": "locality"}.get(kind, kind)
        rows = self._rows("kind = ? AND code = ?", (kind, str(code)))
        cands = [self._cand(r, 1.0, [f"{kind} code {code}"], None, geometry) for r in rows]
        for c in cands:
            c["share"] = 1.0
        note = None
        if kind == "postcode":
            note = ("ABS postal areas approximate Australia Post postcodes from mesh blocks; "
                    "boundaries can differ from the delivery area at the edges.")
        out = {"query": str(code), "kind": kind, "candidates": cands, "count": len(cands),
               "top_share": 1.0 if cands else 0.0, "margin": 1.0 if cands else 0.0,
               "right_place_hint": 1.0 if cands else 0.0}
        if note:
            out["note"] = note
        return out

    def lga(self, text: str, state: str | None = None, near=None, geometry: str = "point") -> dict:
        if re.fullmatch(r"\d{5}", text.strip()):
            return self.by_code("lga", text.strip(), geometry)
        return self.search(text, kind="lga", state=state, near=near, geometry=geometry)

    def reverse(self, lon: float, lat: float, kinds=("locality", "postcode", "lga")) -> dict:
        out = {}
        for kind in kinds:
            rows = self._rows("kind = ? AND minx <= ? AND maxx >= ? AND miny <= ? AND maxy >= ?",
                              (kind, lon, lon, lat, lat))
            hits = [r for r in rows if point_in_geometry(lon, lat, self.geometry(r["id"]))]
            if hits:
                r = hits[0]
                out[kind] = self._cand(r, 1.0, ["contains the point (simplified boundary)"], None, "point")
        return out

    def _cand(self, r, score, reasons, dist_km, geometry) -> dict:
        label = r["name"]
        if r["kind"] == "postcode":
            label = f"Postcode {r['code']}"
        elif r["state"]:
            label = f"{r['name']}, {r['state']}"
        if r["kind"] == "lga":
            label += " (LGA)"
        c = {"id": f"{r['kind']}:{r['code']}", "kind": r["kind"], "code": r["code"], "name": r["name"],
             "qualifier": r["qualifier"], "state": r["state"], "lga": r["lga"], "label": label,
             "point": [r["lon"], r["lat"]], "radius_m": r["radius_m"], "area_km2": r["area_km2"],
             "score": score, "reasons": reasons}
        if dist_km is not None:
            c["distance_km"] = round(dist_km, 1)
        if geometry == "polygon":
            c["geometry"] = self.geometry(r["id"])
        return c


# ----------------------------------------------------------------- geometry

GEOM_SCALE = 10000  # stored on a 1e-4 degree grid (~11 m), well inside the simplification


def _varints(values):
    out = bytearray()
    for v in values:
        z = (v << 1) ^ (v >> 63)  # zigzag
        while True:
            b = z & 0x7F
            z >>= 7
            if z:
                out.append(b | 0x80)
            else:
                out.append(b)
                break
    return out


def _read_varints(buf):
    vals, z, shift = [], 0, 0
    for b in buf:
        z |= (b & 0x7F) << shift
        if b & 0x80:
            shift += 7
            continue
        vals.append((z >> 1) ^ -(z & 1))
        z, shift = 0, 0
    return vals


def encode_geometry(geom: dict) -> bytes:
    """(Multi)Polygon -> zlib(zigzag varints): polygon count, then per polygon
    the ring count, per ring the point count, then delta-coded x, y."""
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    head, body = [len(polys)], []
    px = py = 0
    for poly in polys:
        head.append(len(poly))
        for ring in poly:
            head.append(len(ring))
            for x, y in ring:
                ix, iy = round(x * GEOM_SCALE), round(y * GEOM_SCALE)
                body += [ix - px, iy - py]
                px, py = ix, iy
    return zlib.compress(bytes(_varints(head + body)), 9)


def decode_geometry(blob: bytes) -> dict:
    vals = _read_varints(zlib.decompress(blob))
    i = 0
    npoly = vals[i]; i += 1
    shape_ = []
    for _ in range(npoly):
        nring = vals[i]; i += 1
        rings = []
        for _ in range(nring):
            rings.append(vals[i]); i += 1
        shape_.append(rings)
    px = py = 0
    polys = []
    for rings in shape_:
        poly = []
        for npts in rings:
            ring = []
            for _ in range(npts):
                px += vals[i]; py += vals[i + 1]; i += 2
                ring.append([px / GEOM_SCALE, py / GEOM_SCALE])
            poly.append(ring)
        polys.append(poly)
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


def _ring_contains(x, y, ring) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def point_in_geometry(x, y, geom) -> bool:
    if not geom:
        return False
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        if poly and _ring_contains(x, y, poly[0]) and not any(_ring_contains(x, y, h) for h in poly[1:]):
            return True
    return False


# ----------------------------------------------------------------- CLI

def _pt(s: str):
    lon, lat = (float(v) for v in s.split(","))
    return lon, lat


@lru_cache(maxsize=1)
def default() -> Gazetteer:
    return Gazetteer()


def run_batch(gz: Gazetteer, spec: dict) -> dict:
    frame = spec.get("frame", {})
    state = frame.get("state")
    near = [tuple(p) for p in frame.get("near", [])]
    out = []
    for q in spec.get("queries", []):
        kind = q.get("kind")
        text = q["text"]
        if kind == "postcode":
            res = gz.by_code("postcode", text, geometry=q.get("geometry", "point"))
        elif kind == "lga":
            res = gz.lga(text, state=q.get("state", state), near=near or None, geometry=q.get("geometry", "point"))
        else:
            res = gz.search(text, kind=kind, state=q.get("state", state), near=near or None,
                            geometry=q.get("geometry", "point"))
        res["id"] = q.get("id")
        out.append(res)
    return {"resolver": gz.resolver(), "results": out}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("text")
    s.add_argument("--kind", choices=["locality", "lga"])
    s.add_argument("--state", choices=sorted(STATES))
    s.add_argument("--near", type=_pt, nargs="*")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--geometry", choices=["point", "polygon"], default="point")
    p = sub.add_parser("postcode")
    p.add_argument("code")
    p.add_argument("--geometry", choices=["point", "polygon"], default="point")
    lg = sub.add_parser("lga")
    lg.add_argument("text")
    lg.add_argument("--state", choices=sorted(STATES))
    lg.add_argument("--geometry", choices=["point", "polygon"], default="point")
    r = sub.add_parser("reverse")
    r.add_argument("lon", type=float)
    r.add_argument("lat", type=float)
    b = sub.add_parser("batch")
    b.add_argument("path", help="JSON file, or - for stdin")
    sub.add_parser("info")
    a = ap.parse_args(argv)

    try:
        gz = Gazetteer(a.db)
    except FileNotFoundError as e:
        print(json.dumps({"error": str(e)}))
        return 2
    if a.cmd == "search":
        out = gz.search(a.text, a.kind, a.state, a.near, a.limit, a.geometry)
    elif a.cmd == "postcode":
        out = gz.by_code("postcode", a.code, a.geometry)
    elif a.cmd == "lga":
        out = gz.lga(a.text, a.state, geometry=a.geometry)
    elif a.cmd == "reverse":
        out = gz.reverse(a.lon, a.lat)
    elif a.cmd == "batch":
        spec = json.loads(sys.stdin.read() if a.path == "-" else Path(a.path).read_text(encoding="utf-8"))
        out = run_batch(gz, spec)
    else:
        out = gz.meta()
        out["counts"] = {k: n for k, n in gz.db.execute("SELECT kind, COUNT(*) FROM places GROUP BY kind")}
    if isinstance(out, dict) and a.cmd != "info":
        out.setdefault("resolver", gz.resolver())
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
