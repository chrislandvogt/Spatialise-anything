"""Opt-in checks against real, public ArcGIS services. Read-only.

    SPATIALISE_LIVE=1 python -m pytest tests/test_live.py

They prove the REST handling against real servers (ABS MapServer 11.5, ArcGIS
Online hosted feature layers, and the World Geocoding Service's candidates and
token error), which the in-process fake can only imitate. They are
skipped by default: CI should not depend on other people's servers.
"""

import os
from pathlib import Path

import httpx
import pytest

from conftest import call
from spatialise_mcp import config as C
from spatialise_mcp import esri, resolvers
from spatialise_mcp.server import build

pytestmark = [pytest.mark.anyio,
              pytest.mark.skipif(not os.environ.get("SPATIALISE_LIVE"), reason="set SPATIALISE_LIVE=1 to run")]

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"
WORLD = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer"


@pytest.fixture
def live():
    resolvers.clear_cache()
    made = []

    def _make(extra=None, env=None):
        import tomllib
        data = tomllib.loads(EXAMPLE.read_text())
        data.update(extra or {})
        app = build(C.from_dict(data, env or {}), http=httpx.AsyncClient(timeout=40))
        made.append(app)
        return app
    return _make


async def test_abs_sa2_by_code_and_name(live):
    out = await call(live(), "lookup_boundary", {"boundary": "sa2", "codes": ["202021030"], "names": ["Castlemaine"],
                                                 "state": "Victoria", "max_vertices": 200})
    kyneton = out["results"][0]["candidates"][0]
    assert kyneton["label"] == "Kyneton" and kyneton["state"] == "Victoria" and kyneton["vertices"] <= 200
    assert 450 < kyneton["area_km2"] < 650                     # ABS says 556.5 km2; ours is from the simplified ring
    assert kyneton["geometry"]["type"] in ("Polygon", "MultiPolygon")
    ring = kyneton["geometry"]["coordinates"][0] if kyneton["geometry"]["type"] == "Polygon" else kyneton["geometry"]["coordinates"][0][0]
    assert esri.signed_area(ring) > 0 and ring[0] == ring[-1]  # RFC 7946 winding, closed
    assert 144.2 < kyneton["point"][0] < 144.8 and -37.5 < kyneton["point"][1] < -37.0
    assert kyneton["attributes"]["sa3_name_2021"] and kyneton["resolver"]["version"].startswith("ABS ASGS")
    assert out["results"][1]["candidates"][0]["code"] == "202021027"


async def test_abs_lga_postcode_and_locality(live):
    app = live()
    lga = await call(app, "lookup_boundary", {"boundary": "lga", "names": ["Greater Bendigo"], "geometry": "point"})
    c = lga["results"][0]["candidates"][0]
    assert c["code"] == "22620" and 2500 < c["area_km2"] < 3500 and c["state"] == "Victoria"
    poa = await call(app, "lookup_boundary", {"boundary": "postcode", "codes": ["3550", "9726"]})
    assert poa["results"][0]["count"] == 1 and poa["results"][1]["count"] == 0
    sal = await call(app, "lookup_boundary", {"boundary": "locality", "names": ["Golden Square"], "geometry": "none"})
    assert sal["results"][0]["candidates"][0]["state"] == "Victoria"


async def test_vicnames_gazetteer(live):
    app = live()
    out = await call(app, "search_gazetteer", {"names": ["Lake Eppalock", "Richmond"], "near": [144.28, -36.76]})
    eppalock = {c["kind"]: c for c in out["results"][0]["candidates"]}
    assert {"LAKE", "LOCALITY"} <= set(eppalock)
    lake = eppalock["LAKE"]
    assert lake["label"] == "LAKE EPPALOCK" and lake["uncertainty_m"] == 5000 and lake["state"] == "VIC"
    assert esri.haversine_m(*lake["point"], 144.5428476, -36.9045757) < 6000   # OSM's point for the reservoir
    kinds = [c["kind"] for c in out["results"][1]["candidates"]]
    assert "LOCALITY" in kinds and len(set(kinds)) >= 3      # one name, several kinds of thing
    only = await call(app, "search_gazetteer", {"names": ["Richmond"], "feature_types": ["Rail Station"]})
    assert [c["kind"] for c in only["results"][0]["candidates"]] == ["RAIL STATION"]
    odd = await call(app, "search_gazetteer", {"names": ["x' OR '1'='1"]})
    assert odd["results"][0]["candidates"] == []


async def test_real_locator_response_shape(live):
    """One unstored request to the World Geocoding Service, to prove the candidate handling on a real
    locator. The config loader forbids an unstored public geocoder, so this builds the locator by hand:
    it is a shape test, not a supported way to run."""
    import dataclasses

    from spatialise_mcp.arcgis import Anonymous, ArcGIS
    from spatialise_mcp.config import Locator
    from spatialise_mcp.resolvers import Resolvers
    app = live()
    loc = Locator(name="world-unstored", url=WORLD, kind="customer", auth="none", for_storage=False)
    cfg = dataclasses.replace(app.cfg, locators=(loc,))
    out = await Resolvers(cfg, ArcGIS(cfg, app.http, Anonymous())).geocode(
        ["189 Hargreaves Street, Bendigo VIC 3550"], near=[144.28, -36.76])
    assert out["status"] == "ok" and out["resolver"]["version"].startswith("ArcGIS REST")
    c = out["results"][0]["candidates"][0]
    assert c["kind"] == "PointAddress" and c["uncertainty_m"] == 15 and c["score"] >= 0.95
    assert esri.haversine_m(*c["point"], 144.2825028, -36.7588188) < 150      # OSM's Bendigo Town Hall
    assert c["attributes"]["RegionAbbr"] == "VIC" and c["distance_km"] < 2
    assert "forStorage" not in str(out)


async def test_world_geocoder_needs_a_sign_in_for_stored_results(live):
    """The policy path end to end on the real service: enabled, but there is no
    sign-in, and stored geocodes need a token - so it must say so, not guess."""
    app = live({"locators": [{"name": "world", "url": WORLD, "kind": "public", "auth": "none"}],
                "policy": {"allow_public_geocoder": True}})
    out = await call(app, "geocode", {"queries": ["Bendigo Town Hall"]})
    assert "needs a token" in out["_error"] or "sign-in" in out["_error"]


async def test_along_the_calder_between_kyneton_and_castlemaine(live):
    """The plan's own example, on the real Vicmap road network: a line on the freeway, not a point and
    not a straight chord."""
    kyneton, castlemaine = [144.459671, -37.227406], [144.218266, -37.058717]   # ABS locality points
    out = await call(live(), "trace_segment", {"name": "Calder", "start": kyneton, "end": castlemaine})
    assert out["status"] == "ok" and "CALDER FREEWAY" in out["matched_names"]
    (c,) = out["candidates"]
    line = c["geometry"]["coordinates"]
    assert c["geometry"]["type"] == "LineString" and c["method"] == "trace_segment" and len(line) <= 300
    assert set(c["names_m"]) <= {n for n in out["matched_names"]} and "CALDER FREEWAY" in c["names_m"]
    assert 27_000 < c["length_m"] < 32_000 and c["length_m"] >= c["straight_line_m"]
    assert c["start_offset_m"] < 2500                       # the freeway skirts Kyneton
    assert 4000 < c["end_offset_m"] < 8000                  # and bypasses Castlemaine by about 6 km
    assert c["uncertainty_m"] == c["end_offset_m"] and "anchor_approximate" in c["uncertainty_basis"]
    assert esri.haversine_m(*line[0], *kyneton) < 2500 and esri.haversine_m(*line[-1], *castlemaine) < 8000
    # it runs up the corridor past Malmsbury, Taradale and Elphinstone (rough town positions; the freeway
    # bypasses each by a kilometre or three)
    for town in ([144.3822, -37.1886], [144.3560, -37.1390], [144.3370, -37.1010]):
        assert min(esri.haversine_m(*p, *town) for p in line) < 4000
    wrong = await call(live(), "trace_segment", {"name": "Hume", "start": kyneton, "end": castlemaine})
    assert wrong["status"] in ("no_match", "too_far", "no_path") and wrong["candidates"] == []
