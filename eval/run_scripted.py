#!/usr/bin/env python3
"""Scripts-only baseline: run the deterministic tier 0 path over the corpus
with no model in the loop, and write one output per item.

This is what the skill's scripts do on their own. It covers the explicit
items, the asset table and the no-places document; items that need
judgement (named, relative, implicit) are skipped and should be scored
with --only.

    python eval/run_scripted.py --out runs/scripted
    python eval/score.py --outputs runs/scripted --offline \
        --only t0-coords-mixed t0-datums t0-photos t1-assets none-traps
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "spatialise-anything" / "scripts"))
import contract as C  # noqa: E402
import parse_coords as P  # noqa: E402
import read_exif as X  # noqa: E402
import sniff_table as S  # noqa: E402

ITEMS = {"t0-coords-mixed", "t0-datums", "t0-photos", "t1-assets", "none-traps"}
FRAME = P.FRAMES["AU"]


def run_text(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    dets = P.detect(text, frame=FRAME)
    return C.make_collection([P.to_feature(d, path.name) for d in dets], inputs=[path.name],
                             threshold=C.DEFAULT_THRESHOLD)


def run_photos(paths: list[Path]) -> dict:
    recs = [X.record(p) for p in paths]
    return C.make_collection([X.to_feature(r) for r in recs], inputs=[p.name for p in paths],
                             threshold=C.DEFAULT_THRESHOLD)


def _header_datum(header: list[str]) -> str | None:
    joined = " ".join(header).lower()
    if re.search(r"(mga|gda)_?2020", joined):
        return "GDA2020"
    if re.search(r"(mga|gda)_?94", joined):
        return "GDA94"
    return None


def run_table(path: Path) -> dict:
    rep = S.sniff(path)
    header = [c["name"] for c in rep["columns"]]
    grid_datum = _header_datum(header)
    feats = []
    for rm in S.row_mentions(path, rep):
        src = C.make_source(path.name, row=rm["row"])
        if rm.get("empty"):
            feats.append(C.make_feature(
                geometry=None, label=rm.get("name"), mention=rm.get("asset_id") or f"row {rm['row']}", source=src,
                tier="structured", method="not_attempted", resolver={"name": "none"}, confidence=0.0,
                uncertainty_m=None, status="unresolved", source_crs="n/a", flags=["needs_network"]))
            continue
        datum = grid_datum if rm.get("form") == "grid" else None
        dets = P.detect(rm["mention"], default_datum=datum, frame=FRAME)
        if not dets:
            feats.append(C.make_feature(
                geometry=None, label=rm.get("name"), mention=rm["mention"], source=src, tier="explicit",
                method="coord_parse", resolver=P.RESOLVER, confidence=0.0, uncertainty_m=None,
                status="unresolved", source_crs="unknown"))
            continue
        f = P.to_feature(dets[0], path.name)
        f["properties"]["source"] = src
        f["properties"]["label"] = rm.get("name") or f["properties"]["label"]
        feats.append(f)
    return C.make_collection(feats, inputs=[path.name], threshold=C.DEFAULT_THRESHOLD)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", type=Path, default=ROOT / "corpus")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((a.corpus / "manifest.json").read_text(encoding="utf-8"))
    done = []
    for item in manifest["items"]:
        if item["id"] not in ITEMS:
            continue
        d = a.corpus / "items" / item["id"]
        inputs = [d / n for n in item["inputs"]]
        if all(p.suffix.lower() in (".jpg", ".jpeg", ".tif", ".tiff") for p in inputs):
            fc = run_photos(inputs)
        elif inputs[0].suffix.lower() in (".csv", ".tsv", ".xlsx"):
            fc = run_table(inputs[0])
        else:
            fc = run_text(inputs[0])
        (a.out / f"{item['id']}.geojson").write_text(json.dumps(fc, indent=2, ensure_ascii=False) + "\n",
                                                    encoding="utf-8")
        done.append(item["id"])
    print(json.dumps({"out": str(a.out), "items": done}))


if __name__ == "__main__":
    main()
