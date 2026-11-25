import parse_coords as P


def dets(text, **kw):
    kw.setdefault("frame", P.FRAMES["AU"])
    return P.detect(text, **kw)


def one(text, **kw):
    d = dets(text, **kw)
    assert len(d) == 1, d
    return d[0]


def test_decimal_pair_with_stated_datum():
    d = one("Pump at -37.24712, 144.45291 (GDA2020).")
    assert d["mention"] == "-37.24712, 144.45291"
    assert d["source_crs"] == "EPSG:7844" and "datum_assumed" not in d["flags"]
    assert d["geometry"]["coordinates"] == [144.45291, -37.24712]


def test_span_offsets_point_at_the_mention():
    text = "Header line\nBore located 37°03'50.4\"S 144°13'01.2\"E."
    d = one(text)
    assert text[d["char_start"]:d["char_end"]] == d["mention"]


def test_dms_and_ddm():
    d = one("37°03'50.4\"S 144°13'01.2\"E")
    lon, lat = d["geometry"]["coordinates"]
    assert abs(lat - -(37 + 3 / 60 + 50.4 / 3600)) < 1e-7 and abs(lon - (144 + 13 / 60 + 1.2 / 3600)) < 1e-7
    d = one("36° 45.42' S, 144° 16.77' E")
    assert abs(d["geometry"]["coordinates"][1] - -(36 + 45.42 / 60)) < 1e-7


def test_no_datum_is_flagged_not_silent():
    d = one("GPS -36.75700, 144.27900")
    assert "datum_assumed" in d["flags"] and "(assumed)" in d["source_crs"]


def test_gda94_is_transformed():
    d = one("GDA94: -36.85640, 144.45720")
    lon, lat = d["geometry"]["coordinates"]
    assert (lon, lat) != (144.4572, -36.8564)
    assert d["source_crs"] == "EPSG:4283"


def test_agd66_flagged_with_wide_uncertainty():
    d = one("AMG66 zone 55, E 273300 N 5918050")
    assert "datum_unsupported" in d["flags"] and d["uncertainty_m"] >= 250


def test_grid_needs_a_zone():
    d = one("Grid ref E 250000 N 5900000 with no zone stated.")
    assert d["status"] == "unresolved"
    d = one("Grid ref E 250000 N 5900000", default_zone=55)
    assert d["status"] == "resolved" and d["confidence"] < 0.9


def test_utm_band_and_mgrs():
    d = one("Gauge 55H 273456 5918234")
    assert d["kind"] == "grid" and d["zone"] == 55
    d = one("MGRS 55HCU 7345 1823")
    assert d["kind"] == "mgrs" and abs(d["uncertainty_m"] - 7.07) < 0.1


def test_wkt_and_ewkt():
    d = one("POINT(144.2794 -36.7570)")
    assert d["geometry"]["coordinates"] == [144.2794, -36.757]
    d = one("SRID=7855;POINT(273456 5918234)")
    assert d["source_crs"] == "EPSG:7855" and 144 < d["geometry"]["coordinates"][0] < 145
    d = one("LINESTRING(144.1 -37.1, 144.2 -37.2)")
    assert d["geometry"]["type"] == "LineString"


def test_traps_are_not_coordinates():
    traps = [
        "Call 03 5422 1234 or +61 3 5422 1234.",
        "Part no. PN-144.9631-37 and PO-144-37-8136, release v2.3.1.",
        "Invoice total $1,234.56, $12.50, 13.75.",
        "Flow ratio 1.25, 3.50.",
        "Measured 12.345, 67.891 mm.",
        "Budget line 1.250, 3.750 approved.",
        "Recorded 14:30 on 12/03/2026, build 2026.11.25.",
        "ABN 12 345 678 901",
        "See pages 3.5, 4.1 of the manual.",
    ]
    for t in traps:
        assert dets(t) == [], t


def test_outside_frame_is_kept_but_ambiguous():
    d = one("GPS 1.2500, 3.7500")
    assert d["status"] == "ambiguous" and "outside_frame" in d["flags"]
    f = P.to_feature(d, "x.txt")
    assert f["geometry"] is None and f["properties"]["candidates"]


def test_lon_lat_order_swap():
    d = one("coordinates 144.96310, -37.81360")
    assert d["geometry"]["coordinates"] == [144.9631, -37.8136]


def test_features_validate():
    from pathlib import Path

    import contract as C
    import validate_features as V
    root = Path(__file__).resolve().parent.parent
    text = (root / "corpus" / "items" / "t0-coords-mixed" / "field-report.txt").read_text(encoding="utf-8")
    fc = C.make_collection([P.to_feature(d, "f.txt") for d in dets(text)])
    errs, _ = V.validate(fc)
    assert not errs and len(fc["features"]) == 8
