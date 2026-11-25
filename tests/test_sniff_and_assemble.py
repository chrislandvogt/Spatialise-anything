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


# ---------------------------------------------------------------- connector candidates

LOCATOR_CAND = {"label": "45 Lydiard Street North, Ballarat Central, Victoria, 3350", "kind": "PointAddress",
                "point": [143.8589, -37.5601], "score": 0.98, "uncertainty_m": 15,
                "uncertainty_basis": "typical for a PointAddress match", "method": "locator",
                "resolver": {"name": "agency-addresses", "version": "G-NAF 2026-11"}}
OTHER_CAND = {**LOCATOR_CAND, "label": "45 Lydiard Street, Soldiers Hill, Victoria, 3350", "point": [143.86, -37.54],
              "score": 0.91}
LAKE_CAND = {"id": "vicnames:1234", "label": "LAKE WENDOUREE", "kind": "LAKE", "exact": True,
             "point": [143.8324, -37.5494], "method": "gazetteer", "resolver": {"name": "vicnames"},
             "uncertainty_m": 3000, "uncertainty_basis": "configured default for LAKE: a point gazetteer cannot say "
             "how big the place is (flag: default_uncertainty)"}
SA2_CAND = {"id": "sa2:201011001", "label": "Alfredton", "kind": "sa2", "code": "201011001", "method": "boundary_join",
            "resolver": {"name": "sa2", "version": "ABS ASGS Edition 3 (2021)"}, "point": [143.78, -37.55],
            "radius_m": 3100, "uncertainty_m": 55, "uncertainty_basis": "boundary simplified to about 55 m",
            "geometry": {"type": "Polygon", "coordinates": [[[143.7, -37.6], [143.8, -37.6], [143.8, -37.5],
                                                             [143.7, -37.5], [143.7, -37.6]]]}}


def run_assemble(tmp_path, decisions):
    d = tmp_path / "d.json"
    d.write_text(json.dumps(decisions))
    out = tmp_path / "o.geojson"
    code = A.main(["--decisions", str(d), "-o", str(out)])
    return code, (json.loads(out.read_text())["features"] if out.exists() else None)


def test_connector_candidates_become_contract_features_unchanged(tmp_path):
    src = lambda a, b: {"doc": "r.txt", "char_start": a, "char_end": b}  # noqa: E731
    code, feats = run_assemble(tmp_path, [
        {"mention": "45 Lydiard St North", "source": src(0, 19), "tier": "structured", "candidate": LOCATOR_CAND,
         "confidence": 0.9, "others": [{"candidate": OTHER_CAND, "why": "the report names Ballarat Central"}]},
        {"mention": "Lake Wendouree", "source": src(30, 44), "tier": "named", "candidate": LAKE_CAND, "confidence": 0.85},
        {"mention": "Alfredton SA2", "source": src(50, 63), "tier": "structured", "candidate": SA2_CAND, "confidence": 0.95},
        {"mention": "in Alfredton", "source": src(70, 82), "tier": "structured", "candidate": SA2_CAND,
         "geometry": "point", "confidence": 0.95},
    ])
    assert code == 0
    addr, lake, area, area_pt = (f["properties"] for f in feats)
    assert feats[0]["geometry"] == {"type": "Point", "coordinates": [143.8589, -37.5601]}
    assert addr["label"] == LOCATOR_CAND["label"] and addr["method"] == "locator" and addr["uncertainty_m"] == 15
    assert addr["resolver"] == {"name": "agency-addresses", "version": "G-NAF 2026-11"}
    assert addr["candidates"] == [{"label": OTHER_CAND["label"] + " (PointAddress)",
                                   "rejected": "the report names Ballarat Central", "confidence": 0.91,
                                   "geometry": {"type": "Point", "coordinates": [143.86, -37.54]}}]
    assert lake["flags"] == ["default_uncertainty"] and lake["uncertainty_m"] == 3000 and lake["method"] == "gazetteer"
    assert feats[2]["geometry"]["type"] == "Polygon" and area["uncertainty_m"] == 55 and area["method"] == "boundary_join"
    assert feats[3]["geometry"]["type"] == "Point" and area_pt["uncertainty_m"] == 3100   # an area at a point


def test_connector_ambiguous_and_below_threshold(tmp_path):
    code, feats = run_assemble(tmp_path, [
        {"mention": "Lydiard St", "source": {"doc": "r.txt", "char_start": 0, "char_end": 10}, "tier": "structured",
         "status": "ambiguous", "confidence": 0.5,
         "others": [{"candidate": LOCATOR_CAND, "why": "equal score"}, {"candidate": OTHER_CAND, "why": "equal score"}]},
        {"mention": "Wendouree", "source": {"doc": "r.txt", "char_start": 20, "char_end": 29}, "tier": "named",
         "candidate": LAKE_CAND, "confidence": 0.55},
    ])
    assert code == 0
    tie, low = feats
    assert tie["geometry"] is None and tie["properties"]["status"] == "ambiguous"
    assert len(tie["properties"]["candidates"]) == 2 and tie["properties"]["resolver"]["name"] == "agency-addresses"
    assert low["geometry"] is None and low["properties"]["status"] == "ambiguous"
    assert low["properties"]["candidates"][0]["rejected"] == "best candidate, but below threshold"
    assert low["properties"]["candidates"][0]["id"] == "vicnames:1234"


def test_a_decision_that_cannot_be_expanded_stops_the_run(tmp_path, capsys):
    trimmed = {k: v for k, v in LOCATOR_CAND.items() if k != "resolver"}
    code, feats = run_assemble(tmp_path, [
        {"mention": "ok", "source": {"doc": "r.txt"}, "tier": "structured", "candidate": LOCATOR_CAND, "confidence": 0.9},
        {"mention": "trimmed", "source": {"doc": "r.txt"}, "tier": "structured", "candidate": trimmed, "confidence": 0.9},
        {"mention": "nothing", "source": {"doc": "r.txt"}, "tier": "named"},
    ])
    report = json.loads(capsys.readouterr().out)
    assert code == 1 and feats is None and report["valid"] is False
    assert "decision[1] ('trimmed')" in report["errors"][0] and "as the tool returned it" in report["errors"][0]
    assert "decision[2]" in report["errors"][1]
