#!/usr/bin/env python3
"""Build skills/spatialise-anything/data/gazetteer.sqlite from ABS ASGS
Edition 3 digital boundary files (CC BY 4.0).

Inputs (GDA2020 shapefiles, unzipped into one directory):
    SAL_2021_AUST_GDA2020.shp   Suburbs and Localities 2021
    POA_2021_AUST_GDA2020.shp   Postal Areas 2021
    LGA_2025_AUST_GDA2020.shp   Local Government Areas 2025

Download from:
    https://www.abs.gov.au/statistics/standards/australian-statistical-geography-standard-asgs/
    edition-3-july-2021-june-2026/access-and-downloads/digital-boundary-files

Build-time dependencies (not needed by the skill): pyshp, shapely.

    pip install pyshp shapely
    python tools/build_gazetteer.py --src /path/to/abs --out skills/spatialise-anything/data/gazetteer.sqlite
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

import shapefile  # pyshp
from shapely.geometry import mapping, shape
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "spatialise-anything" / "scripts"))
from gazetteer import STATE_BY_NAME, encode_geometry, normalise  # noqa: E402

LAYERS = [
    # kind, file stem, code field, name field, state field, area field, tolerance (deg)
    ("lga", "LGA_2025_AUST_GDA2020", "LGA_CODE25", "LGA_NAME25", "STE_NAME21", "AREASQKM", 0.004),
    ("locality", "SAL_2021_AUST_GDA2020", "SAL_CODE21", "SAL_NAME21", "STE_NAME21", "AREASQKM21", 0.002),
    ("postcode", "POA_2021_AUST_GDA2020", "POA_CODE21", "POA_NAME21", None, "AREASQKM21", 0.002),
]
SOURCES = {
    "lga": "ABS ASGS Edition 3, Local Government Areas 2025 (GDA2020)",
    "locality": "ABS ASGS Edition 3, Suburbs and Localities 2021 (GDA2020)",
    "postcode": "ABS ASGS Edition 3, Postal Areas 2021 (GDA2020)",
}
SKIP = re.compile(r"no usual address|migratory|offshore|shipping|outside australia", re.I)


def _round_coords(obj, nd=4):
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            return [round(obj[0], nd), round(obj[1], nd)]
        return [_round_coords(o, nd) for o in obj]
    return obj


def _orient(geom):
    """RFC 7946: exterior rings anticlockwise, holes clockwise."""
    from shapely.geometry import MultiPolygon, Polygon
    from shapely.geometry.polygon import orient
    if isinstance(geom, Polygon):
        return orient(geom, 1.0)
    if isinstance(geom, MultiPolygon):
        return MultiPolygon([orient(p, 1.0) for p in geom.geoms])
    return geom


def _drop_specks(geom, min_deg2=2e-6):
    """Drop polygon parts smaller than ~0.02 km2 (rocks, sandbars, slivers),
    always keeping the largest part. They cost more bytes than they are worth
    at this simplification."""
    from shapely.geometry import MultiPolygon
    if not isinstance(geom, MultiPolygon):
        return geom
    parts = sorted(geom.geoms, key=lambda g: -g.area)
    keep = [parts[0]] + [g for g in parts[1:] if g.area >= min_deg2]
    return keep[0] if len(keep) == 1 else MultiPolygon(keep)


def _split_name(name: str) -> tuple[str, str | None]:
    m = re.match(r"^(.*?)\s*\((.+)\)\s*$", name)
    return (m.group(1), m.group(2)) if m else (name, None)


def build(src: Path, out: Path) -> dict:
    if out.exists():
        out.unlink()
    db = sqlite3.connect(out)
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE places (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,          -- locality | postcode | lga
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            qualifier TEXT,              -- ABS disambiguator, e.g. "Mackay - Qld"
            name_norm TEXT NOT NULL,
            state TEXT,                  -- NSW, VIC, ... (postcodes: from the label point)
            lga TEXT,                    -- LGA containing the label point (localities, postcodes)
            lon REAL NOT NULL, lat REAL NOT NULL,   -- label point: centroid, or a point inside if the centroid falls out
            area_km2 REAL,
            radius_m REAL,               -- radius of a circle of the same area: a positional uncertainty for the label point
            minx REAL, miny REAL, maxx REAL, maxy REAL,
            geom BLOB                    -- simplified (Multi)Polygon, see gazetteer.encode_geometry
        );
    """)
    counts = {}
    lga_index = None
    lga_records = []
    for kind, stem, code_f, name_f, state_f, area_f, tol in LAYERS:
        reader = shapefile.Reader(str(src / stem))
        rows = []
        for sr in reader.iterShapeRecords():
            rec = sr.record.as_dict()
            name = str(rec[name_f]).strip()
            if SKIP.search(name) or not sr.shape.points:
                continue
            geom = shape(sr.shape.__geo_interface__)
            if geom.is_empty:
                continue
            if not geom.is_valid:
                geom = geom.buffer(0)
            simple = _drop_specks(geom.simplify(tol, preserve_topology=True))
            if simple.is_empty:
                simple = geom
            simple = _orient(simple)
            pt = geom.centroid
            if not geom.contains(pt):
                pt = geom.representative_point()
            base, qual = _split_name(name)
            state = STATE_BY_NAME.get(str(rec[state_f]).lower()) if state_f else None
            area = float(rec[area_f] or 0.0)
            lga = None
            if kind != "lga" and lga_index is not None:
                for i in lga_index.query(pt):
                    if lga_records[i][1].contains(pt):
                        lga = lga_records[i][0]
                        if state is None:
                            state = lga_records[i][2]
                        break
            minx, miny, maxx, maxy = simple.bounds
            gj = mapping(simple)
            blob = encode_geometry({"type": gj["type"], "coordinates": _round_coords(gj["coordinates"])})
            rows.append((kind, str(rec[code_f]), base, qual, normalise(name), state, lga,
                         round(pt.x, 6), round(pt.y, 6), round(area, 4),
                         round(math.sqrt(area / math.pi) * 1000, 0) if area else None,
                         minx, miny, maxx, maxy, blob))
            if kind == "lga":
                lga_records.append((base, geom, state))
        db.executemany("INSERT INTO places (kind, code, name, qualifier, name_norm, state, lga, lon, lat, area_km2, "
                       "radius_m, minx, miny, maxx, maxy, geom) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        counts[kind] = len(rows)
        if kind == "lga":
            lga_index = STRtree([g for _, g, _ in lga_records])
        print(f"{kind}: {len(rows)}", file=sys.stderr)
    db.executescript("""
        CREATE INDEX idx_name ON places(name_norm, kind);
        CREATE INDEX idx_code ON places(kind, code);
        CREATE INDEX idx_bbox ON places(kind, minx, maxx, miny, maxy);
    """)
    version = date.today().strftime("%Y-%m")
    meta = {
        "version": version,
        "built": date.today().isoformat(),
        "crs": "EPSG:7844 (GDA2020), treated as EPSG:4326 for output",
        "sources": json.dumps(SOURCES),
        "licence": "CC BY 4.0 - https://creativecommons.org/licenses/by/4.0/",
        "attribution": "Source: Australian Bureau of Statistics, Australian Statistical Geography Standard (ASGS) "
                       "Edition 3, licensed under CC BY 4.0. Boundaries simplified by Spatialise Anything.",
        "simplification_deg": json.dumps({k: t for k, *_, t in LAYERS}),
        "notes": "Postal Areas approximate Australia Post postcodes. Label points are centroids, or a point "
                 "inside the polygon where the centroid falls outside. radius_m is the equal-area radius.",
        "counts": json.dumps(counts),
    }
    db.executemany("INSERT INTO meta VALUES (?, ?)", list(meta.items()))
    db.commit()
    db.execute("VACUUM")
    db.close()
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, required=True, help="directory holding the unzipped ABS shapefiles")
    ap.add_argument("--out", type=Path, default=ROOT / "skills" / "spatialise-anything" / "data" / "gazetteer.sqlite")
    a = ap.parse_args(argv)
    counts = build(a.src, a.out)
    print(json.dumps({"out": str(a.out), "bytes": a.out.stat().st_size, "counts": counts}, indent=2))


if __name__ == "__main__":
    main()
