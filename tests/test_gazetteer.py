import pytest

import gazetteer as g

@pytest.fixture(scope="module")
def gz():
    return g.Gazetteer()


def labels(res):
    return [c["label"] for c in res["candidates"]]


def test_normalise():
    assert g.normalise("Mt Macedon") == "mount macedon"
    assert g.normalise("Richmond (Vic.)") == "richmond"
    assert g.normalise("St Kilda") == "saint kilda"
    assert g.split_state("Richmond, VIC 3121") == ("Richmond", "VIC")
    assert g.split_state("Kyneton Victoria") == ("Kyneton", "VIC")


def test_geometry_codec_roundtrip():
    geom = {"type": "MultiPolygon", "coordinates": [
        [[[144.1, -37.1], [144.2, -37.1], [144.2, -37.2], [144.1, -37.1]]],
        [[[145.0, -38.0], [145.1, -38.0], [145.1, -38.1], [145.0, -38.0]]]]}
    assert g.decode_geometry(g.encode_geometry(geom)) == geom


def test_bare_richmond_is_a_tie(gz):
    r = gz.search("Richmond")
    assert r["count"] >= 6 and r["margin"] == 0 and r["right_place_hint"] < 0.3


def test_state_settles_richmond(gz):
    r = gz.search("Richmond", state="VIC")
    assert labels(r)[0] == "Richmond, VIC" and r["right_place_hint"] > 0.7


def test_neighbours_settle_richmond(gz):
    r = gz.search("Richmond", near=[(150.82, -33.61)])  # Windsor NSW
    assert labels(r)[0] == "Richmond, NSW"


def test_springfield_near_ipswich(gz):
    r = gz.search("Springfield", near=[(152.76, -27.61)])
    assert r["candidates"][0]["lga"] == "Ipswich"


def test_springfield_lakes_is_its_own_place(gz):
    assert labels(gz.search("Springfield Lakes")) == ["Springfield Lakes, QLD"]


def test_misspelling_caps_the_hint(gz):
    r = gz.search("Castelmaine")
    assert labels(r)[0] == "Castlemaine, VIC" and r["right_place_hint"] < 0.8


def test_lga_and_postcode(gz):
    r = gz.lga("City of Greater Bendigo")
    assert labels(r)[0] == "Greater Bendigo, VIC (LGA)"
    p = gz.by_code("postcode", "3550", geometry="polygon")
    assert p["candidates"][0]["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert gz.by_code("postcode", "9726")["count"] == 0


def test_reverse(gz):
    r = gz.reverse(144.2794, -36.757)
    assert r["locality"]["name"] == "Bendigo" and r["postcode"]["code"] == "3550"
    assert r["lga"]["name"] == "Greater Bendigo"


def test_meta_has_attribution(gz):
    m = gz.meta()
    assert "CC BY 4.0" in m["licence"] and "Australian Bureau of Statistics" in m["attribution"]


def test_same_name_lga_does_not_dilute_its_locality(gz):
    r = gz.search("Woollahra")
    assert r["right_place_hint"] == 1.0
    lga = next(c for c in r["candidates"] if c["kind"] == "lga")
    assert lga["nested_in"] == r["candidates"][0]["id"]


def test_bare_richmond_is_six_places(gz):
    r = gz.search("Richmond")
    rivals = [c for c in r["candidates"] if "nested_in" not in c]
    assert len(rivals) == 6 and all(abs(c["share"] - 1 / 6) < 0.01 for c in rivals)
