from spatialise_mcp import esri


def sq(x, y, d, clockwise):
    ring = [[x - d, y - d], [x - d, y + d], [x + d, y + d], [x + d, y - d], [x - d, y - d]]
    return ring if clockwise else ring[::-1]


def test_point_line_and_empty():
    assert esri.to_geojson({"x": 144.5, "y": -37.2}) == {"type": "Point", "coordinates": [144.5, -37.2]}
    assert esri.to_geojson({"x": "NaN", "y": "NaN"}) is None
    assert esri.to_geojson(None) is None
    line = esri.to_geojson({"paths": [[[0, 0], [1, 1]]]})
    assert line == {"type": "LineString", "coordinates": [[0.0, 0.0], [1.0, 1.0]]}
    assert esri.to_geojson({"paths": [[[0, 0], [1, 1]], [[2, 2], [3, 3]]]})["type"] == "MultiLineString"


def test_esri_polygon_becomes_rfc7946_winding():
    g = esri.to_geojson({"rings": [sq(0, 0, 2, clockwise=True), sq(0, 0, 1, clockwise=False)]})
    assert g["type"] == "Polygon" and len(g["coordinates"]) == 2
    assert esri.signed_area(g["coordinates"][0]) > 0   # exterior anticlockwise
    assert esri.signed_area(g["coordinates"][1]) < 0   # hole clockwise


def test_holes_go_to_the_exterior_that_contains_them():
    g = esri.to_geojson({"rings": [sq(0, 0, 2, True), sq(10, 0, 2, True), sq(10, 0, 1, False)]})
    assert g["type"] == "MultiPolygon"
    assert [len(p) for p in g["coordinates"]] == [1, 2]


def test_unclosed_ring_is_closed_and_slivers_dropped():
    g = esri.to_geojson({"rings": [[[0, 0], [0, 1], [1, 1], [1, 0]], [[5, 5], [5, 6]]]})
    assert g["type"] == "Polygon" and g["coordinates"][0][0] == g["coordinates"][0][-1]


def test_geojson_to_esri_winding_and_round_trip():
    gj = {"type": "Polygon", "coordinates": [sq(0, 0, 2, False), sq(0, 0, 1, True)]}  # RFC 7946
    e = esri.to_esri(gj)
    assert esri.signed_area(e["rings"][0]) < 0 and esri.signed_area(e["rings"][1]) > 0
    assert e["spatialReference"] == {"wkid": 4326}
    back = esri.to_geojson(e)
    assert back == {"type": "Polygon", "coordinates": [[list(map(float, p)) for p in r] for r in gj["coordinates"]]}
    # wrongly wound input is normalised, not trusted
    e2 = esri.to_esri({"type": "Polygon", "coordinates": [sq(0, 0, 2, True)]})
    assert esri.signed_area(e2["rings"][0]) < 0
    assert len(esri.to_esri({"type": "MultiPolygon", "coordinates": [[sq(0, 0, 1, False)], [sq(5, 5, 1, False)]]})["rings"]) == 2
    assert esri.to_esri({"type": "LineString", "coordinates": [[0, 0], [1, 1]]})["paths"] == [[[0.0, 0.0], [1.0, 1.0]]]


def test_helpers():
    poly = {"type": "Polygon", "coordinates": [sq(144, -37, 0.1, False)]}
    assert [round(v, 9) for v in esri.representative_point(poly)] == [144.0, -37.0]
    assert [round(v, 9) for v in esri.bbox(poly)] == [143.9, -37.1, 144.1, -36.9]
    assert esri.vertex_count(poly) == 5
    assert 380 < esri.area_km2(poly) < 400          # 0.2 deg square at 37S is about 394 km2
    assert 110_000 < esri.haversine_m(144, -37, 144, -36) < 112_000
    c_shape = {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 1], [1, 1], [1, 9], [10, 9], [10, 10], [0, 10], [0, 0]]]}
    x, y = esri.representative_point(c_shape)
    assert esri.ring_contains(c_shape["coordinates"][0], x, y) or [x, y] in c_shape["coordinates"][0]


def test_sql_literals_cannot_break_out():
    assert esri.sql_text("O'BRIEN") == "'O''BRIEN'"
    assert esri.sql_text("x' OR '1'='1") == "'x'' OR ''1''=''1'"
    assert esri.sql_text("a\x00b\nc") == "'abc'"
    assert esri.like_text("50%_OFF'", prefix=True, suffix=True) == "'%50OFF''%'"
