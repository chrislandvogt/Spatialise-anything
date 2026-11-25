"""End-to-end: the scripts-only baseline meets the tier 0 bar on the corpus
(the scripted half of the Phase 2 exit test), and the scorer scores the
expected outputs as perfect."""
import json
import shutil
from pathlib import Path

import run_scripted
import score

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "corpus"
T0 = {"t0-coords-mixed", "t0-datums", "t0-photos", "t1-assets", "none-traps"}


def test_scorer_self_test(tmp_path):
    for d in (CORPUS / "items").iterdir():
        shutil.copy(d / "expected.geojson", tmp_path / f"{d.name}.geojson")
    res = score.score(CORPUS, tmp_path, offline=False, threshold=None)
    assert res["confident_wrong_rate"] == 0
    assert all(res["bars"].values()), res["bars"]
    assert all(d["outcome"] == "correct" for d in res["details"])


def test_scripted_baseline_meets_tier0_bar(tmp_path):
    run_scripted.main(["--out", str(tmp_path)])
    res = score.score(CORPUS, tmp_path, offline=True, threshold=None, only=T0)
    assert res["bars"]["explicit: no misses, no false positives"], res["details"]
    assert res["bars"]["no-places document: no features"]
    assert res["bars"]["every mention accounted for"]
    assert res["confident_wrong_rate"] == 0


def test_empty_run_drops_everything(tmp_path):
    res = score.score(CORPUS, tmp_path, offline=False, threshold=None)
    assert res["total"]["dropped"] == res["total"]["n"]
    assert not res["bars"]["every mention accounted for"]


def test_named_reference_points_sit_in_the_bundled_gazetteer_polygons():
    """Cross-check: corpus positions come from raw ABS shapefiles; the bundled
    gazetteer is a simplified copy. Each expected locality point must fall in
    the gazetteer polygon of the locality the label names."""
    import gazetteer as g
    gz = g.Gazetteer()
    for item in ("t2-melbourne", "t2-bendigo", "t2-springfield", "t2-hawkesbury"):
        fc = json.loads((CORPUS / "items" / item / "expected.geojson").read_text(encoding="utf-8"))
        for f in fc["features"]:
            p = f["properties"]
            if p["resolver"]["name"] != "reference:ABS ASGS Ed3":
                continue
            name, state = p["label"].split(", ")
            res = gz.search(name, state=state, kind="locality", geometry="polygon")
            assert g.point_in_geometry(*f["geometry"]["coordinates"], res["candidates"][0]["geometry"]), p["label"]
