from pathlib import Path

import read_exif as X

PHOTOS = Path(__file__).resolve().parent.parent / "corpus" / "items" / "t0-photos"


def test_gps_heading_and_error():
    r = X.record(PHOTOS / "IMG_0001.jpg")
    assert abs(r["lat"] - -37.2465) < 1e-6 and abs(r["lon"] - 144.4551) < 1e-6
    assert r["heading_deg"] == 270 and r["heading_ref"] == "true"
    f = X.to_feature(r)
    assert f["properties"]["uncertainty_m"] == 4.5 and f["properties"]["source_crs"] == "EPSG:4326"


def test_gps_only_gets_default_uncertainty_and_assumed_datum():
    f = X.to_feature(X.record(PHOTOS / "IMG_0002.jpg"))
    assert set(f["properties"]["flags"]) == {"default_uncertainty", "datum_assumed"}


def test_no_gps_is_unresolved_not_dropped():
    f = X.to_feature(X.record(PHOTOS / "IMG_0003.jpg"))
    assert f["geometry"] is None and f["properties"]["status"] == "unresolved"


def test_not_an_image(tmp_path):
    p = tmp_path / "x.jpg"
    p.write_bytes(b"not a jpeg")
    r = X.record(p)
    assert "error" in r and X.to_feature(r)["properties"]["status"] == "unresolved"
