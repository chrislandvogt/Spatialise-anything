#!/usr/bin/env python3
"""Find the location columns in a table, by header and by value pattern.

Reads CSV/TSV with the standard library, and XLSX when openpyxl is
available. For every column it reports the most likely role, the evidence
for it, and a confidence; then it proposes how the table resolves (which
tier, which columns). With --rows it also emits one mention per row, built
from the location columns, ready for parse_coords.py or gazetteer.py.

Roles: lat, lon, easting, northing, zone, coord_pair, mgrs, wkt, address,
street, locality, state, postcode, lga, sa2, asset_id, name.

Usage:
    sniff_table.py assets.csv [--sheet Sheet1] [--rows] [--max-rows 5000]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

HEADER_HINTS = {
    "lat": r"^(lat|latitude|y_?coord|lat_?dd|gps_?lat|decimal_?lat.*|point_?y)$",
    "lon": r"^(lon|long|lng|longitude|x_?coord|lon_?dd|long_?dd|gps_?lon[g]?|decimal_?lon.*|point_?x)$",
    "easting": r"^(e|east|easting|mga_?e(asting)?|x_?mga|amg_?e(asting)?)$",
    "northing": r"^(n|north|northing|mga_?n(orthing)?|y_?mga|amg_?n(orthing)?)$",
    "zone": r"^(zone|utm_?zone|mga_?zone|z)$",
    "coord_pair": r"^(coords?|coordinates|location|latlon|lat_?long|lat/long|position|gps)$",
    "mgrs": r"^(mgrs|grid_?ref|military_?grid)$",
    "wkt": r"^(wkt|geom|geometry|shape|the_?geom)$",
    "address": r"^(address|full_?address|site_?address|street_?address|location_?address|addr)$",
    "street": r"^(street|street_?name|road|road_?name|address_?line_?1)$",
    "locality": r"^(suburb|locality|town|city|place|suburb_?town)$",
    "state": r"^(state|st|state_?code|jurisdiction)$",
    "postcode": r"^(postcode|post_?code|pcode|zip|postal_?code)$",
    "lga": r"^(lga|lga_?name|council|municipality|shire|lga_?code)$",
    "sa2": r"^(sa2|sa2_?code|sa2_?name|sa2_?main.*)$",
    "asset_id": r"^(asset_?id|asset|asset_?no|asset_?number|site_?id|facility_?id|gis_?id|equipment_?id)$",
    "name": r"^(name|site_?name|site|facility|facility_?name|description)$",
}
_STREET_TYPES = (r"st|street|rd|road|ave|avenue|dr|drive|ct|court|pl|place|cres|crescent|hwy|highway|pde|parade|"
                 r"tce|terrace|ln|lane|blvd|boulevard|way|cl|close|gr|grove|esp|esplanade|cct|circuit|fwy|freeway")
VALUE_TESTS = {
    "coord_pair": re.compile(r"^\s*[-+]?\d{1,3}\.\d{3,}\s*[, ]\s*[-+]?\d{1,3}\.\d{3,}\s*$"),
    "mgrs": re.compile(r"^\s*\d{1,2}[C-HJ-NP-X]\s?[A-HJ-NP-Z][A-HJ-NP-V]\s?(\d{2,10}|\d{1,5}\s\d{1,5})\s*$"),
    "wkt": re.compile(r"^\s*(SRID=\d+;)?\s*(POINT|LINESTRING|POLYGON|MULTI\w+)\s*\(", re.I),
    "address": re.compile(r"^\s*(?:(?:unit|lot|shop)\s*\w+[,/ ]\s*)?\d+[A-Za-z]?(?:-\d+)?\s+[A-Za-z'\- ]+\s(?:" +
                          _STREET_TYPES + r")\b", re.I),
    "postcode": re.compile(r"^\s*(0[89]\d{2}|[1-9]\d{3})\s*$"),
    "state": re.compile(r"^\s*(NSW|VIC|QLD|SA|WA|TAS|NT|ACT|New South Wales|Victoria|Queensland|South Australia|"
                        r"Western Australia|Tasmania|Northern Territory|Australian Capital Territory)\s*$", re.I),
    "sa2": re.compile(r"^\s*[1-9]\d{8}\s*$"),
}


def _num(v):
    try:
        return float(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def read_table(path: Path, sheet: str | None = None, max_rows: int = 5000):
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        try:
            import openpyxl  # optional
        except ImportError:
            raise SystemExit("openpyxl is not installed: save the sheet as CSV and run again")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else f"col{i + 1}" for i, h in enumerate(next(it))]
        rows = []
        for i, r in enumerate(it):
            if i >= max_rows:
                break
            rows.append(["" if v is None else str(v) for v in r])
        return header, rows, ws.title
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    header = [h.strip() for h in next(reader)]
    rows = []
    for i, r in enumerate(reader):
        if i >= max_rows:
            break
        rows.append(r)
    return header, rows, None


def _key(h: str) -> str:
    return re.sub(r"[^a-z0-9/]+", "_", h.lower()).strip("_")


def score_column(header: str, values: list[str]) -> list[tuple[str, float, str]]:
    """Return [(role, confidence, evidence)] sorted best first."""
    key = _key(header)
    vals = [v for v in values if str(v).strip()]
    n = len(vals) or 1
    out = {}

    def add(role, conf, why):
        if conf > out.get(role, (0, ""))[0]:
            out[role] = (conf, why)

    for role, pat in HEADER_HINTS.items():
        if re.match(pat, key):
            add(role, 0.6, f"header {header!r}")

    nums = [_num(v) for v in vals]
    numeric = [x for x in nums if x is not None]
    frac = len(numeric) / n
    if numeric and frac > 0.9:
        has_dec = sum(1 for v in vals if "." in str(v)) / n
        if all(-90 <= x <= 90 for x in numeric) and has_dec > 0.8:
            add("lat", 0.45, "decimals within +/-90")
        if all(-180 <= x <= 180 for x in numeric) and has_dec > 0.8 and any(abs(x) > 90 for x in numeric):
            add("lon", 0.55, "decimals with |value| > 90")
        if all(100000 <= x < 1000000 for x in numeric):
            add("easting", 0.45, "6-digit values")
        if all(1000000 <= x < 10000000 for x in numeric):
            add("northing", 0.45, "7-digit values")
        if all(float(x).is_integer() and 1 <= x <= 60 for x in numeric) and len(set(numeric)) <= 3:
            add("zone", 0.4, "small set of integers 1-60")
    for role, pat in VALUE_TESTS.items():
        hits = sum(1 for v in vals if pat.match(str(v)))
        if hits / n >= 0.6:
            add(role, 0.35 + 0.35 * hits / n, f"{hits}/{len(vals)} values look like {role}")

    # Header and values agreeing is stronger than either alone.
    ranked = []
    for role, (conf, why) in out.items():
        header_hit = bool(re.match(HEADER_HINTS.get(role, r"$^"), key))
        value_hit = why.startswith(("decimals", "6-digit", "7-digit", "small set")) or "look like" in why
        if header_hit and value_hit:
            conf = min(0.98, conf + 0.35)
            why = f"header {header!r} and values agree"
        elif header_hit and not vals:
            conf = 0.5
        ranked.append((role, round(conf, 2), why))
    ranked.sort(key=lambda t: -t[1])
    return ranked


def sniff(path: Path, sheet=None, max_rows=5000) -> dict:
    header, rows, sheet_name = read_table(path, sheet, max_rows)
    cols = []
    for i, h in enumerate(header):
        values = [r[i] if i < len(r) else "" for r in rows]
        ranked = score_column(h, values)
        best = ranked[0] if ranked and ranked[0][1] >= 0.4 else None
        cols.append({
            "index": i, "name": h,
            "role": best[0] if best else None,
            "confidence": best[1] if best else 0.0,
            "evidence": best[2] if best else None,
            "alternatives": [{"role": r, "confidence": c} for r, c, _ in ranked[1:3]],
            "empty": sum(1 for v in values if not str(v).strip()),
            "sample": [v for v in values if str(v).strip()][:3],
        })
    _resolve_conflicts(cols)
    roles = {c["role"]: c for c in cols if c["role"]}
    plan = _plan(roles)
    return {"file": path.name, "sheet": sheet_name, "rows": len(rows), "columns": cols, "plan": plan}


def _resolve_conflicts(cols):
    """One column per role: the strongest keeps it; others fall back to
    their next alternative or to no role."""
    by_role = {}
    for c in cols:
        if c["role"]:
            by_role.setdefault(c["role"], []).append(c)
    for role, cs in by_role.items():
        if len(cs) < 2 or role in ("name",):
            continue
        cs.sort(key=lambda c: -c["confidence"])
        for c in cs[1:]:
            c["evidence"] = f"{c['evidence']}; weaker than column {cs[0]['name']!r} for {role}"
            c["role"] = None


def _plan(roles: dict) -> dict:
    if "lat" in roles and "lon" in roles:
        return {"tier": "explicit", "method": "coord_parse", "columns": [roles["lat"]["name"], roles["lon"]["name"]],
                "how": "build 'lat, lon' per row and run parse_coords.py"}
    if "coord_pair" in roles:
        return {"tier": "explicit", "method": "coord_parse", "columns": [roles["coord_pair"]["name"]],
                "how": "run parse_coords.py on each value"}
    if "easting" in roles and "northing" in roles:
        cols = [roles["easting"]["name"], roles["northing"]["name"]] + ([roles["zone"]["name"]] if "zone" in roles else [])
        return {"tier": "explicit", "method": "coord_parse", "columns": cols,
                "how": "build 'zone Z E ... N ...' per row; ask for the zone and datum if no column or header says"}
    if "mgrs" in roles:
        return {"tier": "explicit", "method": "coord_parse", "columns": [roles["mgrs"]["name"]], "how": "MGRS per row"}
    if "wkt" in roles:
        return {"tier": "explicit", "method": "coord_parse", "columns": [roles["wkt"]["name"]], "how": "WKT per row"}
    if "address" in roles or "street" in roles:
        cols = [roles[r]["name"] for r in ("address", "street", "locality", "state", "postcode") if r in roles]
        return {"tier": "structured", "method": "locator", "columns": cols,
                "how": "needs a locator (MCP geocode); offline, fall back to locality/postcode with wide uncertainty"}
    if "postcode" in roles or "lga" in roles or "sa2" in roles:
        cols = [roles[r]["name"] for r in ("postcode", "lga", "sa2") if r in roles]
        return {"tier": "structured", "method": "boundary_join", "columns": cols,
                "how": "gazetteer.py postcode/lga per row (polygons); SA2 needs lookup_boundary"}
    if "locality" in roles:
        cols = [roles[r]["name"] for r in ("locality", "state") if r in roles]
        return {"tier": "named", "method": "gazetteer", "columns": cols, "how": "gazetteer.py search per row, with state"}
    if "asset_id" in roles:
        return {"tier": "structured", "method": "asset_lookup", "columns": [roles["asset_id"]["name"]],
                "how": "needs the customer's asset layer via the MCP server"}
    return {"tier": None, "method": None, "columns": [], "how": "no location columns found"}


def row_mentions(path: Path, report: dict, sheet=None, max_rows=5000) -> list[dict]:
    header, rows, _ = read_table(path, sheet, max_rows)
    idx = {c["name"]: c["index"] for c in report["columns"]}
    role_of = {c["role"]: c["name"] for c in report["columns"] if c["role"]}
    out = []
    for n, r in enumerate(rows, start=2):  # row 1 is the header
        get = lambda role: (r[idx[role_of[role]]].strip() if role in role_of and idx[role_of[role]] < len(r) else "")
        plan = report["plan"]
        text = ""
        rec_method = None
        if plan["columns"] and plan["tier"] == "explicit":
            # Per row, take the first explicit form that is filled in: tables
            # often mix lat/lon rows with grid-reference rows.
            if "lat" in role_of and "lon" in role_of and get("lat") and get("lon"):
                text, rec_method = f"{get('lat')}, {get('lon')}", "lat_lon"
            elif "easting" in role_of and "northing" in role_of and get("easting") and get("northing"):
                z = get("zone")
                text, rec_method = (f"zone {z} " if z else "") + f"E {get('easting')} N {get('northing')}", "grid"
            elif "coord_pair" in role_of and get("coord_pair"):
                text, rec_method = get("coord_pair"), "coord_pair"
            elif "mgrs" in role_of and get("mgrs"):
                text, rec_method = get("mgrs"), "mgrs"
            elif "wkt" in role_of and get("wkt"):
                text, rec_method = get("wkt"), "wkt"
        elif plan["columns"]:
            text = ", ".join(r[idx[c]].strip() for c in plan["columns"] if idx[c] < len(r) and r[idx[c]].strip())
        rec = {"row": n, "mention": text}
        if rec_method:
            rec["form"] = rec_method
        if not text:
            rec["empty"] = True
        if "asset_id" in role_of:
            rec["asset_id"] = get("asset_id")
        if "name" in role_of:
            rec["name"] = get("name")
        out.append(rec)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--sheet")
    ap.add_argument("--rows", action="store_true", help="also emit one mention per row")
    ap.add_argument("--max-rows", type=int, default=5000)
    a = ap.parse_args(argv)
    p = Path(a.path)
    rep = sniff(p, a.sheet, a.max_rows)
    if a.rows:
        rep["row_mentions"] = row_mentions(p, rep, a.sheet, a.max_rows)
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
