#!/usr/bin/env python3
"""Construct relative geometry from an anchor: geodesic offsets, buffers and
sectors, in metres on the ellipsoid.

The words decide the shape (see references/geometry.md):
  "200 m north of X"     -> offset   (point; uncertainty grows with distance
                                       and with how coarse the bearing was)
  "within 2 km of X"     -> buffer   (polygon)
  "north of X"           -> sector   (wedge polygon; give --max)
  "between X and Y"      -> line     (straight geodesic, a stand-in only:
                                       the MCP server's trace_segment follows
                                       the real road or river)

Usage:
    build_geometry.py offset --from 144.452,-37.247 --bearing NE --distance "5 km" [--anchor-uncertainty 1500]
    build_geometry.py buffer --from 144.452,-37.247 --radius "2 km"
    build_geometry.py sector --from 144.452,-37.247 --bearing N --spread 45 --max "10 km" [--min "0 m"]
    build_geometry.py line   --points 144.452,-37.247 144.217,-37.064
    build_geometry.py parse  --bearing "north-north-east" --distance "1.5 nautical miles"

Prints JSON: {"geometry": ..., "uncertainty_m": ..., "method": "constructed", "notes": [...]}.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import geodesy as G  # noqa: E402

_POINTS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
_WORDS = {"north": "N", "south": "S", "east": "E", "west": "W"}

_UNITS = {
    "m": 1.0, "metre": 1.0, "metres": 1.0, "meter": 1.0, "meters": 1.0,
    "km": 1000.0, "kms": 1000.0, "kilometre": 1000.0, "kilometres": 1000.0, "kilometer": 1000.0, "kilometers": 1000.0,
    "mi": 1609.344, "mile": 1609.344, "miles": 1609.344,
    "nm": 1852.0, "nmi": 1852.0, "nautical mile": 1852.0, "nautical miles": 1852.0,
    "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
    "yd": 0.9144, "yard": 0.9144, "yards": 0.9144,
}


def parse_bearing(s: str) -> tuple[float, float]:
    """Return (bearing degrees, half-width of the implied sector degrees).

    Compass words carry their own vagueness: "north" means anywhere within
    +/-22.5 deg of north (8-point), "NNE" +/-11.25 (16-point). Numeric bearings
    are taken as +/-2 deg. Quadrant bearings like N45E are supported."""
    t = s.strip().lower().replace("–", "-")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:°|deg(?:rees)?)?\s*(?:t|true|m|mag)?", t)
    if m:
        return float(m.group(1)) % 360, 2.0
    m = re.fullmatch(r"([ns])\s*(\d+(?:\.\d+)?)\s*°?\s*([ew])", t)
    if m:
        ang = float(m.group(2))
        ns, ew = m.group(1), m.group(3)
        b = {("n", "e"): ang, ("s", "e"): 180 - ang, ("s", "w"): 180 + ang, ("n", "w"): 360 - ang}[(ns, ew)]
        return b % 360, 2.0
    for w, c in _WORDS.items():
        t = t.replace(w, c.lower())
    t = re.sub(r"[\s\-_]|ward(s)?|erly|ern|of", "", t).upper()
    if t in _POINTS:
        idx = _POINTS.index(t)
        half = 22.5 if len(t) <= 2 else 11.25
        return idx * 22.5, half
    raise ValueError(f"cannot read bearing {s!r}")


def parse_distance(s: str) -> float:
    t = s.strip().lower().replace(",", "")
    m = re.fullmatch(r"(?:about|approx\.?|approximately|around|~)?\s*(\d+(?:\.\d+)?)\s*([a-z ]+?)\s*", t)
    if not m:
        raise ValueError(f"cannot read distance {s!r}")
    unit = m.group(2).strip()
    if unit not in _UNITS:
        raise ValueError(f"unknown distance unit {unit!r}")
    return float(m.group(1)) * _UNITS[unit]


def _pt(s: str) -> tuple[float, float]:
    lon, lat = (float(x) for x in s.split(","))
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError("points are lon,lat")
    return lon, lat


def _r(v: float) -> float:
    return round(v, 7)


def offset(anchor, bearing_s, distance_s, anchor_unc=0.0, distance_exact=False):
    brg, half = parse_bearing(bearing_s)
    d = parse_distance(distance_s)
    lon, lat = G.destination(anchor[0], anchor[1], brg, d)
    along = d * (0.05 if distance_exact else 0.25)
    lateral = d * math.sin(math.radians(half))
    unc = anchor_unc + math.hypot(along, lateral)
    notes = [f"bearing {brg:.1f} deg +/- {half} deg, distance {d:.0f} m "
             f"({'stated exactly' if distance_exact else 'taken as approximate, +/-25%'})"]
    return {"geometry": {"type": "Point", "coordinates": [_r(lon), _r(lat)]},
            "uncertainty_m": round(unc, 1), "method": "constructed", "notes": notes}


def _ring_close(coords):
    if coords[0] != coords[-1]:
        coords.append(list(coords[0]))
    return coords


def buffer(anchor, radius_s, anchor_unc=0.0, segments=72):
    r = parse_distance(radius_s)
    ring = []
    for k in range(segments):
        lon, lat = G.destination(anchor[0], anchor[1], 360.0 * k / segments, r)
        ring.append([_r(lon), _r(lat)])
    # GeoJSON exterior rings run anticlockwise: destination() walks clockwise.
    ring.reverse()
    return {"geometry": {"type": "Polygon", "coordinates": [_ring_close(ring)]},
            "uncertainty_m": round(anchor_unc, 1), "method": "constructed",
            "notes": [f"geodesic buffer, radius {r:.0f} m, {segments} segments"]}


def sector(anchor, bearing_s, max_s, min_s="0 m", spread=None, anchor_unc=0.0, step_deg=3.0):
    brg, half = parse_bearing(bearing_s)
    if spread is not None:
        half = float(spread) / 2
    rmax = parse_distance(max_s)
    rmin = parse_distance(min_s)
    if rmin >= rmax:
        raise ValueError("min must be less than max")
    a0, a1 = brg - half, brg + half
    n = max(2, int(math.ceil((a1 - a0) / step_deg)))
    outer = [G.destination(anchor[0], anchor[1], a0 + (a1 - a0) * k / n, rmax) for k in range(n + 1)]
    if rmin > 0:
        inner = [G.destination(anchor[0], anchor[1], a1 - (a1 - a0) * k / n, rmin) for k in range(n + 1)]
    else:
        inner = [anchor]
    ring = [[_r(x), _r(y)] for x, y in outer + inner]
    ring.reverse()  # anticlockwise exterior
    return {"geometry": {"type": "Polygon", "coordinates": [_ring_close(ring)]},
            "uncertainty_m": round(anchor_unc, 1), "method": "constructed",
            "notes": [f"sector {a0 % 360:.1f}-{a1 % 360:.1f} deg, {rmin:.0f}-{rmax:.0f} m"]}


def line(points, anchor_unc=0.0, densify_m=1000.0):
    coords = []
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        dist, brg = G.inverse(x0, y0, x1, y1)
        n = max(1, int(dist // densify_m))
        for k in range(n):
            if k == 0:
                coords.append([_r(x0), _r(y0)])
                continue
            # Walk along the geodesic by re-solving from the start each step.
            lon, lat = G.destination(x0, y0, brg, dist * k / n)
            coords.append([_r(lon), _r(lat)])
    coords.append([_r(points[-1][0]), _r(points[-1][1])])
    total = sum(G.inverse(a[0], a[1], b[0], b[1])[0] for a, b in zip(points, points[1:]))
    return {"geometry": {"type": "LineString", "coordinates": coords},
            "uncertainty_m": round(anchor_unc + 0.1 * total, 1), "method": "constructed",
            "notes": ["straight geodesic between anchors - a stand-in until trace_segment follows the "
                      "real road or river; uncertainty set to 10% of length"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("offset")
    o.add_argument("--from", dest="anchor", type=_pt, required=True)
    o.add_argument("--bearing", required=True)
    o.add_argument("--distance", required=True)
    o.add_argument("--exact", action="store_true", help="the distance was surveyed, not estimated")
    o.add_argument("--anchor-uncertainty", type=float, default=0.0)
    b = sub.add_parser("buffer")
    b.add_argument("--from", dest="anchor", type=_pt, required=True)
    b.add_argument("--radius", required=True)
    b.add_argument("--segments", type=int, default=72)
    b.add_argument("--anchor-uncertainty", type=float, default=0.0)
    s = sub.add_parser("sector")
    s.add_argument("--from", dest="anchor", type=_pt, required=True)
    s.add_argument("--bearing", required=True)
    s.add_argument("--max", required=True)
    s.add_argument("--min", default="0 m")
    s.add_argument("--spread", type=float, help="full angular width in degrees (default from the bearing word)")
    s.add_argument("--anchor-uncertainty", type=float, default=0.0)
    ln = sub.add_parser("line")
    ln.add_argument("--points", type=_pt, nargs="+", required=True)
    ln.add_argument("--anchor-uncertainty", type=float, default=0.0)
    p = sub.add_parser("parse")
    p.add_argument("--bearing")
    p.add_argument("--distance")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "offset":
            out = offset(a.anchor, a.bearing, a.distance, a.anchor_uncertainty, a.exact)
        elif a.cmd == "buffer":
            out = buffer(a.anchor, a.radius, a.anchor_uncertainty, a.segments)
        elif a.cmd == "sector":
            out = sector(a.anchor, a.bearing, a.max, a.min, a.spread, a.anchor_uncertainty)
        elif a.cmd == "line":
            if len(a.points) < 2:
                raise ValueError("a line needs at least two points")
            out = line(a.points, a.anchor_uncertainty)
        else:
            out = {}
            if a.bearing:
                brg, half = parse_bearing(a.bearing)
                out["bearing_deg"], out["half_width_deg"] = brg, half
            if a.distance:
                out["distance_m"] = parse_distance(a.distance)
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        return 2
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
