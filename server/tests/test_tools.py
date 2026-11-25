"""The read-only tools, through the MCP protocol, against the fake portal."""

import pytest

from conftest import call
from fake_arcgis import PORTAL, PUBLIC, SERVICES, WORLD
from spatialise_mcp import arcgis as arcgis_mod
from spatialise_mcp import esri

pytestmark = pytest.mark.anyio

AGOL = {"portal": {"url": "https://www.arcgis.com"},
        "locators": [{"name": "world", "url": WORLD, "kind": "public"}], "gazetteers": [], "boundaries": []}


def calls_to(fake, suffix):
    return [c for c in fake.calls if c["path"].endswith(suffix)]


# ---------------------------------------------------------------- list_resolvers

async def test_list_resolvers_reports_policy_and_blocks(make_app):
    app = make_app({"locators": [
        {"name": "customer-addresses", "url": f"{SERVICES}/Addresses/GeocodeServer", "kind": "customer"},
        {"name": "world", "url": WORLD, "kind": "public"}]})
    out = await call(app, "list_resolvers")
    by = {l["name"]: l for l in out["locators"]}
    assert by["customer-addresses"]["allowed"] and by["customer-addresses"]["default"]
    assert by["world"]["allowed"] is False and "disabled by server policy" in by["world"]["blocked_reason"]
    assert by["world"]["billed"] and by["world"]["credits_per_1000"] == 40 and by["world"]["stored_results"]
    assert out["policy"]["public_geocoder_allowed"] is False
    assert [b["name"] for b in out["boundaries"]] == ["sa2", "zones"]
    assert out["publish"]["enabled"] and out["not_available"] == []


async def test_list_resolvers_check_reports_broken_services(make_app, fake):
    app = make_app({"gazetteers": [{"name": "gone", "url": f"{SERVICES}/Nope/FeatureServer/0", "name_field": "N"}]})
    out = await call(app, "list_resolvers", {"check": True})
    assert out["locators"][0]["status"] == "ok"
    assert out["gazetteers"][0]["status"].startswith("unavailable")
    assert all("_url" not in e for e in out["locators"] + out["gazetteers"] + out["boundaries"])


async def test_nothing_configured_says_what_is_missing(make_app):
    out = await call(make_app({"locators": [], "gazetteers": [], "boundaries": []}, mode="none"), "list_resolvers")
    assert len(out["not_available"]) == 3 and out["publish"]["enabled"] is False
    assert "sign-in" in out["publish"]["reason"]


# ---------------------------------------------------------------- geocode

async def test_geocode_returns_candidates_for_the_contract(make_app, fake):
    out = await call(make_app(), "geocode", {"queries": ["Bendigo Town Hall, 189-193 Hargreaves Street, Bendigo VIC 3550"]})
    assert out["status"] == "ok" and out["resolver"] == {"name": "customer-addresses", "version": "2026-11"}
    r = out["results"][0]
    assert r["status"] == "matched" and r["index"] == 0
    c = r["candidates"][0]
    assert c["label"] == "189-193 Hargreaves Street, Bendigo, Victoria, 3550"
    assert c["point"] == [144.2825028, -36.7588188] and c["kind"] == "PointAddress"
    assert c["method"] == "locator" and c["resolver"]["name"] == "customer-addresses"
    assert c["uncertainty_m"] == 15 and "PointAddress" in c["uncertainty_basis"]
    assert 0.8 <= c["score"] <= 1.0 and c["attributes"]["PlaceName"] == "Bendigo Town Hall"
    assert "estimated_credits" not in out  # a customer locator is not billed


async def test_geocode_sends_the_token_in_the_body_only_and_no_storage_flag(make_app, fake):
    await call(make_app(), "geocode", {"queries": ["Kyneton Town Hall"]})
    (req,) = calls_to(fake, "/findAddressCandidates")
    assert req["method"] == "POST" and req["query"] == {}          # nothing in the URL, so nothing in logs
    assert req["form"]["token"].startswith("agol-") and req["form"]["f"] == "json"
    assert req["form"]["SingleLine"] == "Kyneton Town Hall" and req["form"]["sourceCountry"] == "AUS"
    assert "forStorage" not in req["form"] and req["form"]["outSR"] == "4326"


async def test_area_like_matches_get_uncertainty_from_their_extent(make_app):
    out = await call(make_app(), "geocode", {"queries": ["Railway Place, Bendigo"]})
    c = out["results"][0]["candidates"][0]
    assert c["kind"] == "StreetName" and 200 < c["uncertainty_m"] < 400 and "extent" in c["uncertainty_basis"]


async def test_ambiguous_address_returns_every_candidate_and_does_not_choose(make_app):
    out = await call(make_app(), "geocode", {"queries": ["1 Main Street"], "near": [144.28, -36.76]})
    cands = out["results"][0]["candidates"]
    assert len(cands) == 2 and cands[0]["score"] == cands[1]["score"]
    assert {c["attributes"]["City"] for c in cands} == {"Greendale", "Bridgewater"}
    assert all("distance_km" in c for c in cands)


async def test_no_match_stays_unmatched_with_a_hint(make_app):
    out = await call(make_app(), "geocode", {"queries": ["99 Nowhere Lane, Atlantis"]})
    assert out["results"][0] == {"index": 0, "query": "99 Nowhere Lane, Atlantis", "status": "no_match", "candidates": []}
    assert "stay unresolved" in out["no_match_hint"]


async def test_batch_reports_the_count_before_anything_is_sent(make_app, fake):
    app = make_app()
    args = {"queries": ["Bendigo Town Hall", "Kyneton Town Hall", "Railway Place, Bendigo"]}
    dry = await call(app, "geocode", args)
    assert dry["status"] == "confirmation_required" and dry["count"] == 3
    assert dry["sends_text_to"] == "gis.portal.test" and dry["estimated_credits"] == 0
    assert "Nothing has been sent yet" in dry["message"]
    assert calls_to(fake, "/findAddressCandidates") == []          # the property that matters
    out = await call(app, "geocode", {**args, "confirm_token": dry["confirm_token"]})
    assert out["status"] == "ok" and out["geocodes_used"] == 3
    assert [r["index"] for r in out["results"]] == [0, 1, 2]
    assert len(calls_to(fake, "/findAddressCandidates")) == 3


async def test_confirm_token_is_bound_to_the_exact_batch(make_app, fake):
    app = make_app()
    dry = await call(app, "geocode", {"queries": ["Bendigo Town Hall", "Kyneton Town Hall"]})
    bigger = await call(app, "geocode", {"queries": ["Bendigo Town Hall", "Kyneton Town Hall", "1 Main Street"],
                                         "confirm_token": dry["confirm_token"]})
    assert "does not match this request" in bigger["_error"]
    forged = await call(app, "geocode", {"queries": ["a", "b"], "confirm_token": "9999999999.deadbeef"})
    assert "does not match" in forged["_error"]
    assert "malformed" in (await call(app, "geocode", {"queries": ["a", "b"], "confirm_token": "nonsense"}))["_error"]
    cross = await call(app, "reverse_geocode", {"points": [[144.28, -36.76], [144.45, -37.25]],
                                                "confirm_token": dry["confirm_token"]})
    assert "_error" in cross
    assert calls_to(fake, "/findAddressCandidates") == [] and calls_to(fake, "/reverseGeocode") == []


async def test_confirm_every_geocode_when_policy_says_zero(make_app, fake):
    app = make_app({"policy": {"confirm_batches_over": 0}})
    dry = await call(app, "geocode", {"queries": ["Bendigo Town Hall"]})
    assert dry["status"] == "confirmation_required" and dry["count"] == 1


async def test_batch_over_the_limit_is_refused(make_app):
    out = await call(make_app({"policy": {"max_batch": 2}}), "geocode", {"queries": ["a", "b", "c"]})
    assert "over this server's limit of 2" in out["_error"]


async def test_public_geocoder_is_blocked_unless_enabled(make_app, fake):
    out = await call(make_app(AGOL), "geocode", {"queries": ["Bendigo"], "resolver": "world"})
    assert "disabled by server policy" in out["_error"] and "needs_network" in out["_error"]
    out = await call(make_app(AGOL), "geocode", {"queries": ["Bendigo"]})
    assert "blocked by policy" in out["_error"]
    assert fake.calls == []                                        # not even a service-info request went out


async def test_public_geocoder_when_enabled_is_stored_billed_and_counted(make_app, fake):
    app = make_app({**AGOL, "policy": {"allow_public_geocoder": True}})
    dry = await call(app, "geocode", {"queries": ["Bendigo", "Bendigo VIC"]})
    assert dry["status"] == "confirmation_required" and dry["sends_text_to"] == "geocode.arcgis.com"
    assert dry["stored_results"] is True and dry["estimated_credits"] == 0.08 and "0.08 ArcGIS credits" in dry["message"]
    out = await call(app, "geocode", {"queries": ["Bendigo"]})
    assert out["estimated_credits"] == 0.04 and out["stored_results"] is True
    (req,) = calls_to(fake, "/findAddressCandidates")
    assert req["form"]["forStorage"] == "true" and req["form"]["token"]
    c = out["results"][0]["candidates"][0]
    assert c["kind"] == "Locality" and c["uncertainty_m"] > 5000   # a town centroid is honest about its size


async def test_unknown_resolver_and_bad_inputs_give_actionable_errors(make_app):
    app = make_app()
    assert "list_resolvers" in (await call(app, "geocode", {"queries": ["x"], "resolver": "nope"}))["_error"]
    assert "latitude first" in (await call(app, "geocode", {"queries": ["x"], "near": [-36.7, 144.2]}))["_error"]
    assert "non-empty text" in (await call(app, "geocode", {"queries": ["  "]}))["_error"]
    assert "min below max" in (await call(app, "geocode", {"queries": ["x"], "bbox": [145, -37, 144, -36]}))["_error"]


async def test_locator_with_its_own_single_line_field_name(make_app, fake):
    fake.locators["gis.portal.test/Addresses"].single_line = "Single Line Input"
    await call(make_app(), "geocode", {"queries": ["Kyneton Town Hall"]})
    assert calls_to(fake, "/findAddressCandidates")[0]["form"]["Single Line Input"] == "Kyneton Town Hall"


# ---------------------------------------------------------------- reverse_geocode

async def test_reverse_geocode_labels_but_never_moves(make_app):
    out = await call(make_app(), "reverse_geocode", {"points": [[144.2826, -36.7589]]})
    r = out["results"][0]
    assert r["status"] == "matched" and r["label"].startswith("189-193 Hargreaves Street")
    assert r["point"] == [144.2826, -36.7589] and r["matched_point"] == [144.2825028, -36.7588188]
    assert 0 < r["distance_m"] < 30 and r["kind"] == "PointAddress"


async def test_reverse_geocode_no_match_and_batch_confirmation(make_app, fake):
    app = make_app()
    out = await call(app, "reverse_geocode", {"points": [[150.0, -30.0]]})
    assert out["results"][0]["status"] == "no_match"
    dry = await call(app, "reverse_geocode", {"points": [[144.28, -36.76], [144.45, -37.25]]})
    assert dry["status"] == "confirmation_required" and dry["count"] == 2 and "coordinates" in dry["message"]
    out = await call(app, "reverse_geocode", {"points": [[144.28, -36.76], [144.45, -37.25]],
                                              "confirm_token": dry["confirm_token"]})
    assert [r["status"] for r in out["results"]] == ["matched", "matched"]


# ---------------------------------------------------------------- search_gazetteer

async def test_gazetteer_returns_every_feature_type_for_a_name(make_app):
    out = await call(make_app(), "search_gazetteer", {"names": ["Lake Eppalock"]})
    assert out["gazetteers"] == ["placenames", "water"]
    cands = out["results"][0]["candidates"]
    assert sorted((c["resolver"]["name"], c["kind"]) for c in cands) == [
        ("placenames", "LAKE"), ("placenames", "LOCALITY"), ("water", "reservoir")]
    lake = next(c for c in cands if c["kind"] == "LAKE")
    assert lake["label"] == "LAKE EPPALOCK" and lake["id"] == "placenames:15001" and lake["state"] == "VIC"
    assert lake["point"] == [144.5686861, -36.8696968] and lake["method"] == "gazetteer"
    assert lake["uncertainty_m"] == 5000 and "default_uncertainty" in lake["uncertainty_basis"]
    assert lake["resolver"] == {"name": "placenames", "version": "test-1"}
    poly = next(c for c in cands if c["kind"] == "reservoir")       # a polygon layer, asked for as a point
    assert "geometry" not in poly and poly["geometry_type"] == "Polygon" and len(poly["bbox"]) == 4
    assert 3000 < poly["uncertainty_m"] < 5000 and "extent" in poly["uncertainty_basis"]
    assert poly["resolver"]["version"].startswith("ArcGIS REST")    # falls back to the service version


async def test_gazetteer_same_name_different_things(make_app):
    out = await call(make_app(), "search_gazetteer", {"names": ["Richmond"], "gazetteer": "placenames",
                                                      "near": [144.99, -37.82]})
    cands = out["results"][0]["candidates"]
    assert [c["kind"] for c in cands] == ["RAIL STATION", "POST OFFICE", "LOCALITY"]  # nearest first
    assert all(c["exact"] for c in cands) and cands[0]["distance_km"] < cands[-1]["distance_km"]
    only = await call(make_app(), "search_gazetteer", {"names": ["Richmond"], "feature_types": ["locality"],
                                                       "gazetteer": "placenames"})
    assert [c["kind"] for c in only["results"][0]["candidates"]] == ["LOCALITY"]


async def test_gazetteer_base_filter_and_match_modes(make_app):
    app = make_app()
    hist = await call(app, "search_gazetteer", {"names": ["Old Name"], "gazetteer": "placenames"})
    assert hist["results"][0]["candidates"] == []                   # HISTORICAL names are filtered by config
    pre = await call(app, "search_gazetteer", {"names": ["Richmond"], "gazetteer": "placenames", "match": "prefix"})
    labels = [c["label"] for c in pre["results"][0]["candidates"]]
    assert "RICHMOND PARK" in labels and labels.index("RICHMOND") < labels.index("RICHMOND PARK")  # exact first
    sub = await call(app, "search_gazetteer", {"names": ["eppalock"], "gazetteer": "placenames", "match": "contains"})
    assert len(sub["results"][0]["candidates"]) == 2


async def test_gazetteer_text_cannot_inject_sql(make_app, fake):
    app = make_app()
    ok = await call(app, "search_gazetteer", {"names": ["O'Briens Crossing"], "gazetteer": "placenames"})
    assert [c["label"] for c in ok["results"][0]["candidates"]] == ["O'BRIENS CROSSING"]
    for attack in ["x' OR '1'='1", "x') OR (1=1", "RICHMOND' --", "%"]:
        out = await call(app, "search_gazetteer", {"names": [attack], "gazetteer": "placenames"})
        assert out["results"][0]["candidates"] == [], attack
    wild = await call(app, "search_gazetteer", {"names": ["%"], "gazetteer": "placenames", "match": "contains"})
    assert "_error" in wild or len(wild["results"][0]["candidates"]) <= 10
    where = calls_to(fake, "/query")[1]["form"]["where"]
    assert "'X'' OR ''1''=''1'" in where and "PLACE_NAME_STATUS = 'REGISTERED'" in where


async def test_gazetteer_limit_truncation_and_bbox(make_app):
    app = make_app()
    out = await call(app, "search_gazetteer", {"names": ["Richmond"], "gazetteer": "placenames", "limit": 2})
    r = out["results"][0]
    assert r["count"] == 2 and r["truncated"] is True and "narrow" in r["hint"]
    boxed = await call(app, "search_gazetteer", {"names": ["Richmond"], "gazetteer": "placenames",
                                                 "bbox": [144.98, -37.83, 144.995, -37.82]})
    assert [c["kind"] for c in boxed["results"][0]["candidates"]] == ["RAIL STATION"]


async def test_gazetteer_can_return_shape_within_a_vertex_budget(make_app):
    out = await call(make_app(), "search_gazetteer", {"names": ["Lake Eppalock"], "gazetteer": "water",
                                                      "geometry": "full"})
    c = out["results"][0]["candidates"][0]
    assert c["geometry"]["type"] == "Polygon" and esri.signed_area(c["geometry"]["coordinates"][0]) > 0
    assert c["uncertainty_m"] == 0 and c["uncertainty_basis"] == "full geometry as served"


async def test_wrong_field_in_config_is_named_clearly(make_app):
    app = make_app({"gazetteers": [{"name": "bad", "url": f"{SERVICES}/Placenames/FeatureServer/0",
                                    "name_field": "PLACENAME"}]})
    out = await call(app, "search_gazetteer", {"names": ["Richmond"]})
    assert "field 'PLACENAME' is not in the layer" in out["_error"] and "PLACE_NAME" in out["_error"]
    svc = make_app({"gazetteers": [{"name": "svc", "url": f"{SERVICES}/Placenames/FeatureServer", "name_field": "N"}]})
    assert "not a queryable layer" in (await call(svc, "search_gazetteer", {"names": ["x"]}))["_error"]


async def test_no_gazetteer_configured_points_back_to_the_contract(make_app):
    out = await call(make_app({"gazetteers": []}), "search_gazetteer", {"names": ["Lake Eppalock"]})
    assert "needs_network" in out["_error"]


# ---------------------------------------------------------------- lookup_boundary

async def test_boundary_by_code_is_simplified_to_the_vertex_budget(make_app, fake):
    out = await call(make_app(), "lookup_boundary", {"boundary": "sa2", "codes": ["202021030"], "max_vertices": 100})
    assert out["resolver"] == {"name": "sa2", "version": "ASGS 2021"}
    c = out["results"][0]["candidates"][0]
    assert c["id"] == "sa2:202021030" and c["label"] == "Kyneton" and c["state"] == "Victoria"
    assert c["method"] == "boundary_join" and c["kind"] == "sa2"
    assert c["vertices"] <= 100 and c["geometry"]["type"] == "Polygon"
    assert c["uncertainty_m"] > 0 and "simplified to about" in c["uncertainty_basis"]
    assert c["area_km2"] > 100 and c["radius_m"] > 5000 and len(c["point"]) == 2
    offsets = [q["form"].get("maxAllowableOffset") for q in calls_to(fake, "/query")]
    assert len(offsets) >= 2 and offsets == sorted(offsets, key=float)   # it climbed the ladder


async def test_boundary_holes_and_multipart_convert_correctly(make_app):
    app = make_app()
    out = await call(app, "lookup_boundary", {"boundary": "sa2", "names": ["castlemaine", "Islands"]})
    donut = out["results"][0]["candidates"][0]["geometry"]
    assert donut["type"] == "Polygon" and len(donut["coordinates"]) == 2
    assert esri.signed_area(donut["coordinates"][0]) > 0 > esri.signed_area(donut["coordinates"][1])
    islands = out["results"][1]["candidates"][0]["geometry"]
    assert islands["type"] == "MultiPolygon" and len(islands["coordinates"]) == 2
    assert out["results"][0]["by"] == "name" and out["results"][0]["query"] == "castlemaine"


async def test_public_boundary_service_never_sees_the_token(make_app, fake):
    await call(make_app(), "lookup_boundary", {"boundary": "sa2", "codes": ["202021030"]})
    public = [c for c in fake.calls if c["host"] == "public.example"]
    assert public and all("token" not in c["form"] for c in public)


async def test_boundary_name_state_filter_and_no_match(make_app):
    app = make_app()
    out = await call(app, "lookup_boundary", {"boundary": "sa2", "names": ["Richmond (Vic.)", "Atlantis"],
                                              "state": "Victoria", "geometry": "point"})
    c = out["results"][0]["candidates"][0]
    assert c["code"] == "206041122" and "geometry" not in c and c["uncertainty_m"] == c["radius_m"]
    assert out["results"][1]["count"] == 0 and "stays unresolved" in out["no_match_hint"]


async def test_boundary_numeric_codes_and_layers_without_pagination(make_app, fake):
    app = make_app()
    out = await call(app, "lookup_boundary", {"boundary": "Zones", "codes": ["7"], "geometry": "none"})
    c = out["results"][0]["candidates"][0]
    assert c["label"] == "North depot zone" and c["code"] == "7" and "point" not in c
    q = calls_to(fake, "/query")[-1]["form"]
    assert q["where"] == "ZONE_NO IN (7)" and "resultRecordCount" not in q and q["returnGeometry"] == "false"
    assert q["token"]                                               # the customer's own layer does get the token
    bad = await call(app, "lookup_boundary", {"boundary": "zones", "codes": ["7 OR 1=1"]})
    assert "is not a number" in bad["_error"]


async def test_boundary_input_errors(make_app):
    app = make_app()
    assert "codes, names or both" in (await call(app, "lookup_boundary", {"boundary": "sa2"}))["_error"]
    assert "Configured: ['sa2', 'zones']" in (await call(app, "lookup_boundary", {"boundary": "lga", "codes": ["1"]}))["_error"]
    full = await call(app, "lookup_boundary", {"boundary": "sa2", "codes": ["202021030"], "geometry": "full",
                                               "max_vertices": 50})
    assert "over max_vertices=50" in full["results"][0]["candidates"][0]["geometry_omitted"]


# ---------------------------------------------------------------- the ArcGIS client

async def test_transient_failures_are_retried_then_reported(make_app, fake, monkeypatch):
    monkeypatch.setattr(arcgis_mod, "DEFAULT_BACKOFF_S", 0)
    app = make_app()
    fake.fail_next = [503]
    assert (await call(app, "lookup_boundary", {"boundary": "sa2", "codes": ["202021030"]}))["status"] == "ok"
    fake.fail_next = [503, 502, 429]
    out = await call(app, "lookup_boundary", {"boundary": "zones", "codes": ["7"]})
    assert "busy or unavailable" in out["_error"] and "gis.portal.test" in out["_error"]


async def test_expired_token_asks_the_user_to_sign_in_again(make_app, fake):
    app = make_app(token=fake.issue_token(ttl=-10))
    out = await call(app, "geocode", {"queries": ["Kyneton Town Hall"]})
    assert "sign-in" in out["_error"] and "expired" in out["_error"] and "Reconnect" in out["_error"]


async def test_secured_service_without_sign_in_says_so(make_app):
    out = await call(make_app(mode="none"), "geocode", {"queries": ["Kyneton Town Hall"]})
    assert "needs an ArcGIS sign-in" in out["_error"] and "auth_mode = oauth" in out["_error"]
    ok = await call(make_app(mode="none"), "lookup_boundary", {"boundary": "sa2", "codes": ["202021030"]})
    assert ok["status"] == "ok"                                     # public layers still work with no sign-in


async def test_non_arcgis_endpoint_is_reported_plainly(make_app):
    app = make_app({"boundaries": [{"name": "junk", "url": f"{PUBLIC}/../../index.html", "code_field": "c",
                                    "auth": "none"}]})
    out = await call(app, "lookup_boundary", {"boundary": "junk", "codes": ["1"]})
    assert "HTTP 404" in out["_error"] or "did not return JSON" in out["_error"]


async def test_client_refuses_to_send_a_token_to_an_untrusted_host(make_app, fake):
    from spatialise_mcp.arcgis import ArcGIS, StaticIdentity
    from spatialise_mcp.errors import SpatialiseError
    app = make_app()
    client = ArcGIS(app.cfg, app.http, StaticIdentity("secret-token"))
    with pytest.raises(SpatialiseError, match="untrusted host public.example"):
        await client.call(f"{PUBLIC}/SA2/MapServer/0", {}, auth=True)
    assert fake.calls == []
    with pytest.raises(SpatialiseError, match="needs a token"):        # asked without auth: none is attached
        await client.call(f"{PORTAL}/sharing/rest/portals/self", {}, auth=False)
    assert "token" not in fake.calls[-1]["form"]


# ---------------------------------------------------------------- trace_segment

A, B = [144.05, -36.99], [144.33, -37.02]      # two "towns", each about a kilometre or two off the road


async def test_trace_follows_the_named_road_between_two_anchors(make_app, fake):
    out = await call(make_app(), "trace_segment", {"name": "Main", "start": A, "end": B})
    assert out["status"] == "ok" and out["network"] == "roads" and out["resolver"] == {"name": "roads", "version": "roads-1"}
    assert out["matched_names"] == {"MAIN HIGHWAY": 14, "MAIN FREEWAY": 6}      # MAIN STREET is outside the corridor
    (c,) = out["candidates"]
    line = c["geometry"]["coordinates"]
    assert c["geometry"]["type"] == "LineString" and c["method"] == "trace_segment" and c["kind"] == "roads"
    assert abs(line[0][0] - 144.05) < 0.006 and abs(line[-1][0] - 144.33) < 0.001   # nearest point on a wiggly road
    assert all(-37.0001 <= p[1] <= -36.9979 for p in line)                     # on the road, not a straight chord
    xs = [p[0] for p in line]
    assert xs == sorted(xs)
    assert c["label"] == "MAIN HIGHWAY / MAIN FREEWAY" and list(c["names_m"]) == ["MAIN HIGHWAY", "MAIN FREEWAY"]
    assert 26_800 < c["length_m"] < 27_800 and c["length_m"] > c["straight_line_m"]   # 24.4 km straight, plus the wiggles
    assert 900 < c["start_offset_m"] < 1200 and 2100 < c["end_offset_m"] < 2300
    assert c["uncertainty_m"] == c["end_offset_m"] and "anchor_approximate" in c["uncertainty_basis"]
    assert c["gaps_m"] and 80 < c["gaps_m"][0] < 95 and "bridged" in c["gaps_note"]
    assert c["point"] in line and c["vertices"] == len(line)


async def test_trace_pages_through_a_layer_that_serves_eight_at_a_time(make_app, fake):
    out = await call(make_app(), "trace_segment", {"name": "Main", "start": A, "end": B})
    pages = calls_to(fake, "/query")
    assert len(pages) == 3 and [p["form"]["resultOffset"] for p in pages] == ["0", "8", "16"]
    assert out["segments"] == 20 and "incomplete" not in out
    assert all(p["form"]["token"] for p in pages) and "LIKE 'MAIN%'" in pages[0]["form"]["where"]
    assert pages[0]["form"]["geometryType"] == "esriGeometryEnvelope"


async def test_trace_exact_name_stops_where_the_name_does(make_app):
    out = await call(make_app(), "trace_segment", {"name": "Main Highway", "start": A, "end": B, "match": "exact"})
    assert out["matched_names"] == {"MAIN HIGHWAY": 14} and out["status"] == "ok"
    (c,) = out["candidates"]
    assert c["label"] == "MAIN HIGHWAY" and c["geometry"]["coordinates"][-1][0] <= 144.2801
    assert c["end_offset_m"] > 4000 and c["uncertainty_m"] == c["end_offset_m"]   # honest about the short end


async def test_trace_refuses_rather_than_guesses(make_app, fake):
    app = make_app()
    none = await call(app, "trace_segment", {"name": "Calder", "start": A, "end": B})
    assert none["status"] == "no_match" and none["candidates"] == [] and "stays unresolved" in none["hint"]
    far = await call(app, "trace_segment", {"name": "Main", "start": A, "end": [144.33, -37.3], "corridor_km": 40})
    assert far["status"] == "too_far" and far["candidates"] == [] and far["end_offset_m"] > 30_000
    assert "probably not the\\n" not in far["hint"] and "MAIN" in far["hint"]
    fake.services["gis.portal.test/Roads"]["layers"][0].features[9]["attributes"]["ROAD_NAME"] = "HIGH STREET"
    split = await call(app, "trace_segment", {"name": "Main", "start": A, "end": B})
    assert split["status"] == "no_path" and "do not draw a straight line" in split["hint"]
    assert "do not join up" in split["hint"]


async def test_trace_respects_the_vertex_budget_and_input_rules(make_app):
    app = make_app()
    small = await call(app, "trace_segment", {"name": "Main", "start": A, "end": B, "max_vertices": 20})
    c = small["candidates"][0]
    assert c["vertices"] <= 20 and c["uncertainty_m"] >= 25
    assert "same place" in (await call(app, "trace_segment", {"name": "Main", "start": A, "end": A}))["_error"]
    long = await call(app, "trace_segment", {"name": "Main", "start": [115.8, -31.9], "end": [153.0, -27.5]})
    assert "shorter stretches" in long["_error"]
    wrong = await call(app, "trace_segment", {"name": "Main", "start": A, "end": B, "network": "rail"})
    assert "Configured: ['roads']" in wrong["_error"]
    none = await call(make_app({"networks": []}), "trace_segment", {"name": "Main", "start": A, "end": B})
    assert "needs_network" in none["_error"]
    poly = make_app({"networks": [{"name": "bad", "url": f"{SERVICES}/Water/FeatureServer/0", "name_field": "NAME"}]})
    assert "tracing needs lines" in (await call(poly, "trace_segment", {"name": "x", "start": A, "end": B}))["_error"]


async def test_trace_says_when_a_layer_cannot_give_everything(make_app, fake):
    fake.services["gis.portal.test/Roads"]["layers"][0].pagination = False
    out = await call(make_app(), "trace_segment", {"name": "Main", "start": A, "end": B})
    assert "incomplete" in out and out["segments"] == 8


async def test_list_resolvers_includes_networks(make_app):
    out = await call(make_app(), "list_resolvers", {"check": True})
    assert out["networks"] == [{"name": "roads", "host": "gis.portal.test", "needs_sign_in": True,
                                "version": "roads-1", "status": "ok"}]
    assert "trace_segment" in (await call(make_app({"networks": []}), "list_resolvers"))["not_available"]
