import copy
import json
from pathlib import Path

import pytest

import contract as C
import validate_features as V

ROOT = Path(__file__).resolve().parent.parent


def good():
    return C.make_collection([C.make_feature(
        geometry=C.point(144.99, -37.82), label="Richmond, VIC", mention="Richmond", source=C.make_source("a.txt"),
        tier="named", method="gazetteer", resolver={"name": "g"}, confidence=0.9, uncertainty_m=1188,
        status="resolved", source_crs="EPSG:7844")])


@pytest.mark.parametrize("path", sorted((ROOT / "corpus" / "items").glob("*/expected.geojson")))
def test_corpus_expected_validates(path):
    """Phase 1 exit test: the schema validates every hand-written expected output."""
    errs, _ = V.validate(json.loads(path.read_text(encoding="utf-8")))
    assert errs == []


def test_good_is_valid():
    assert V.validate(good())[0] == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda p, f: p.update(status="ambiguous"), "must have null geometry"),
    (lambda p, f: p.update(confidence=0.5), "below threshold"),
    (lambda p, f: p.update(uncertainty_m=None), "needs uncertainty_m"),
    (lambda p, f: p.update(tier="vague"), "tier"),
    (lambda p, f: p.update(extra=1), "unknown property"),
    (lambda p, f: p.pop("source_crs"), "source_crs"),
    (lambda p, f: p.update(candidates=[{"label": "x"}]), "rejected"),
    (lambda p, f: f.update(geometry={"type": "Point", "coordinates": [-37.82, 144.99]}), "outside lon/lat"),
    (lambda p, f: f.update(geometry={"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1]]]}), "not closed"),
    (lambda p, f: p["source"].update(char_start=9, char_end=2), "char_start > char_end"),
])
def test_rules(mutate, needle):
    fc = copy.deepcopy(good())
    f = fc["features"][0]
    mutate(f["properties"], f)
    errs, _ = V.validate(fc)
    assert any(needle in e for e in errs), errs


def test_ambiguous_needs_candidates():
    fc = good()
    p = fc["features"][0]["properties"]
    fc["features"][0]["geometry"] = None
    p.update(status="ambiguous", confidence=0.3, uncertainty_m=None)
    assert any("candidates" in e for e in V.validate(fc)[0])


def test_mentions_coverage():
    errs, _ = V.validate(good(), mentions=["Richmond", "Kyneton"])
    assert any("Kyneton" in e for e in errs)
