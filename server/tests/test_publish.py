"""publish_features: plan first, write only with the plan's token, drop nothing."""

import copy
import json
import sys
from pathlib import Path

import pytest

from conftest import call
from fake_arcgis import SERVICES
from spatialise_mcp import esri, publish

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "skills" / "spatialise-anything" / "scripts"))


def feature(geometry, status="resolved", mention="m", conf=0.9, **props):
    base = {"label": "L", "mention": mention, "source": {"doc": "d.txt", "page": 2, "char_start": 5, "char_end": 9},
            "tier": "named", "method": "gazetteer", "resolver": {"name": "r", "version": "1"},
            "confidence": conf, "uncertainty_m": 100 if status == "resolved" else None, "status": status,
            "candidates": [], "source_crs": "EPSG:7844"}
    base.update(props)
    return {"type": "Feature", "geometry": geometry, "properties": base}


def collection():
    ccw = [[144.0, -37.0], [144.1, -37.0], [144.1, -36.9], [144.0, -36.9], [144.0, -37.0]]
    return {"type": "FeatureCollection", "spatialise": {"contract_version": "0.1.0", "threshold": 0.7,
                                                        "counts": {"resolved": 4, "ambiguous": 1, "unresolved": 1}},
            "features": [
                feature({"type": "Point", "coordinates": [144.28, -36.76]}, mention="the depot", flags=["datum_assumed"]),
                feature({"type": "LineString", "coordinates": [[144.4, -37.2], [144.2, -37.0]]}, mention="along the road"),
                feature({"type": "Polygon", "coordinates": [ccw]}, mention="postcode 3550"),
                feature({"type": "MultiPoint", "coordinates": [[144.1, -37.1], [144.2, -37.2]]}, mention="two sites"),
                feature(None, status="ambiguous", mention="Richmond", conf=0.17, candidates=[
                    {"label": "Richmond, VIC", "rejected": "no context", "geometry": {"type": "Point", "coordinates": [145.0, -37.8]}},
                    {"label": "Richmond, NSW", "rejected": "no context"}]),
                feature(None, status="unresolved", mention="12 High St", conf=0.0, method="not_attempted",
                        flags=["needs_network"]),
            ]}


def layer(fake, name, idx):
    return fake.services[f"gis.portal.test/{name}"]["layers"][idx]


async def publish_new(app, fc, title="Ops report places"):
    dry = await call(app, "publish_features", {"features": fc, "create": title})
    assert dry.get("status") == "confirmation_required", dry
    return dry, await call(app, "publish_features", {"features": fc, "create": title,
                                                     "confirm_token": dry["confirm_token"]})


async def test_first_call_is_a_plan_and_writes_nothing(make_app, fake):
    before = set(fake.services)
    dry = await call(make_app(), "publish_features", {"features": collection(), "create": "Ops report places"})
    plan = dry["plan"]
    assert plan["action"] == "create" and plan["service_name"] == "Ops_report_places"
    assert plan["title"] == "Ops report places" and plan["portal"] == "portal.test" and plan["as_user"] == "jane"
    assert plan["sharing"] == "private to the owner"
    assert plan["writes"] == {"points": 3, "lines": 1, "polygons": 1, "unplaced": 2}
    assert plan["statuses"] == {"resolved": 4, "ambiguous": 1, "unresolved": 1}
    assert "Nothing has been written yet" in dry["message"]
    assert set(fake.services) == before
    assert not [c for c in fake.calls if "createService" in c["path"] or "addFeatures" in c["path"]]


async def test_confirmed_publish_creates_layers_and_drops_nothing(make_app, fake):
    _, out = await publish_new(make_app(), collection())
    assert out["status"] == "published" and out["written"] == 7 and out["failed"] == 0
    assert out["service_url"] == f"{SERVICES}/Ops_report_places/FeatureServer"
    assert out["item_url"].startswith("https://portal.test/home/item.html?id=") and out["sharing"] == "private to the owner"
    assert [(l["name"], l["added"]) for l in out["layers"]] == [("points", 3), ("lines", 1), ("polygons", 1), ("unplaced", 2)]
    svc = fake.services["gis.portal.test/Ops_report_places"]
    assert svc["title"] == "Ops report places" and svc["owner"] == "jane"
    assert publish.MARKER in svc["description"]
    assert [(l.name, l.geometry_type) for l in svc["layers"].values()] == [
        ("points", "esriGeometryPoint"), ("lines", "esriGeometryPolyline"), ("polygons", "esriGeometryPolygon"),
        ("unplaced", None)]

    pt = layer(fake, "Ops_report_places", 0).features[0]
    assert pt["geometry"] == {"x": 144.28, "y": -36.76, "spatialReference": {"wkid": 4326}}
    a = pt["attributes"]
    assert a["mention"] == "the depot" and a["status"] == "resolved" and a["confidence"] == 0.9
    assert a["source_doc"] == "d.txt" and a["source_page"] == 2 and a["char_start"] == 5 and a["char_end"] == 9
    assert a["resolver_name"] == "r" and a["resolver_version"] == "1" and a["source_crs"] == "EPSG:7844"
    assert a["flags"] == "datum_assumed" and a["run_id"] == out["run_id"] and isinstance(a["published_at"], int)

    ring = layer(fake, "Ops_report_places", 2).features[0]["geometry"]["rings"][0]
    assert esri.signed_area(ring) < 0                               # Esri exterior rings are clockwise
    assert len(layer(fake, "Ops_report_places", 0).features) == 3   # the MultiPoint became two point rows

    unplaced = layer(fake, "Ops_report_places", 3).features
    assert [u["attributes"]["status"] for u in unplaced] == ["ambiguous", "unresolved"]
    assert all("geometry" not in u for u in unplaced)
    amb = unplaced[0]["attributes"]
    assert amb["candidate_count"] == 2 and json.loads(amb["candidates_json"])[0]["label"] == "Richmond, VIC"
    assert unplaced[1]["attributes"]["flags"] == "needs_network"


async def test_token_is_bound_to_these_features_and_this_target(make_app, fake):
    app = make_app()
    fc = collection()
    dry = await call(app, "publish_features", {"features": fc, "create": "Layer A"})
    changed = copy.deepcopy(fc)
    changed["features"][0]["geometry"]["coordinates"] = [150.0, -33.0]
    out = await call(app, "publish_features", {"features": changed, "create": "Layer A", "confirm_token": dry["confirm_token"]})
    assert "arguments changed since the dry run" in out["_error"]
    out = await call(app, "publish_features", {"features": fc, "create": "Layer B", "confirm_token": dry["confirm_token"]})
    assert "does not match" in out["_error"]
    assert "gis.portal.test/Layer_A" not in fake.services and "gis.portal.test/Layer_B" not in fake.services


async def test_features_that_break_the_contract_are_refused_before_anything_exists(make_app, fake):
    fc = collection()
    fc["features"][0]["properties"]["confidence"] = 0.4            # resolved below threshold
    del fc["features"][1]["properties"]["source_crs"]
    out = await call(make_app(), "publish_features", {"features": fc, "create": "Bad"})
    assert "break the output contract" in out["_error"] and "below threshold" in out["_error"]
    assert "source_crs" in out["_error"] and "gis.portal.test/Bad" not in fake.services


async def test_target_rules(make_app, fake):
    app = make_app()
    fc = collection()
    assert "name exactly one target" in (await call(app, "publish_features", {"features": fc}))["_error"]
    both = await call(app, "publish_features", {"features": fc, "create": "x", "url": f"{SERVICES}/x/FeatureServer"})
    assert "name exactly one target" in both["_error"]
    taken = await call(app, "publish_features", {"features": fc, "create": "Zones"})
    assert "already exists" in taken["_error"]
    empty = await call(app, "publish_features", {"features": {"type": "FeatureCollection", "features": []}, "create": "x"})
    assert "nothing to publish" in empty["_error"]
    assert publish.service_name("2026 flood: sites & roads!") == "Spatialise_2026_flood_sites_roads"
    assert publish.service_name("Ops report") == "Ops_report"


async def test_append_to_a_layer_this_tool_created(make_app, fake):
    app = make_app()
    _, first = await publish_new(app, collection(), "Flood sites")
    url = first["service_url"]
    more = {"type": "FeatureCollection", "features": [feature({"type": "Point", "coordinates": [144.5, -37.3]}, mention="new site")]}
    dry = await call(app, "publish_features", {"features": more, "url": url + "/0"})   # a layer URL is fine too
    assert dry["plan"]["action"] == "append" and dry["plan"]["service_url"] == url and dry["plan"]["sharing"] == "unchanged"
    out = await call(app, "publish_features", {"features": more, "url": url + "/0", "confirm_token": dry["confirm_token"]})
    assert out["status"] == "published" and out["written"] == 1 and "item_url" not in out
    assert out["run_id"] != first["run_id"]
    assert len(layer(fake, "Flood_sites", 0).features) == 4


async def test_append_refuses_foreign_layers_and_untrusted_hosts(make_app, fake):
    app = make_app()
    fc = collection()
    foreign = await call(app, "publish_features", {"features": fc, "url": f"{SERVICES}/Zones/FeatureServer"})
    assert "does not have the Spatialise schema" in foreign["_error"]
    evil = await call(app, "publish_features", {"features": fc, "url": "https://evil.example/arcgis/rest/services/x/FeatureServer"})
    assert "not a host of this server's ArcGIS portal" in evil["_error"]
    assert not [c for c in fake.calls if c["host"] == "evil.example"]
    notfs = await call(app, "publish_features", {"features": fc, "url": f"{SERVICES}/Zones/MapServer"})
    assert "FeatureServer URL" in notfs["_error"]


async def test_policy_and_permission_gates(make_app, fake):
    fc = collection()
    off = await call(make_app({"policy": {"allow_publish": False}}), "publish_features", {"features": fc, "create": "x"})
    assert "disabled on this server" in off["_error"] and "GeoJSON file instead" in off["_error"]
    anon = await call(make_app(mode="none"), "publish_features", {"features": fc, "create": "x"})
    assert "needs an ArcGIS sign-in" in anon["_error"]
    big = await call(make_app({"policy": {"max_publish_features": 3}}), "publish_features", {"features": fc, "create": "x"})
    assert "over this server's limit of 3" in big["_error"]
    fake.users["jane"]["privileges"] = ["features:user:edit"]
    fake.users["jane"]["role"] = "org_user"
    viewer = await call(make_app(), "publish_features", {"features": fc, "create": "x"})
    assert "not allowed to publish hosted feature layers" in viewer["_error"]


async def test_partial_failure_is_reported_not_hidden(make_app, fake):
    app = make_app()
    _, first = await publish_new(app, collection(), "Partial")
    for f in layer(fake, "Partial", 1).fields:                      # make the lines layer reject long mentions
        if f["name"] == "mention":
            f["length"] = 5
    more = {"type": "FeatureCollection", "features": [
        feature({"type": "Point", "coordinates": [144.5, -37.3]}, mention="ok point"),
        feature({"type": "LineString", "coordinates": [[144.4, -37.2], [144.2, -37.0]]}, mention="a long line mention")]}
    dry = await call(app, "publish_features", {"features": more, "url": first["service_url"]})
    out = await call(app, "publish_features", {"features": more, "url": first["service_url"], "confirm_token": dry["confirm_token"]})
    assert out["status"] == "partial" and out["written"] == 1 and out["failed"] == 1
    lines = next(l for l in out["layers"] if l["name"] == "lines")
    assert lines["added"] == 0 and lines["failed"] == 1 and "truncated" in lines["errors"][0]
    assert "not written" in out["message"] and out["run_id"] in out["message"]


async def test_long_values_are_clipped_and_candidate_detail_trimmed_honestly(make_app, fake):
    big_ring = [[144 + i / 10000, -37 + (i % 2) / 10000] for i in range(700)] + [[144.0, -37.0]]
    big_ring.insert(0, [144.0, -37.0])
    cands = [{"label": f"Candidate {i}", "rejected": "too far", "geometry": {"type": "Polygon", "coordinates": [big_ring]}}
             for i in range(3)]
    fc = {"type": "FeatureCollection", "features": [
        feature(None, status="ambiguous", conf=0.2, mention="x" * 2500, candidates=cands)]}
    _, out = await publish_new(make_app(), fc, "Clipped")
    assert out["status"] == "published" and "candidates_trimmed" in out
    a = layer(fake, "Clipped", 3).features[0]["attributes"]
    assert len(a["mention"]) == 2000 and a["mention"].endswith("…")
    assert a["candidate_count"] == 3 and len(a["candidates_json"]) <= publish.CANDIDATES_LEN
    kept = json.loads(a["candidates_json"])
    assert len(kept) == 3 and all("geometry" not in c for c in kept)   # shapes dropped, every candidate kept


async def test_large_publish_is_chunked(make_app, fake, monkeypatch):
    monkeypatch.setattr(publish, "CHUNK_FEATURES", 40)
    fc = {"type": "FeatureCollection", "features": [
        feature({"type": "Point", "coordinates": [144 + i / 1000, -37.0]}, mention=f"site {i}") for i in range(95)]}
    _, out = await publish_new(make_app(), fc, "Many")
    assert out["written"] == 95
    assert len([c for c in fake.calls if c["path"].endswith("/0/addFeatures")]) == 3
    assert len(layer(fake, "Many", 0).features) == 95


async def test_corpus_expected_outputs_all_publish(make_app, fake):
    """Every expected output in the corpus is publishable as it stands."""
    app = make_app()
    for path in sorted((ROOT / "corpus" / "items").glob("*/expected.geojson")):
        fc = json.loads(path.read_text(encoding="utf-8"))
        if not fc["features"]:
            continue
        title = "corpus " + path.parent.name
        _, out = await publish_new(app, fc, title)
        assert out["status"] == "published", (path.parent.name, out)
        svc = fake.services[f"gis.portal.test/{publish.service_name(title)}"]
        rows = sum(len(l.features) for l in svc["layers"].values())
        multipoints = sum(len(f["geometry"]["coordinates"]) - 1 for f in fc["features"]
                          if f["geometry"] and f["geometry"]["type"] == "MultiPoint")
        assert rows == len(fc["features"]) + multipoints           # mentions in == rows out
