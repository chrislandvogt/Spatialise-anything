#!/usr/bin/env python3
"""Merge script outputs and the skill's decisions into one validated
FeatureCollection.

The skill makes the judgements; this script does the bookkeeping so that
judgement never has to be typed out as raw GeoJSON:

  * FeatureCollections from parse_coords.py and read_exif.py pass through;
  * decisions (a JSON list) are expanded into contract features, pulling
    geometry, label, uncertainty and resolver version from the gazetteer;
  * where two features cover overlapping text in the same source, the
    lower tier wins ("a coordinate beats a place name in the same sentence");
  * the result is validated, and the run fails if anything breaks the
    contract.

Decision forms (one object per mention):

  Gazetteer pick
    {"mention": "the Kensington depot", "source": {"doc": "r.pdf", "page": 4, "char_start": 10, "char_end": 30},
     "tier": "named", "pick": "locality:21327", "geometry": "point" | "polygon",
     "confidence": 0.82, "rejects": {"locality:12107": "document context is metropolitan Melbourne"}}

  Ambiguous (no geometry; candidates carry theirs)
    {"mention": "Kensington", "source": {...}, "tier": "named", "status": "ambiguous", "confidence": 0.2,
     "candidates": {"locality:21327": "no state or nearby place to separate them", "locality:12107": "..."}}

  Constructed (from build_geometry.py output)
    {"mention": "4 km west of Wendouree", "source": {...}, "tier": "relative",
     "constructed": {<build_geometry.py JSON>}, "label": "4 km W of Wendouree, VIC",
     "anchors": ["locality:22745"], "confidence": 0.75}

  Unresolved
    {"mention": "the treatment plant", "source": {...}, "tier": "implicit", "status": "unresolved",
     "flags": ["tier4_disabled"], "method": "not_attempted"}

  Connector pick (a candidate exactly as a Spatialise MCP tool returned it)
    {"mention": "45 Lydiard St North, Ballarat", "source": {...}, "tier": "structured",
     "candidate": {<candidate from geocode / search_gazetteer / lookup_boundary>}, "confidence": 0.9,
     "others": [{"candidate": {<another candidate>}, "why": "wrong town: the report is about Ballarat"}]}
    Geometry, label, uncertainty, resolver and method are taken from the candidate, unchanged.
    Add "geometry": "point" to place an area candidate at its point (uncertainty becomes its radius).

  Connector ambiguous
    {"mention": "Main Street", "source": {...}, "tier": "structured", "status": "ambiguous", "confidence": 0.5,
     "others": [{"candidate": {...}, "why": "equal score, nothing in the text separates them"}, {...}]}

Usage:
    assemble.py --features a.geojson b.geojson --decisions d.json -o out.geojson [--threshold 0.7]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contract as C  # noqa: E402
import validate_features as V  # noqa: E402

_TIER_RANK = {t: i for i, t in enumerate(C.TIERS)}


class _Gz:
    """Lazy gazetteer: only opened when a decision references it."""

    def __init__(self, db=None):
        self.db = db
        self._g = None

    @property
    def g(self):
        if self._g is None:
            import gazetteer
            self._g = gazetteer.Gazetteer(self.db) if self.db else gazetteer.Gazetteer()
        return self._g

    def get(self, ref: str, polygon: bool = False) -> dict:
        kind, code = ref.split(":", 1)
        res = self.g.by_code(kind, code, geometry="polygon" if polygon else "point")
        if not res["candidates"]:
            raise ValueError(f"gazetteer has no {ref}")
        return res["candidates"][0]


def _point(c):
    return C.point(c["point"][0], c["point"][1])


def expand(dec: dict, gz: _Gz, threshold: float) -> dict:
    mention = dec["mention"]
    source = dec["source"] if isinstance(dec.get("source"), dict) else {"doc": dec.get("source", "unknown")}
    tier = dec.get("tier", "named")
    status = dec.get("status")
    flags = list(dec.get("flags", []))
    conf = float(dec.get("confidence", 0.0))

    if status == "unresolved":
        return C.make_feature(geometry=None, label=dec.get("label"), mention=mention, source=source, tier=tier,
                              method=dec.get("method", "not_attempted"),
                              resolver=dec.get("resolver", {"name": "spatialise-skill"}),
                              confidence=conf, uncertainty_m=None, status="unresolved",
                              candidates=dec.get("candidate_list", []), source_crs=dec.get("source_crs", "n/a"),
                              flags=flags)

    if "candidate" in dec or "others" in dec:
        return _from_connector(dec, mention, source, tier, status, flags, conf, threshold)

    if status == "ambiguous" or ("candidates" in dec and "pick" not in dec):
        cands = []
        for ref, why in dec.get("candidates", {}).items():
            c = gz.get(ref)
            cands.append({"id": ref, "label": c["label"], "rejected": why, "geometry": _point(c)})
        return C.make_feature(geometry=None, label=None, mention=mention, source=source, tier=tier,
                              method=dec.get("method", "gazetteer"), resolver=gz.g.resolver(),
                              confidence=min(conf, threshold - 0.001), uncertainty_m=None, status="ambiguous",
                              candidates=cands, source_crs="EPSG:7844", flags=flags)

    if "pick" in dec:
        polygon = dec.get("geometry") == "polygon"
        c = gz.get(dec["pick"], polygon=polygon)
        geom = c["geometry"] if polygon else _point(c)
        unc = dec.get("uncertainty_m", 0.0 if polygon else c["radius_m"])
        rejects = []
        for ref, why in dec.get("rejects", {}).items():
            r = gz.get(ref)
            rejects.append({"id": ref, "label": r["label"], "rejected": why, "geometry": _point(r)})
        method = dec.get("method", "boundary_join" if c["kind"] in ("postcode", "lga") and polygon else "gazetteer")
        st = "resolved" if conf >= threshold else "ambiguous"
        if st == "ambiguous" and not rejects:
            rejects.append({"id": dec["pick"], "label": c["label"], "rejected": "best candidate, but below threshold",
                            "geometry": _point(c)})
        return C.make_feature(geometry=geom, label=dec.get("label", c["label"]), mention=mention, source=source,
                              tier=tier, method=method, resolver=gz.g.resolver(), confidence=conf,
                              uncertainty_m=unc, status=st, candidates=rejects, source_crs="EPSG:7844", flags=flags)

    if "constructed" in dec:
        built = dec["constructed"]
        unc = float(built.get("uncertainty_m", 0.0))
        if dec.get("anchors"):
            anchor_unc = max(gz.get(a)["radius_m"] or 0 for a in dec["anchors"])
            if unc < anchor_unc:
                unc += anchor_unc
                flags.append("anchor_approximate")
        st = "resolved" if conf >= threshold else "ambiguous"
        cands = list(dec.get("rejects_list", []))
        if st == "ambiguous" and not cands:
            cands.append({"label": dec.get("label", mention), "rejected": "constructed, but below threshold",
                          "geometry": built["geometry"]})
        return C.make_feature(geometry=built["geometry"], label=dec.get("label", mention), mention=mention,
                              source=source, tier=tier, method=built.get("method", "constructed"),
                              resolver=dec.get("resolver", {"name": "build_geometry", "version": C.CONTRACT_VERSION}),
                              confidence=conf, uncertainty_m=unc, status=st, candidates=cands,
                              source_crs=dec.get("source_crs", "EPSG:7844"), flags=flags)

    if "feature" in dec:
        return dec["feature"]
    raise ValueError(f"decision for {mention!r} has no pick, candidate, candidates, constructed geometry or status")


def _connector_geometry(cand: dict, as_point: bool) -> tuple[dict, float, list[str]]:
    """Geometry, uncertainty and flags for a candidate a Spatialise MCP tool returned."""
    for key in ("label", "resolver", "method"):
        if not cand.get(key):
            raise ValueError(f"connector candidate is missing {key!r}: pass the candidate as the tool returned it")
    # The connector names any flag a candidate needs in its uncertainty_basis.
    flags = [fl for fl in ("default_uncertainty", "anchor_approximate")
             if fl in str(cand.get("uncertainty_basis", ""))]
    shape = cand.get("geometry")
    if shape and not as_point:
        return shape, float(cand.get("uncertainty_m") or 0.0), flags
    pt = cand.get("point")
    if not pt:
        raise ValueError(f"connector candidate {cand.get('label')!r} has no point or geometry (was it fetched "
                         "with geometry='none'?)")
    if shape or cand.get("radius_m") and cand.get("method") == "boundary_join":
        # An area placed at a point: the honest uncertainty is the size of the area.
        unc = cand.get("radius_m") or cand.get("uncertainty_m")
    else:
        unc = cand.get("uncertainty_m")
    if unc is None:
        raise ValueError(f"connector candidate {cand.get('label')!r} has no uncertainty_m")
    return C.point(pt[0], pt[1]), float(unc), flags


def _connector_reject(entry: dict) -> dict:
    cand, why = entry.get("candidate") or {}, entry.get("why")
    if not cand.get("label") or not why:
        raise ValueError("each entry in 'others' needs a candidate (with its label) and a 'why'")
    out = {"label": cand["label"] + (f" ({cand['kind']})" if cand.get("kind") else ""), "rejected": why}
    if cand.get("id"):
        out["id"] = str(cand["id"])
    if cand.get("point"):
        out["geometry"] = C.point(cand["point"][0], cand["point"][1])
    if isinstance(cand.get("score"), (int, float)):
        out["confidence"] = C.clamp01(cand["score"])
    return out


def _from_connector(dec, mention, source, tier, status, flags, conf, threshold) -> dict:
    others = [_connector_reject(e) for e in dec.get("others", [])]
    if "candidate" not in dec or status == "ambiguous":
        pool = ([_connector_reject({"candidate": dec["candidate"], "why": dec.get("why", "could not be separated "
                                    "from the other candidates")})] if "candidate" in dec else []) + others
        if not pool:
            raise ValueError(f"ambiguous decision for {mention!r} lists no candidates")
        first = (dec.get("candidate") or dec["others"][0]["candidate"])
        return C.make_feature(geometry=None, label=None, mention=mention, source=source, tier=tier,
                              method=first.get("method", "locator"), resolver=first.get("resolver", {"name": "connector"}),
                              confidence=min(conf, threshold - 0.001), uncertainty_m=None, status="ambiguous",
                              candidates=pool, source_crs="EPSG:4326", flags=flags)
    cand = dec["candidate"]
    geom, unc, cand_flags = _connector_geometry(cand, dec.get("geometry") == "point")
    st = "resolved" if conf >= threshold else "ambiguous"
    if st == "ambiguous":
        others.insert(0, _connector_reject({"candidate": cand, "why": "best candidate, but below threshold"}))
    return C.make_feature(geometry=geom, label=dec.get("label", cand["label"]), mention=mention, source=source,
                          tier=tier, method=cand["method"], resolver=cand["resolver"], confidence=conf,
                          uncertainty_m=unc, status=st, candidates=others, source_crs="EPSG:4326",
                          flags=flags + cand_flags)


def _span(f):
    s = f["properties"]["source"]
    return s.get("doc"), s.get("page"), s.get("row"), s.get("char_start"), s.get("char_end")


def lowest_tier_wins(features: list[dict]) -> tuple[list[dict], list[dict]]:
    kept, dropped = [], []
    for f in sorted(features, key=lambda f: _TIER_RANK[f["properties"]["tier"]]):
        doc, page, row, cs, ce = _span(f)
        clash = None
        if cs is not None and ce is not None:
            for k in kept:
                kd, kp, kr, ks, ke = _span(k)
                if (kd, kp, kr) == (doc, page, row) and ks is not None and ke is not None and cs < ke and ks < ce:
                    clash = k
                    break
        if clash is not None and _TIER_RANK[clash["properties"]["tier"]] < _TIER_RANK[f["properties"]["tier"]]:
            dropped.append(f)
        else:
            kept.append(f)
    kept.sort(key=lambda f: (str(_span(f)[0]), _span(f)[1] or 0, _span(f)[2] or 0, _span(f)[3] or 0))
    return kept, dropped


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--features", nargs="*", default=[], help="FeatureCollections from the scripts")
    ap.add_argument("--decisions", help="JSON list of decisions")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--threshold", type=float, default=C.DEFAULT_THRESHOLD)
    ap.add_argument("--db", help="gazetteer path")
    ap.add_argument("--frame", help="JSON object describing the document frame, recorded in the output")
    a = ap.parse_args(argv)

    feats, inputs = [], []
    for p in a.features:
        fc = json.loads(Path(p).read_text(encoding="utf-8"))
        feats += fc.get("features", [])
        inputs += (fc.get("spatialise") or {}).get("inputs", [])
    gz = _Gz(a.db)
    if a.decisions:
        problems = []
        for i, dec in enumerate(json.loads(Path(a.decisions).read_text(encoding="utf-8"))):
            try:
                feats.append(expand(dec, gz, a.threshold))
            except (ValueError, KeyError, TypeError) as e:
                what = f"missing {e}" if isinstance(e, KeyError) else str(e)
                problems.append(f"decision[{i}] ({str(dec.get('mention', '?'))[:40]!r}): {what}")
                continue
            doc = (dec.get("source") or {}).get("doc") if isinstance(dec.get("source"), dict) else dec.get("source")
            if doc:
                inputs.append(doc)
        if problems:
            # A decision that cannot be expanded would be a dropped mention: stop, write nothing.
            print(json.dumps({"out": None, "valid": False, "errors": problems}, indent=2))
            return 1
    kept, dropped = lowest_tier_wins(feats)
    frame = json.loads(a.frame) if a.frame else None
    fc = C.make_collection(kept, inputs=sorted(set(inputs)), threshold=a.threshold, frame=frame)
    errs, warns = V.validate(fc, a.threshold)
    Path(a.out).write_text(json.dumps(fc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"out": a.out, "features": len(kept), "counts": fc["spatialise"]["counts"],
                      "dropped_by_lower_tier": [f["properties"]["mention"] for f in dropped],
                      "valid": not errs, "errors": errs, "warnings": warns}, indent=2))
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
