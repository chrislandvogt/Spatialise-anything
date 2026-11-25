import json
from pathlib import Path

import assemble as A
import contract as C
import sniff_table as S

ROOT = Path(__file__).resolve().parent.parent
ITEMS = ROOT / "corpus" / "items"


def test_sniff_assets():
    rep = S.sniff(ITEMS / "t1-assets" / "assets.csv")
    roles = {c["name"]: c["role"] for c in rep["columns"]}
    assert roles["lat"] == "lat" and roles["lon"] == "lon"
    assert roles["easting_mga2020"] == "easting" and roles["northing_mga2020"] == "northing"
    assert roles["asset_id"] == "asset_id"
    assert rep["plan"]["tier"] == "explicit"
    rows = S.row_mentions(ITEMS / "t1-assets" / "assets.csv", rep)
    assert [r.get("form") for r in rows] == ["lat_lon", "lat_lon", "grid", "grid", None]
    assert rows[-1]["empty"] and rows[-1]["asset_id"] == "HY-330"


def test_sniff_areas():
    rep = S.sniff(ITEMS / "t1-areas" / "requests_by_area.csv")
    assert rep["rows"] == 5


def test_assemble_decisions(tmp_path):
    decisions = [
        {"mention": "Richmond", "source": {"doc": "d.txt", "char_start": 21, "char_end": 29}, "tier": "named",
         "pick": "locality:22170", "confidence": 0.9, "rejects": {"locality:13375": "inner Melbourne context"}},
        {"mention": "Richmond", "source": {"doc": "m.txt", "char_start": 0, "char_end": 8}, "tier": "named",
         "status": "ambiguous", "confidence": 0.17,
         "candidates": {"locality:22170": "no context", "locality:13375": "no context"}},
        {"mention": "3550", "source": {"doc": "t.csv", "row": 2}, "tier": "structured", "pick": "postcode:3550",
         "geometry": "polygon", "confidence": 0.98},
        {"mention": "the plant", "source": {"doc": "s.txt", "char_start": 0, "char_end": 9}, "tier": "implicit",
         "status": "unresolved", "flags": ["tier4_disabled"]},
    ]
    d = tmp_path / "d.json"
    d.write_text(json.dumps(decisions))
    out = tmp_path / "o.geojson"
    assert A.main(["--decisions", str(d), "-o", str(out)]) == 0
    fc = json.loads(out.read_text())
    st = {f["properties"]["source"]["doc"]: f for f in fc["features"]}
    assert st["d.txt"]["properties"]["label"] == "Richmond, VIC" and st["d.txt"]["properties"]["uncertainty_m"] > 1000
    assert st["d.txt"]["properties"]["candidates"][0]["label"] == "Richmond, NSW"
    assert st["m.txt"]["geometry"] is None and len(st["m.txt"]["properties"]["candidates"]) == 2
    assert st["t.csv"]["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert st["t.csv"]["properties"]["method"] == "boundary_join"


def test_lowest_tier_wins():
    def f(tier, cs, ce):
        return C.make_feature(geometry=C.point(144, -37), label=None, mention="x", source=C.make_source(
            "a.txt", char_start=cs, char_end=ce), tier=tier, method="m", resolver={"name": "r"}, confidence=0.9,
            uncertainty_m=1, status="resolved")
    kept, dropped = A.lowest_tier_wins([f("named", 0, 30), f("explicit", 10, 25), f("named", 40, 50)])
    assert [k["properties"]["tier"] for k in kept] == ["explicit", "named"]
    assert len(dropped) == 1


def test_preview_embeds_data_safely(tmp_path):
    import preview_map as M
    fc = json.loads((ITEMS / "t2-richmond-alone" / "expected.geojson").read_text(encoding="utf-8"))
    fc["features"][0]["properties"]["mention"] = "</script><script>alert(1)</script>"
    html = M.render(fc, "T <b>", "none")
    assert "</script><script>alert(1)" not in html and "T &lt;b&gt;" in html
    assert 'const BASEMAP = "none"' in html
