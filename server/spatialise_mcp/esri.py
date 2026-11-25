"""Esri JSON <-> GeoJSON, and the small geometry helpers the tools share.

Queries always ask for f=json and convert here. f=geojson is not offered by
every ArcGIS Server version or service type, and one code path is easier to
trust than two.

Ring orientation is the trap. Esri JSON writes exterior rings clockwise and
holes anticlockwise; GeoJSON (RFC 7946) is the reverse. A polygon written
with the wrong winding draws as its own hole in some clients, so both
directions are normalised from the signed area rather than assumed.
"""

from __future__ import annotations

import math
from typing import Any

Position = list[float]
ESRI_TYPE = {"Point": "esriGeometryPoint", "MultiPoint": "esriGeometryMultipoint",
             "LineString": "esriGeometryPolyline", "MultiLineString": "esriGeometryPolyline",
             "Polygon": "esriGeometryPolygon", "MultiPolygon": "esriGeometryPolygon"}
WGS84 = {"wkid": 4326}


def signed_area(ring: list[Position]) -> float:
    """Shoelace area in coordinate units. Positive is anticlockwise."""
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:])) / 2.0


def _closed(ring: list[Position]) -> list[Position]:
    ring = [[float(p[0]), float(p[1])] for p in ring]
    if ring and ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return ring


def ring_contains(ring: list[Position], x: float, y: float) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


# ---------------------------------------------------------------- Esri -> GeoJSON

def to_geojson(geom: dict[str, Any] | None) -> dict[str, Any] | None:
    if not geom:
        return None
    if "x" in geom and "y" in geom:
        if geom["x"] is None or geom["y"] is None or geom["x"] == "NaN":
            return None
        return {"type": "Point", "coordinates": [float(geom["x"]), float(geom["y"])]}
    if "points" in geom:
        return {"type": "MultiPoint", "coordinates": [[float(p[0]), float(p[1])] for p in geom["points"]]}
    if "paths" in geom:
        paths = [[[float(p[0]), float(p[1])] for p in path] for path in geom["paths"] if len(path) >= 2]
        if not paths:
            return None
        if len(paths) == 1:
            return {"type": "LineString", "coordinates": paths[0]}
        return {"type": "MultiLineString", "coordinates": paths}
    if "rings" in geom:
        return _rings_to_geojson(geom["rings"])
    return None


def _rings_to_geojson(rings: list[list[Position]]) -> dict[str, Any] | None:
    rings = [_closed(r) for r in rings]
    rings = [r for r in rings if len(r) >= 4]
    if not rings:
        return None
    exteriors, holes = [], []
    for r in rings:
        (exteriors if signed_area(r) <= 0 else holes).append(r)  # Esri exterior = clockwise
    if not exteriors:  # all rings wound the GeoJSON way: treat them as exteriors
        exteriors, holes = holes, []
    polys = [[list(reversed(ext))] if signed_area(ext) < 0 else [ext] for ext in exteriors]
    for hole in holes:
        x, y = hole[0]
        home = None
        for idx, ext in enumerate(exteriors):
            if ring_contains(ext, x, y):
                # the smallest containing exterior owns the hole
                if home is None or abs(signed_area(ext)) < abs(signed_area(exteriors[home])):
                    home = idx
        if home is None:
            polys.append([list(reversed(hole))] if signed_area(hole) < 0 else [hole])
        else:
            polys[home].append(hole if signed_area(hole) < 0 else list(reversed(hole)))
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


# ---------------------------------------------------------------- GeoJSON -> Esri

def to_esri(geom: dict[str, Any]) -> dict[str, Any]:
    t, c = geom["type"], geom["coordinates"]
    if t == "Point":
        out: dict[str, Any] = {"x": float(c[0]), "y": float(c[1])}
    elif t == "MultiPoint":
        out = {"points": [[float(p[0]), float(p[1])] for p in c]}
    elif t == "LineString":
        out = {"paths": [[[float(p[0]), float(p[1])] for p in c]]}
    elif t == "MultiLineString":
        out = {"paths": [[[float(p[0]), float(p[1])] for p in path] for path in c]}
    elif t in ("Polygon", "MultiPolygon"):
        rings = []
        for poly in ([c] if t == "Polygon" else c):
            for i, ring in enumerate(poly):
                ring = _closed(ring)
                area = signed_area(ring)
                want_clockwise = i == 0  # Esri: exterior clockwise, holes anticlockwise
                if (area > 0) == want_clockwise:
                    ring.reverse()
                rings.append(ring)
        out = {"rings": rings}
    else:
        raise ValueError(f"unsupported geometry type {t!r}")
    out["spatialReference"] = dict(WGS84)
    return out


# ---------------------------------------------------------------- helpers

def positions(geom: dict[str, Any]):
    def walk(c):
        if c and isinstance(c[0], (int, float)):
            yield c
        else:
            for x in c:
                yield from walk(x)
    yield from walk(geom["coordinates"])


def vertex_count(geom: dict[str, Any] | None) -> int:
    return sum(1 for _ in positions(geom)) if geom else 0


def bbox(geom: dict[str, Any]) -> list[float]:
    xs, ys = zip(*((p[0], p[1]) for p in positions(geom)))
    return [min(xs), min(ys), max(xs), max(ys)]


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, h)))


def half_diagonal_m(box: list[float] | dict[str, float] | None) -> float | None:
    if not box:
        return None
    if isinstance(box, dict):
        try:
            box = [box["xmin"], box["ymin"], box["xmax"], box["ymax"]]
        except KeyError:
            return None
    return haversine_m(box[0], box[1], box[2], box[3]) / 2


def _ring_centroid(ring: list[Position]) -> Position:
    # Work relative to the first vertex: with raw longitudes near 144 the
    # shoelace terms cancel badly and cost several digits.
    ox, oy = ring[0][0], ring[0][1]
    local = [[p[0] - ox, p[1] - oy] for p in ring]
    a = signed_area(local)
    if a == 0:
        return [sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)]
    cx = sum((p[0] + q[0]) * (p[0] * q[1] - q[0] * p[1]) for p, q in zip(local, local[1:])) / (6 * a)
    cy = sum((p[1] + q[1]) * (p[0] * q[1] - q[0] * p[1]) for p, q in zip(local, local[1:])) / (6 * a)
    return [cx + ox, cy + oy]


def representative_point(geom: dict[str, Any]) -> Position:
    """A point to label the geometry with: the point itself, a line's middle
    vertex, or the centroid of a polygon's largest part (or a vertex of it,
    when the centroid falls outside)."""
    t, c = geom["type"], geom["coordinates"]
    if t == "Point":
        return [c[0], c[1]]
    if t == "MultiPoint":
        return [c[0][0], c[0][1]]
    if t == "LineString":
        return list(c[len(c) // 2][:2])
    if t == "MultiLineString":
        longest = max(c, key=len)
        return list(longest[len(longest) // 2][:2])
    polys = [c] if t == "Polygon" else c
    big = max(polys, key=lambda p: abs(signed_area(p[0])))
    cen = _ring_centroid(big[0])
    if ring_contains(big[0], cen[0], cen[1]):
        return cen
    return list(big[0][0][:2])


def area_km2(geom: dict[str, Any]) -> float | None:
    """Approximate area for a (Multi)Polygon in lon/lat: planar shoelace scaled
    by the latitude. Good to a per cent or so for anything smaller than a state."""
    if geom["type"] not in ("Polygon", "MultiPolygon"):
        return None
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    total = 0.0
    for poly in polys:
        for i, ring in enumerate(poly):
            lat = sum(p[1] for p in ring) / len(ring)
            k = 111.320 * math.cos(math.radians(lat)) * 110.574
            a = abs(signed_area(ring)) * k
            total += a if i == 0 else -a
    return total


def round_geometry(geom: dict[str, Any], ndigits: int = 6) -> dict[str, Any]:
    def walk(c):
        if c and isinstance(c[0], (int, float)):
            return [round(c[0], ndigits), round(c[1], ndigits)]
        return [walk(x) for x in c]
    return {"type": geom["type"], "coordinates": walk(geom["coordinates"])}


def sql_text(value: str) -> str:
    """A string literal for a where clause. Quotes are doubled; control
    characters are dropped. Field names never come from callers - only from
    the operator's config - so this is the only injection surface."""
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return "'" + cleaned.replace("'", "''") + "'"


def like_text(value: str, prefix: bool, suffix: bool) -> str:
    """A LIKE pattern literal. The caller's own % and _ are removed rather than
    escaped: standardised queries do not accept ESCAPE on every data store."""
    cleaned = "".join(ch for ch in value if ch.isprintable() and ch not in "%_")
    return "'" + ("%" if prefix else "") + cleaned.replace("'", "''") + ("%" if suffix else "") + "'"
