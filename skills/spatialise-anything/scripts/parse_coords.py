#!/usr/bin/env python3
"""Find coordinates in text and turn them into contract features.

Formats:
  * decimal degrees, signed or with hemisphere letters, labelled or bare
  * degrees-minutes-seconds and degrees-decimal-minutes, with hemisphere
  * UTM / MGA grid references (zone from the reference, its line, or --zone)
  * MGRS, 1 to 5 digit precision
  * WKT and EWKT (SRID=...;POINT(...))

Every detection records its character span, the datum it stated (or the one
assumed, flagged), and a positional uncertainty derived from the precision
written in the source. GDA94 is transformed to GDA2020; AGD66/AGD84 are not
converted and are flagged with a wide uncertainty instead.

Usage:
    parse_coords.py FILE|- [--doc NAME] [--page N] [--zone 55] [--datum GDA2020]
                    [--frame AU|minlon,minlat,maxlon,maxlat] [--format features|detections]
    parse_coords.py --text "Site at -37.8136, 144.9631 (GDA2020)"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contract as C  # noqa: E402
import geodesy as G  # noqa: E402

RESOLVER = C.resolver("parse_coords", C.CONTRACT_VERSION)
M_PER_DEG = 111320.0

FRAMES = {
    # Australia including Tasmania and the Torres Strait, not external territories.
    "AU": (112.0, -44.5, 154.5, -9.0),
}

# ------------------------------------------------------------------ datums

_DATUM_PATTERNS = [
    (re.compile(r"\b(?:GDA|MGA)\s?-?2020\b|\bMGA20\b|\bGDA2020\b", re.I), "GDA2020"),
    (re.compile(r"\b(?:GDA|MGA)\s?-?94\b", re.I), "GDA94"),
    (re.compile(r"\bWGS\s?-?84\b", re.I), "WGS84"),
    (re.compile(r"\b(?:AGD|AMG)\s?-?66\b", re.I), "AGD66"),
    (re.compile(r"\b(?:AGD|AMG)\s?-?84\b", re.I), "AGD84"),
    (re.compile(r"\b(?:NAD\s?-?(?:27|83)|ED\s?-?50|OSGB\s?-?36)\b", re.I), "OTHER"),
]

_GEOG_EPSG = {"WGS84": 4326, "GDA2020": 7844, "GDA94": 4283, "AGD66": 4202, "AGD84": 4203}


def _grid_epsg(datum: str, zone: int, south: bool) -> str:
    if datum == "GDA2020":
        return f"EPSG:{7800 + zone}"
    if datum == "GDA94":
        return f"EPSG:{28300 + zone}"
    if datum == "AGD66":
        return f"EPSG:{20200 + zone}"
    if datum == "AGD84":
        return f"EPSG:{20300 + zone}"
    return f"EPSG:{(32700 if south else 32600) + zone}"


def find_datum(text: str, start: int, end: int, default: str | None) -> tuple[str | None, bool]:
    """Datum stated inside the mention, else nearest within its line (up to
    120 chars before or 40 after), else the document default. Returns
    (datum, stated)."""
    inside = text[start:end]
    for pat, name in _DATUM_PATTERNS:
        if pat.search(inside):
            return name, True
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end == -1 else line_end
    before = text[max(line_start, start - 120):start]
    after = text[end:min(line_end, end + 40)]
    best = None
    for pat, name in _DATUM_PATTERNS:
        for m in pat.finditer(before):
            dist = len(before) - m.end()
            if best is None or dist < best[0]:
                best = (dist, name)
        m = pat.search(after)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), name)
    if best:
        return best[1], True
    if default:
        return default, True
    return None, False


# ------------------------------------------------------------------ helpers

def _num(s: str) -> float:
    return float(s.replace("−", "-").replace("+", ""))


def _decimals(s: str) -> int:
    return len(s.split(".")[1]) if "." in s else 0


def _deg_precision_m(decimals: int) -> float:
    return 0.5 * 10 ** (-decimals) * M_PER_DEG


def _in_frame(lon: float, lat: float, frame) -> bool | None:
    if not frame:
        return None
    x0, y0, x1, y1 = frame
    return x0 <= lon <= x1 and y0 <= lat <= y1


_CONTEXT_WORDS = re.compile(
    r"\b(?:coord(?:inate)?s?|gps|position|located|location|lat(?:itude)?|lon(?:g(?:itude)?)?|lng|"
    r"fix|waypoint|wpt|site|decimal degrees|dd|centroid|point)\b", re.I)
_NOT_COORD_BEFORE = re.compile(
    r"\b(?:budget|cost|price|total|amount|rate|ratio|version|release|build|rev(?:ision)?|score|scores|"
    r"grade|weight|height|width|length|depth|size|volume|qty|quantity|line item|odds|factor|index)\b[^.\n]{0,25}$", re.I)
_UNIT_AFTER = re.compile(r"^\s?(?:mm|cm|km|m|kg|g|t|%|ml|l|kn|kt|ft|in|hrs?|mins?|secs?|s|v|kv|kw|mw|mpa|kpa|ha)\b", re.I)
_CURRENCY_BEFORE = re.compile(r"(?:\$|AUD|USD|€|£)\s?$", re.I)


class Detection(dict):
    """A detection is a dict; this class only gives it a readable repr."""


def _det(kind, text, s, e, **kw) -> Detection:
    d = Detection(kind=kind, mention=text[s:e], char_start=s, char_end=e)
    d.update(kw)
    d.setdefault("warnings", [])
    d.setdefault("flags", [])
    return d


# ------------------------------------------------------------------ WKT

_WKT = re.compile(
    r"(?:SRID=(?P<srid>\d{4,5});\s*)?\b(?P<type>POINT|LINESTRING|POLYGON|MULTIPOINT|MULTILINESTRING|MULTIPOLYGON)"
    r"\s*(?:Z|M|ZM)?\s*\(", re.I)


def _balanced_end(text: str, open_idx: int) -> int | None:
    depth = 0
    for i in range(open_idx, min(len(text), open_idx + 200000)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _parse_wkt_body(gtype: str, body: str):
    def pos_list(s):
        pts = []
        for chunk in s.split(","):
            nums = chunk.strip().split()
            if len(nums) < 2:
                raise ValueError("bad position")
            pts.append([float(nums[0]), float(nums[1])])
        return pts

    def rings(s):
        return [pos_list(r) for r in re.findall(r"\(([^()]*)\)", s)]

    inner = body.strip()[1:-1].strip()
    t = gtype.upper()
    if t == "POINT":
        return {"type": "Point", "coordinates": pos_list(inner)[0]}
    if t == "LINESTRING":
        return {"type": "LineString", "coordinates": pos_list(inner)}
    if t == "MULTIPOINT":
        return {"type": "MultiPoint", "coordinates": pos_list(inner.replace("(", "").replace(")", ""))}
    if t == "POLYGON":
        return {"type": "Polygon", "coordinates": rings(inner)}
    if t == "MULTILINESTRING":
        return {"type": "MultiLineString", "coordinates": rings(inner)}
    if t == "MULTIPOLYGON":
        polys = re.findall(r"\(((?:\s*\([^()]*\)\s*,?)+)\)", inner)
        return {"type": "MultiPolygon", "coordinates": [rings(p) for p in polys]}
    raise ValueError(t)


def _walk_positions(coords):
    if coords and isinstance(coords[0], (int, float)):
        yield coords
    else:
        for c in coords:
            yield from _walk_positions(c)


def find_wkt(text):
    out = []
    for m in _WKT.finditer(text):
        open_idx = m.end() - 1
        end = _balanced_end(text, open_idx)
        if end is None:
            continue
        try:
            geom = _parse_wkt_body(m.group("type"), text[open_idx:end])
        except (ValueError, IndexError):
            continue
        srid = m.group("srid")
        d = _det("wkt", text, m.start(), end, geometry=geom, srid=int(srid) if srid else None)
        body = text[open_idx:end]
        nums = re.findall(r"-?\d+(?:\.\d+)?", body)
        d["decimals"] = min((_decimals(n) for n in nums), default=0)
        out.append(d)
    return out


# ------------------------------------------------------------------ MGRS

_MGRS = re.compile(
    r"(?<![A-Za-z0-9])(?P<zone>[1-9]|[1-5]\d|60)\s?(?P<band>[C-HJ-NP-X])\s?(?P<col>[A-HJ-NP-Z])(?P<row>[A-HJ-NP-V])"
    r"(?:\s(?P<e>\d{1,5})\s(?P<n>\d{1,5})|\s?(?P<digits>\d{2,10}))?(?![A-Za-z0-9])")


def find_mgrs(text):
    out = []
    for m in _MGRS.finditer(text):
        zone = int(m.group("zone"))
        if m.group("digits"):
            dg = m.group("digits")
            if len(dg) % 2:
                continue
            e, n = dg[: len(dg) // 2], dg[len(dg) // 2:]
        elif m.group("e"):
            e, n = m.group("e"), m.group("n")
            if len(e) != len(n):
                continue
        else:
            continue  # a bare grid square ("55HCU") is too easy to mistake for a code
        try:
            easting, northing, south, prec = G.mgrs_to_utm(zone, m.group("band"), m.group("col"), m.group("row"), e, n)
            lon, lat = G.utm_to_geographic(easting, northing, zone, south)
        except ValueError:
            continue
        out.append(_det("mgrs", text, m.start(), m.end(), lon=lon, lat=lat, zone=zone,
                        precision_m=prec, source_datum="WGS84", datum_by_convention=True))
    return out


# ------------------------------------------------------------------ grid (UTM / MGA)

_ZONE_CTX = re.compile(
    r"(?:\bzone\s*|\b(?:MGA|UTM|AMG)(?:\s?-?(?:94|2020|20|66|84))?\s*(?:zone\s*)?)(?P<zone>\d{1,2})"
    r"(?:\s?(?P<band>[C-HJ-NP-X])\b)?(?!\d)", re.I)
_E = r"(?P<e>\d{6}(?:\.\d+)?)"
_N = r"(?P<n>\d{7}(?:\.\d+)?)"
_GRID_PATTERNS = [
    # E 273456 N 5918234 / Easting: 273456, Northing: 5918234
    re.compile(r"\b(?:E|Easting|East)\s*[:=]?\s*" + _E + r"\s*(?:m|mE)?\s*[,;/]?\s*(?:and\s+)?"
               r"(?:N|Northing|North)\s*[:=]?\s*" + _N + r"(?:\s*(?:m|mN))?\b", re.I),
    # N 5918234 E 273456
    re.compile(r"\b(?:N|Northing|North)\s*[:=]?\s*" + _N + r"\s*(?:m|mN)?\s*[,;/]?\s*(?:and\s+)?"
               r"(?:E|Easting|East)\s*[:=]?\s*" + _E + r"(?:\s*(?:m|mE))?\b", re.I),
    # 273456mE 5918234mN / 273456E 5918234N
    re.compile(r"(?<![\d.])" + _E + r"\s?m?E\s*[,;/]?\s*" + _N + r"\s?m?N\b"),
    # 55H 273456 5918234 (UTM with band) / zone 55 273456 5918234
    re.compile(r"(?<![A-Za-z0-9])(?P<zone>[1-9]|[1-5]\d|60)\s?(?P<band>[C-HJ-NP-X])\s+" + _E +
               r"\s*(?:mE|E)?\s*[,;/]?\s*" + _N + r"(?:\s*(?:mN|N))?(?![\d.])"),
    re.compile(r"\bzone\s*(?P<zone>[1-9]|[1-5]\d|60)\s*[,:;]?\s+" + _E + r"\s*(?:mE|E)?\s*[,;/]?\s*" + _N +
               r"(?:\s*(?:mN|N))?(?![\d.])", re.I),
]


def find_grid(text, default_zone=None, south_default=True):
    out = []
    taken = []
    for pat in _GRID_PATTERNS:
        for m in pat.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            gd = m.groupdict()
            e_s, n_s = gd["e"], gd["n"]
            zone = int(gd["zone"]) if gd.get("zone") else None
            band = gd.get("band")
            zone_src = "mention" if zone else None
            if zone is None:
                line_start = text.rfind("\n", 0, m.start()) + 1
                ctx = text[max(line_start, m.start() - 100):m.start()]
                zms = list(_ZONE_CTX.finditer(ctx))
                after_end = text.find("\n", m.end())
                after_end = len(text) if after_end == -1 else after_end
                zm_after = _ZONE_CTX.search(text[m.end():min(after_end, m.end() + 40)])
                cand = zms[-1] if zms else zm_after
                if cand and 1 <= int(cand.group("zone")) <= 60:
                    zone = int(cand.group("zone"))
                    band = cand.group("band")
                    zone_src = "context"
                elif default_zone:
                    zone = int(default_zone)
                    zone_src = "document_default"
            south = south_default if not band else band.upper() < "N"
            d = _det("grid", text, m.start(), m.end(), easting=_num(e_s), northing=_num(n_s),
                     zone=zone, zone_source=zone_src, south=south,
                     decimals=min(_decimals(e_s), _decimals(n_s)))
            if zone is None:
                d["status"] = "unresolved"
                d["warnings"].append("grid reference has no zone in the mention, its line, or --zone")
            else:
                lon, lat = G.utm_to_geographic(d["easting"], d["northing"], zone, south)
                d["lon"], d["lat"] = lon, lat
            out.append(d)
            taken.append((m.start(), m.end()))
    return out


# ------------------------------------------------------------------ DMS / hemisphere components

_DEG_MARK = r"(?:°|º|˚|\s?deg(?:rees)?\.?)"
_MIN_MARK = r"(?:'|′|’|‘|\s?min(?:utes)?\.?)"
_SEC_MARK = r"(?:\"|″|”|“|''|’’|\s?sec(?:onds)?\.?)"
_COMPONENT = re.compile(
    r"(?<![A-Za-z0-9.])(?:(?P<pre>[NSEW])\s?)?"
    r"(?:"
    r"(?P<deg>\d{1,3}(?:\.\d+)?)\s?" + _DEG_MARK +
    r"(?:\s?(?P<min>\d{1,2}(?:\.\d+)?)\s?" + _MIN_MARK +
    r"(?:\s?(?P<sec>\d{1,2}(?:\.\d+)?)\s?" + _SEC_MARK + r")?)?"
    r"|(?P<cdeg>\d{1,3}):(?P<cmin>\d{2}(?:\.\d+)?)(?::(?P<csec>\d{2}(?:\.\d+)?))?"
    r"|(?P<ddeg>\d{1,3}\.\d+)"
    r")"
    r"\s?(?P<post>[NSEW])?(?![A-Za-z0-9])")


def _components(text):
    comps = []
    for m in _COMPONENT.finditer(text):
        pre, post = m.group("pre"), m.group("post")
        if bool(pre) == bool(post):
            continue  # need exactly one hemisphere letter
        hemi = pre or post
        if m.group("deg") is not None:
            deg, mn, sc = m.group("deg"), m.group("min"), m.group("sec")
        elif m.group("cdeg") is not None:
            deg, mn, sc = m.group("cdeg"), m.group("cmin"), m.group("csec")
        else:
            deg, mn, sc = m.group("ddeg"), None, None
        dv = float(deg)
        mv = float(mn) if mn else 0.0
        sv = float(sc) if sc else 0.0
        if mv >= 60 or sv >= 60:
            continue
        value = dv + mv / 60 + sv / 3600
        limit = 90 if hemi in "NS" else 180
        if value > limit:
            continue
        if sc:
            prec = 0.5 * 10 ** (-_decimals(sc)) / 3600 * M_PER_DEG
        elif mn:
            prec = 0.5 * 10 ** (-_decimals(mn)) / 60 * M_PER_DEG
        else:
            prec = _deg_precision_m(_decimals(deg))
        sign = -1 if hemi in "SW" else 1
        comps.append(dict(start=m.start(), end=m.end(), hemi=hemi, value=sign * value,
                          precision_m=prec, style="dms" if (mn or sc) else "dd_hemi"))
    return comps


_PAIR_GAP = re.compile(r"^\s*(?:[,;/]|and|\s)?\s*$", re.I)


def find_hemisphere_pairs(text):
    comps = _components(text)
    out = []
    i = 0
    while i < len(comps) - 1:
        a, b = comps[i], comps[i + 1]
        gap = text[a["end"]:b["start"]]
        kinds = {a["hemi"] in "NS", b["hemi"] in "NS"}
        if len(gap) <= 8 and _PAIR_GAP.match(gap) and kinds == {True, False}:
            lat_c, lon_c = (a, b) if a["hemi"] in "NS" else (b, a)
            d = _det(a["style"] if a["style"] == b["style"] else "dms", text, a["start"], b["end"],
                     lon=lon_c["value"], lat=lat_c["value"],
                     precision_m=max(a["precision_m"], b["precision_m"]))
            out.append(d)
            i += 2
        else:
            i += 1
    return out


# ------------------------------------------------------------------ decimal degrees

_NUM = r"[-+−]?\d{1,3}\.\d+"
_LABELLED = [
    re.compile(r"\blat(?:itude)?\.?\s*[:=]?\s*(?P<lat>" + _NUM + r"|[-+−]?\d{1,2})(?:[ \t]*°)?(?:[ \t]*(?P<lath>[NS])\b)?[\s,;/]*"
               r"(?:and\s+)?\b(?:lon(?:g(?:itude)?)?|lng)\.?\s*[:=]?\s*(?P<lon>" + _NUM + r"|[-+−]?\d{1,3})(?:[ \t]*°)?(?:[ \t]*(?P<lonh>[EW])\b)?",
               re.I),
    re.compile(r"\b(?:lon(?:g(?:itude)?)?|lng)\.?\s*[:=]?\s*(?P<lon>" + _NUM + r"|[-+−]?\d{1,3})(?:[ \t]*°)?(?:[ \t]*(?P<lonh>[EW])\b)?[\s,;/]*"
               r"(?:and\s+)?\blat(?:itude)?\.?\s*[:=]?\s*(?P<lat>" + _NUM + r"|[-+−]?\d{1,2})(?:[ \t]*°)?(?:[ \t]*(?P<lath>[NS])\b)?",
               re.I),
]
_BARE_PAIR = re.compile(
    r"(?<![\w.\-+−/:])(?P<a>" + _NUM + r")(?:[ \t]*°)?[ \t]*(?:,|;|[ \t])[ \t]*(?P<b>" + _NUM + r")(?:[ \t]*°)?(?![\w.]|\d)")


def find_decimal(text, frame=None):
    out = []
    taken = []
    for pat in _LABELLED:
        for m in pat.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            lat, lon = _num(m.group("lat")), _num(m.group("lon"))
            if m.group("lath") == "S" and lat > 0:
                lat = -lat
            if m.group("lonh") == "W" and lon > 0:
                lon = -lon
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            dec = min(_decimals(m.group("lat")), _decimals(m.group("lon")))
            out.append(_det("dd_labelled", text, m.start(), m.end(), lon=lon, lat=lat,
                            precision_m=_deg_precision_m(dec)))
            taken.append((m.start(), m.end()))
    for m in _BARE_PAIR.finditer(text):
        if any(m.start() < e and s < m.end() for s, e in taken):
            continue
        a_s, b_s = m.group("a"), m.group("b")
        a, b = _num(a_s), _num(b_s)
        dec = min(_decimals(a_s), _decimals(b_s))
        before = text[max(0, m.start() - 40):m.start()]
        context = bool(_CONTEXT_WORDS.search(before))
        if _UNIT_AFTER.match(text[m.end():m.end() + 6]) or _CURRENCY_BEFORE.search(text[max(0, m.start() - 4):m.start()]):
            continue
        if dec < 3 and not context:
            continue  # "1.25, 3.50" is far more often not a coordinate
        if not context and _NOT_COORD_BEFORE.search(before):
            continue  # "Budget line 1.250, 3.750"
        warnings = []
        if abs(a) <= 90 and abs(b) <= 180:
            lat, lon, swapped = a, b, False
        elif abs(b) <= 90 and abs(a) <= 180:
            lat, lon, swapped = b, a, True
        else:
            continue
        order_certain = abs(lon) > 90 or (abs(a) > 90 or abs(b) > 90)
        if not order_certain and frame:
            fits = _in_frame(lon, lat, frame)
            fits_swapped = _in_frame(lat, lon, frame)
            if fits_swapped and not fits:
                lat, lon, swapped = lon, lat, not swapped
                order_certain = True
            elif fits and not fits_swapped:
                order_certain = True
        if swapped:
            warnings.append("values read as longitude, latitude")
        d = _det("dd", text, m.start(), m.end(), lon=lon, lat=lat, precision_m=_deg_precision_m(dec),
                 context=context, decimals=dec, order_certain=order_certain, warnings=warnings)
        out.append(d)
    return out


# ------------------------------------------------------------------ assemble

def detect(text: str, default_zone=None, default_datum=None, frame=None, south_default=True):
    """Run every detector, keep non-overlapping detections (most specific
    first), then attach datum, CRS, uncertainty and confidence."""
    ordered = []
    for finder in (find_wkt, find_mgrs, lambda t: find_grid(t, default_zone, south_default),
                   find_hemisphere_pairs, lambda t: find_decimal(t, frame)):
        for d in finder(text):
            if any(d["char_start"] < o["char_end"] and o["char_start"] < d["char_end"] for o in ordered):
                continue
            ordered.append(d)
    ordered.sort(key=lambda d: d["char_start"])
    for d in ordered:
        _finish(d, text, default_datum, frame)
    return ordered


def _finish(d, text, default_datum, frame):
    kind = d["kind"]
    flags, warns = d["flags"], d["warnings"]
    if d.get("status") == "unresolved":
        d["source_crs"] = "unknown"
        d["confidence"] = 0.0
        return
    datum, stated = find_datum(text, d["char_start"], d["char_end"], default_datum)
    if kind == "mgrs":
        datum, stated = "WGS84", True  # MGRS is defined on WGS84
    if kind == "wkt" and d.get("srid"):
        datum, stated = _datum_from_srid(d["srid"]), True
    if datum is None:
        line_start = text.rfind("\n", 0, d["char_start"]) + 1
        same_line = text[max(line_start, d["char_start"] - 100):d["char_end"]]
        datum = "GDA2020" if (kind == "grid" and re.search(r"\bMGA", same_line, re.I)) else "WGS84"
        stated = False
        flags.append("datum_assumed")
        warns.append(f"no datum stated; assumed {datum}")

    is_place, right_place = 0.97, 0.99
    unc_extra = 0.0
    if not stated:
        unc_extra += 2.0  # WGS84 vs GDA94 is ~1.8 m in Australia
    if datum in ("AGD66", "AGD84", "OTHER"):
        flags.append("datum_unsupported")
        warns.append(f"{datum} is not converted; position may be ~200 m out")
        unc_extra += 250.0

    if kind == "wkt":
        geom = d["geometry"]
        pos = list(_walk_positions(geom["coordinates"]))
        if any(abs(p[0]) > 180 or abs(p[1]) > 90 for p in pos):
            srid = d.get("srid")
            zone = _zone_from_srid(srid) if srid else None
            if zone is None:
                d["status"] = "unresolved"
                warns.append("WKT has projected coordinates and no SRID the parser knows")
                d["source_crs"] = "unknown"
                d["confidence"] = 0.0
                return
            for p in pos:
                lon, lat = G.utm_to_geographic(p[0], p[1], zone, True)
                if datum == "GDA94":
                    lon, lat = G.gda94_to_gda2020(lon, lat)
                p[0], p[1] = round(lon, 7), round(lat, 7)
            d["precision_m"] = 0.5 * 10 ** (-d["decimals"])
        else:
            if any(abs(p[0]) <= 90 and abs(p[1]) > 90 for p in pos):
                warns.append("WKT positions look like lat,lon; WKT is x y (lon lat) - check the source")
                is_place = 0.6
            if datum == "GDA94":
                for p in pos:
                    p[0], p[1] = (round(v, 7) for v in G.gda94_to_gda2020(p[0], p[1]))
            d["precision_m"] = _deg_precision_m(d["decimals"])
        d["source_crs"] = f"EPSG:{d['srid']}" if d.get("srid") else _geog_crs(datum, stated)
        d["uncertainty_m"] = max(1.0, d["precision_m"]) + unc_extra
        is_place = min(is_place, 0.98)
    elif kind == "grid":
        if d.get("status") == "unresolved":
            d["source_crs"] = "unknown"
            d["confidence"] = 0.0
            return
        if d["zone_source"] == "document_default":
            is_place = 0.85
            warns.append(f"zone {d['zone']} taken from the document default")
        if datum == "GDA94":
            d["lon"], d["lat"] = G.gda94_to_gda2020(d["lon"], d["lat"])
        epsg = _grid_epsg(datum, d["zone"], d["south"])
        d["source_crs"] = epsg + ("" if stated else " (assumed)")
        d["precision_m"] = 0.5 * 10 ** (-d["decimals"])
        d["uncertainty_m"] = max(1.0, d["precision_m"]) + unc_extra
        d["geometry"] = C.point(d["lon"], d["lat"])
        if not 49 <= d["zone"] <= 56 and datum in ("GDA2020", "GDA94"):
            warns.append(f"zone {d['zone']} is outside MGA zones 49-56")
    else:
        if kind == "mgrs":
            d["source_crs"] = "EPSG:4326 (MGRS)"
            d["uncertainty_m"] = d["precision_m"] * 0.7071
            is_place = 0.95
        else:
            if datum == "GDA94":
                d["lon"], d["lat"] = G.gda94_to_gda2020(d["lon"], d["lat"])
            d["source_crs"] = _geog_crs(datum, stated)
            d["uncertainty_m"] = max(1.0, d["precision_m"]) + unc_extra
            if kind == "dd":
                is_place = 0.9 if d["decimals"] >= 4 else 0.85
                if d["context"]:
                    is_place = min(0.97, is_place + 0.05)
                if not d["order_certain"]:
                    right_place = 0.6
                    warns.append("latitude/longitude order cannot be told from the values; pass --frame")
        d["geometry"] = C.point(d["lon"], d["lat"])
        if d["uncertainty_m"] > 50:
            flags.append("precision_limited")

    # Frame check: outside the document's region drops confidence below the
    # threshold rather than dropping the feature.
    if frame and d.get("geometry"):
        rep = list(_walk_positions(d["geometry"]["coordinates"]))[0]
        if not _in_frame(rep[0], rep[1], frame):
            flags.append("outside_frame")
            warns.append("position falls outside the document frame")
            right_place = min(right_place, 0.5)
    d["confidence"] = min(is_place, right_place)
    d["status"] = "resolved" if d["confidence"] >= C.DEFAULT_THRESHOLD else "ambiguous"


def _geog_crs(datum, stated):
    if datum in ("GDA2020", "WGS84", "GDA94", "AGD66", "AGD84"):
        return f"EPSG:{_GEOG_EPSG[datum]}" + ("" if stated else " (assumed)")
    return "unknown"


def _datum_from_srid(srid):
    if srid in (7844,) or 7849 <= srid <= 7856:
        return "GDA2020"
    if srid in (4283,) or 28349 <= srid <= 28356:
        return "GDA94"
    if srid == 4326 or 32601 <= srid <= 32760:
        return "WGS84"
    return "OTHER"


def _zone_from_srid(srid):
    for base in (7800, 28300, 32700, 20200, 20300):
        if 1 <= srid - base <= 60:
            return srid - base
    return None


def to_feature(d, doc, page=None, line_offset=0):
    status = d.get("status", "resolved")
    method = "coord_parse"
    label = None
    if d.get("geometry") and d["geometry"]["type"] == "Point":
        lon, lat = d["geometry"]["coordinates"]
        label = f"{abs(lat):.5f}°{'S' if lat < 0 else 'N'}, {abs(lon):.5f}°{'W' if lon < 0 else 'E'}"
    elif d.get("geometry"):
        label = f"{d['geometry']['type']} from WKT"
    candidates = []
    if status == "ambiguous" and d.get("geometry"):
        # Keep the parsed position as a candidate so a reviewer can accept it.
        candidates.append({"label": label or d["mention"], "rejected": "; ".join(d["warnings"]) or "below threshold",
                           "confidence": round(d["confidence"], 3), "geometry": d["geometry"]})
        if d["kind"] == "dd" and not d.get("order_certain", True):
            lon, lat = d["geometry"]["coordinates"]
            if abs(lon) <= 90:
                candidates.append({"label": "read as longitude, latitude", "rejected": "order not determinable",
                                   "geometry": C.point(lat, lon)})
    return C.make_feature(
        geometry=d.get("geometry"),
        label=label,
        mention=d["mention"],
        source=C.make_source(doc, page=page, char_start=d["char_start"] + line_offset,
                             char_end=d["char_end"] + line_offset),
        tier="explicit",
        method=method,
        resolver=RESOLVER,
        confidence=d.get("confidence", 0.0),
        uncertainty_m=d.get("uncertainty_m"),
        status=status,
        candidates=candidates,
        source_crs=d.get("source_crs", "unknown"),
        flags=d.get("flags", []),
    )


def parse_frame(s):
    if not s:
        return None
    if s.upper() in FRAMES:
        return FRAMES[s.upper()]
    parts = [float(x) for x in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("frame is AU or minlon,minlat,maxlon,maxlat")
    return tuple(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", help="text file, or - for stdin")
    ap.add_argument("--text", help="parse this string instead of a file")
    ap.add_argument("--doc", help="source document name (default: the file name)")
    ap.add_argument("--page", type=int)
    ap.add_argument("--zone", type=int, help="document-level UTM/MGA zone for grid refs with none stated")
    ap.add_argument("--datum", choices=["GDA2020", "GDA94", "WGS84", "AGD66", "AGD84"],
                    help="document-level datum, when the document states one once for all coordinates")
    ap.add_argument("--frame", type=parse_frame, help="AU or minlon,minlat,maxlon,maxlat")
    ap.add_argument("--north", action="store_true", help="grid refs without a band are northern hemisphere")
    ap.add_argument("--format", choices=["features", "detections"], default="features")
    a = ap.parse_args(argv)

    if a.text is not None:
        text, doc = a.text, a.doc or "inline"
    elif a.path in (None, "-"):
        text, doc = sys.stdin.read(), a.doc or "stdin"
    else:
        text, doc = Path(a.path).read_text(encoding="utf-8", errors="replace"), a.doc or Path(a.path).name

    dets = detect(text, a.zone, a.datum, a.frame, south_default=not a.north)
    if a.format == "detections":
        print(json.dumps(dets, indent=2, ensure_ascii=False, default=float))
        return 0
    feats = [to_feature(d, doc, a.page) for d in dets]
    C.dump(C.make_collection(feats, inputs=[doc], threshold=C.DEFAULT_THRESHOLD), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
