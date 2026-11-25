#!/usr/bin/env python3
"""Fail the run if any feature breaks the contract.

Checks the JSON shape (mirroring schema/feature.schema.json, without needing
the jsonschema package) and the cross-field rules a schema cannot express:

* resolved  <=> geometry present
* resolved   => uncertainty_m present; otherwise uncertainty_m is null
* resolved   => confidence >= threshold; ambiguous => confidence < threshold
* ambiguous  => at least one candidate, so a reviewer has something to pick
* geometry is valid GeoJSON in lon/lat (ranges, closed rings, enough vertices)
* optionally, every mention listed in --mentions has a feature

Usage:
    validate_features.py out.geojson [--threshold 0.7] [--mentions m.json] [--json]

Exit code 0 when valid, 1 when any error is found. Warnings never fail.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contract as C  # noqa: E402

REQUIRED_PROPS = (
    "label", "mention", "source", "tier", "method", "resolver",
    "confidence", "uncertainty_m", "status", "candidates", "source_crs",
)
OPTIONAL_PROPS = ("flags",)
SOURCE_KEYS = {"doc", "page", "sheet", "row", "column", "char_start", "char_end"}
GEOM_TYPES = {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _check_position(p, where, errs):
    if not (isinstance(p, list) and 2 <= len(p) <= 3 and all(_is_num(v) for v in p)):
        errs.append(f"{where}: position must be [lon, lat] numbers, got {p!r}")
        return
    lon, lat = p[0], p[1]
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        errs.append(f"{where}: position {p!r} outside lon/lat range (is it lat,lon or projected?)")


def _check_ring(ring, where, errs):
    if not isinstance(ring, list) or len(ring) < 4:
        errs.append(f"{where}: polygon ring needs at least 4 positions")
        return
    for i, p in enumerate(ring):
        _check_position(p, f"{where}[{i}]", errs)
    if ring[0] != ring[-1]:
        errs.append(f"{where}: polygon ring is not closed")


def check_geometry(g, where, errs):
    if g is None:
        return
    if not isinstance(g, dict) or g.get("type") not in GEOM_TYPES:
        errs.append(f"{where}: geometry type must be one of {sorted(GEOM_TYPES)}")
        return
    t, c = g["type"], g.get("coordinates")
    if not isinstance(c, list):
        errs.append(f"{where}: geometry has no coordinates array")
        return
    if t == "Point":
        _check_position(c, where, errs)
    elif t == "MultiPoint":
        for i, p in enumerate(c):
            _check_position(p, f"{where}[{i}]", errs)
    elif t == "LineString":
        if len(c) < 2:
            errs.append(f"{where}: LineString needs at least 2 positions")
        for i, p in enumerate(c):
            _check_position(p, f"{where}[{i}]", errs)
    elif t == "MultiLineString":
        for j, line in enumerate(c):
            if not isinstance(line, list) or len(line) < 2:
                errs.append(f"{where}[{j}]: LineString needs at least 2 positions")
                continue
            for i, p in enumerate(line):
                _check_position(p, f"{where}[{j}][{i}]", errs)
    elif t == "Polygon":
        if not c:
            errs.append(f"{where}: Polygon has no rings")
        for j, ring in enumerate(c):
            _check_ring(ring, f"{where}[{j}]", errs)
    elif t == "MultiPolygon":
        if not c:
            errs.append(f"{where}: MultiPolygon has no polygons")
        for k, poly in enumerate(c):
            if not isinstance(poly, list) or not poly:
                errs.append(f"{where}[{k}]: Polygon has no rings")
                continue
            for j, ring in enumerate(poly):
                _check_ring(ring, f"{where}[{k}][{j}]", errs)


def check_feature(f, idx, threshold, errs, warns):
    w = f"feature[{idx}]"
    if not isinstance(f, dict) or f.get("type") != "Feature":
        errs.append(f"{w}: not a GeoJSON Feature")
        return
    if "geometry" not in f:
        errs.append(f"{w}: missing geometry member (use null when unresolved)")
    props = f.get("properties")
    if not isinstance(props, dict):
        errs.append(f"{w}: missing properties")
        return
    mention = props.get("mention")
    if isinstance(mention, str):
        w = f"{w} ({mention[:40]!r})"

    for k in REQUIRED_PROPS:
        if k not in props:
            errs.append(f"{w}: missing property {k!r}")
    for k in props:
        if k not in REQUIRED_PROPS and k not in OPTIONAL_PROPS:
            errs.append(f"{w}: unknown property {k!r} (the contract is closed)")

    geom = f.get("geometry")
    check_geometry(geom, f"{w}.geometry", errs)

    label = props.get("label")
    if label is not None and not isinstance(label, str):
        errs.append(f"{w}: label must be a string or null")
    if not isinstance(mention, str) or not mention.strip():
        errs.append(f"{w}: mention must be the non-empty source text")

    src = props.get("source")
    if not isinstance(src, dict) or not isinstance(src.get("doc"), str) or not src.get("doc"):
        errs.append(f"{w}: source.doc is required")
    else:
        for k in src:
            if k not in SOURCE_KEYS:
                errs.append(f"{w}: unknown source key {k!r}")
        for k in ("page", "row"):
            if k in src and not (isinstance(src[k], int) and src[k] >= 1):
                errs.append(f"{w}: source.{k} must be an integer >= 1")
        cs, ce = src.get("char_start"), src.get("char_end")
        for k, v in (("char_start", cs), ("char_end", ce)):
            if v is not None and not (isinstance(v, int) and v >= 0):
                errs.append(f"{w}: source.{k} must be an integer >= 0")
        if isinstance(cs, int) and isinstance(ce, int) and cs > ce:
            errs.append(f"{w}: source.char_start > char_end")

    tier = props.get("tier")
    if tier not in C.TIERS:
        errs.append(f"{w}: tier {tier!r} not in {C.TIERS}")
    method = props.get("method")
    if not isinstance(method, str) or not method:
        errs.append(f"{w}: method is required")
    elif method not in C.METHODS:
        warns.append(f"{w}: method {method!r} is not in the known list; add it to contract.METHODS if intended")
    res = props.get("resolver")
    if not isinstance(res, dict) or not isinstance(res.get("name"), str):
        errs.append(f"{w}: resolver.name is required")

    conf = props.get("confidence")
    if not _is_num(conf) or not 0 <= conf <= 1:
        errs.append(f"{w}: confidence must be a number in [0, 1]")
        conf = None
    unc = props.get("uncertainty_m")
    if unc is not None and (not _is_num(unc) or unc < 0):
        errs.append(f"{w}: uncertainty_m must be a non-negative number or null")

    status = props.get("status")
    if status not in C.STATUSES:
        errs.append(f"{w}: status {status!r} not in {C.STATUSES}")

    cands = props.get("candidates")
    if not isinstance(cands, list):
        errs.append(f"{w}: candidates must be an array (empty is fine)")
        cands = []
    for j, cand in enumerate(cands):
        cw = f"{w}.candidates[{j}]"
        if not isinstance(cand, dict):
            errs.append(f"{cw}: must be an object")
            continue
        if not isinstance(cand.get("label"), str):
            errs.append(f"{cw}: label is required")
        if not isinstance(cand.get("rejected"), str) or not cand.get("rejected").strip():
            errs.append(f"{cw}: rejected must say why this candidate was not chosen")
        if "confidence" in cand and (not _is_num(cand["confidence"]) or not 0 <= cand["confidence"] <= 1):
            errs.append(f"{cw}: confidence must be in [0, 1]")
        if "geometry" in cand:
            check_geometry(cand["geometry"], f"{cw}.geometry", errs)

    crs = props.get("source_crs")
    if not isinstance(crs, str) or not crs:
        errs.append(f"{w}: source_crs is required (state the datum, or the one assumed)")

    flags = props.get("flags", [])
    if not isinstance(flags, list) or not all(isinstance(x, str) for x in flags):
        errs.append(f"{w}: flags must be an array of strings")
        flags = []
    elif len(set(flags)) != len(flags):
        errs.append(f"{w}: flags must be unique")
    for fl in flags:
        if fl not in C.FLAGS:
            warns.append(f"{w}: flag {fl!r} is not in the known list")

    # Cross-field rules.
    if status == "resolved":
        if geom is None:
            errs.append(f"{w}: resolved feature has no geometry")
        if unc is None:
            errs.append(f"{w}: resolved feature needs uncertainty_m")
        if conf is not None and conf < threshold:
            errs.append(f"{w}: resolved with confidence {conf} below threshold {threshold}; "
                        "status should be ambiguous")
    elif status in ("ambiguous", "unresolved"):
        if geom is not None:
            errs.append(f"{w}: {status} feature must have null geometry (no best guesses on the map)")
        if unc is not None:
            errs.append(f"{w}: {status} feature must have null uncertainty_m")
        if status == "ambiguous":
            if conf is not None and conf >= threshold:
                errs.append(f"{w}: ambiguous with confidence {conf} at or above threshold {threshold}")
            if not cands:
                errs.append(f"{w}: ambiguous feature must list its candidates")
    if "datum_assumed" in flags and "assumed" not in str(crs).lower():
        warns.append(f"{w}: datum_assumed flag set; consider noting 'assumed' in source_crs")


def check_mentions(features, mentions, errs):
    """Every listed mention must appear as a feature. A mention entry is a
    string or an object with 'mention' and optionally 'doc'."""
    have = {}
    for f in features:
        p = f.get("properties") or {}
        key = (p.get("mention"), (p.get("source") or {}).get("doc"))
        have[key] = have.get(key, 0) + 1
        have[(p.get("mention"), None)] = have.get((p.get("mention"), None), 0) + 1
    for m in mentions:
        if isinstance(m, str):
            key = (m, None)
        else:
            key = (m.get("mention"), m.get("doc"))
        if not have.get(key):
            errs.append(f"mention {key[0]!r} has no feature (nothing is dropped)")


def validate(fc, threshold=C.DEFAULT_THRESHOLD, mentions=None):
    errs, warns = [], []
    if not isinstance(fc, dict) or fc.get("type") != "FeatureCollection":
        return [f"top level must be a FeatureCollection"], warns
    feats = fc.get("features")
    if not isinstance(feats, list):
        return ["features must be an array"], warns
    meta = fc.get("spatialise")
    if meta is not None:
        if not isinstance(meta, dict) or "contract_version" not in meta:
            errs.append("spatialise member must include contract_version")
        elif meta["contract_version"].split(".")[:2] != C.CONTRACT_VERSION.split(".")[:2]:
            warns.append(f"contract_version {meta['contract_version']} differs from validator {C.CONTRACT_VERSION}")
    for i, f in enumerate(feats):
        check_feature(f, i, threshold, errs, warns)
    if mentions is not None:
        check_mentions(feats, mentions, errs)
    return errs, warns


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="GeoJSON FeatureCollection, or - for stdin")
    ap.add_argument("--threshold", type=float, default=None,
                    help=f"confidence threshold (default: the file's spatialise.threshold, else {C.DEFAULT_THRESHOLD})")
    ap.add_argument("--mentions", help="JSON array of mentions that must each have a feature")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    a = ap.parse_args(argv)

    text = sys.stdin.read() if a.path == "-" else Path(a.path).read_text(encoding="utf-8")
    try:
        fc = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"not valid JSON: {e}", file=sys.stderr)
        return 1
    threshold = a.threshold
    if threshold is None:
        threshold = (fc.get("spatialise") or {}).get("threshold", C.DEFAULT_THRESHOLD)
    mentions = json.loads(Path(a.mentions).read_text(encoding="utf-8")) if a.mentions else None
    errs, warns = validate(fc, threshold, mentions)
    n = len(fc.get("features", [])) if isinstance(fc, dict) else 0
    if a.json:
        print(json.dumps({"valid": not errs, "features": n, "errors": errs, "warnings": warns}, indent=2))
    else:
        for e in errs:
            print(f"ERROR   {e}")
        for x in warns:
            print(f"warning {x}")
        print(f"{'VALID' if not errs else 'INVALID'}: {n} features, {len(errs)} errors, {len(warns)} warnings")
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
