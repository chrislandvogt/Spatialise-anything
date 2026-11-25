"""Phase 3 exit test, as far as it can run without a real organisation:

    "A document with addresses and ambiguous names lands in a hosted feature
     layer, resolved through a nominated locator."

Everything between the document and the layer is real: the MCP protocol over
HTTP, the OAuth sign-in, the server's tools, the skill's own assemble.py and
validator, and the corpus scorer. Two things stand in:

  * the ArcGIS portal, locator, gazetteer and hosted layer are the in-process
    fake (its answers for the corpus addresses are the corpus reference points,
    so the score here proves the plumbing, not the locator);
  * the decisions a model would make are written out below, each with the
    reason a model would have.

The real exit test needs an ArcGIS organisation, an OAuth app and a locator.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from fake_arcgis import SERVICES

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "skills" / "spatialise-anything" / "scripts"
CORPUS = ROOT / "corpus"
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(SCRIPTS))


def spans(item: str) -> dict[str, dict]:
    fc = json.loads((CORPUS / "items" / item / "expected.geojson").read_text(encoding="utf-8"))
    return {f["properties"]["mention"]: f["properties"]["source"] for f in fc["features"]}


async def test_document_to_hosted_layer_through_a_nominated_locator(web, fake, tmp_path):
    import gazetteer
    import score

    addr_spans, bendigo_spans = spans("t1-addresses"), spans("t2-bendigo")
    async with web() as w:
        token = (await w.sign_in())["access_token"]

        # 1. What may be used here?
        res = await w.tool(token, "list_resolvers")
        assert res["signed_in_as"] == "jane"
        locator = next(l["name"] for l in res["locators"] if l["default"])
        assert locator == "customer-addresses" and res["policy"]["public_geocoder_allowed"] is False

        # 2. Addresses: the count is reported first, then the batch runs through the nominated locator.
        queries = list(addr_spans) + ["1 Main Street"]
        dry = await w.tool(token, "geocode", {"queries": queries, "resolver": locator, "near": [144.28, -36.76]})
        assert dry["status"] == "confirmation_required" and dry["count"] == 4
        geo = await w.tool(token, "geocode", {"queries": queries, "resolver": locator, "near": [144.28, -36.76],
                                              "confirm_token": dry["confirm_token"]})
        assert geo["status"] == "ok" and geo["resolver"]["name"] == locator

        # 3. A name the offline gazetteer does not hold.
        gaz = await w.tool(token, "search_gazetteer", {"names": ["Lake Eppalock"], "gazetteer": "placenames"})
        lake_cands = gaz["results"][0]["candidates"]
        assert {c["kind"] for c in lake_cands} == {"LAKE", "LOCALITY"}

        # 4. The decisions a model would make, with its reasons.
        decisions = []
        for r in geo["results"][:3]:
            top = r["candidates"][0]
            decisions.append({"mention": r["query"], "source": addr_spans[r["query"]], "tier": "structured",
                              "candidate": top, "confidence": 0.9,
                              "others": [{"candidate": c, "why": "lower match score"} for c in r["candidates"][1:]]})
        main = geo["results"][3]["candidates"]
        assert len(main) == 2 and main[0]["score"] == main[1]["score"]
        decisions.append({"mention": "1 Main Street", "source": {"doc": "delivery-note.txt", "char_start": 11,
                                                                 "char_end": 24},
                          "tier": "structured", "status": "ambiguous", "confidence": 0.5,
                          "others": [{"candidate": c, "why": "same score, and the note names no town"} for c in main]})
        lake = next(c for c in lake_cands if c["kind"] == "LAKE")
        decisions.append({"mention": "Lake Eppalock", "source": bendigo_spans["Lake Eppalock"], "tier": "named",
                          "candidate": lake, "confidence": 0.9,
                          "others": [{"candidate": c, "why": "water sampling happens at the lake, not the locality"}
                                     for c in lake_cands if c["kind"] != "LAKE"]})
        gz = gazetteer.Gazetteer()
        anchors = [(144.2794, -36.757)]
        for name in ("Kangaroo Flat", "Eaglehawk", "Epsom", "Strathdale", "Golden Square"):
            found = gz.search(name, state="VIC", near=anchors)
            pick = found["candidates"][0]
            decisions.append({"mention": name, "source": bendigo_spans[name], "tier": "named", "pick": pick["id"],
                              "confidence": round(min(0.9, found["right_place_hint"]), 2),
                              "rejects": {c["id"]: "the other suburbs named are all Bendigo"
                                          for c in found["candidates"][1:] if "nested_in" not in c}})

        # 5. The skill's own bookkeeping and validation.
        (tmp_path / "decisions.json").write_text(json.dumps(decisions))
        out = tmp_path / "spatialised.geojson"
        run = subprocess.run([sys.executable, str(SCRIPTS / "assemble.py"), "--decisions",
                              str(tmp_path / "decisions.json"), "-o", str(out)], capture_output=True, text=True)
        assert run.returncode == 0, run.stdout + run.stderr
        report = json.loads(run.stdout)
        assert report["valid"] and report["counts"] == {"resolved": 9, "ambiguous": 1, "unresolved": 0}
        collection = json.loads(out.read_text())

        # 6. Publish: plan first, then only to the layer the user named.
        plan = await w.tool(token, "publish_features", {"features": collection, "create": "Bendigo works notice"})
        assert plan["status"] == "confirmation_required"
        assert plan["plan"]["writes"] == {"points": 9, "unplaced": 1} and plan["plan"]["as_user"] == "jane"
        assert "gis.portal.test/Bendigo_works_notice" not in fake.services
        done = await w.tool(token, "publish_features", {"features": collection, "create": "Bendigo works notice",
                                                        "confirm_token": plan["confirm_token"]})
        assert done["status"] == "published" and done["written"] == 10
        assert done["service_url"] == f"{SERVICES}/Bendigo_works_notice/FeatureServer"

    # 7. It landed: every mention is a row, with its provenance.
    layers = fake.services["gis.portal.test/Bendigo_works_notice"]["layers"]
    points = {f["attributes"]["mention"]: f for f in layers[0].features}
    assert len(points) == 9 and fake.services["gis.portal.test/Bendigo_works_notice"]["owner"] == "jane"
    hall = points["Bendigo Town Hall, 189-193 Hargreaves Street, Bendigo VIC 3550"]
    assert hall["geometry"]["x"] == 144.2825028 and hall["attributes"]["method"] == "locator"
    assert hall["attributes"]["resolver_name"] == "customer-addresses" and hall["attributes"]["resolver_version"] == "2026-11"
    assert hall["attributes"]["source_doc"] == "works-notice.txt" and hall["attributes"]["uncertainty_m"] == 15
    eppalock = points["Lake Eppalock"]["attributes"]
    assert eppalock["flags"] == "default_uncertainty" and eppalock["candidate_count"] == 1
    assert "not the locality" in eppalock["candidates_json"]
    assert points["Golden Square"]["attributes"]["resolver_name"] == "bundled-gazetteer"
    (unplaced,) = layers[3].features
    assert unplaced["attributes"]["mention"] == "1 Main Street" and unplaced["attributes"]["status"] == "ambiguous"
    assert unplaced["attributes"]["candidate_count"] == 2 and "geometry" not in unplaced

    # 8. And it scores against the corpus, online: nothing dropped, nothing confidently wrong.
    runs = tmp_path / "runs"
    runs.mkdir()
    by_doc = {"works-notice.txt": "t1-addresses", "streetlight-audit.txt": "t2-bendigo"}
    for doc, item in by_doc.items():
        feats = [f for f in collection["features"] if f["properties"]["source"]["doc"] == doc]
        (runs / f"{item}.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    result = score.score(CORPUS, runs, offline=False, threshold=None, only=set(by_doc.values()))
    outcomes = {d["mention"]: d["outcome"] for d in result["details"]}
    assert all(o == "correct" for o in outcomes.values()), outcomes
    assert len(outcomes) == 9 and result["confident_wrong_rate"] == 0 and result["total"].get("dropped", 0) == 0
