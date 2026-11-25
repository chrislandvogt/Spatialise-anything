import random

import pytest

import geodesy as G

pyproj = pytest.importorskip("pyproj")


def test_utm_inverse_matches_proj():
    t = pyproj.Transformer.from_crs(7855, 7844, always_xy=True)
    rnd = random.Random(1)
    for _ in range(200):
        e, n = rnd.uniform(160000, 840000), rnd.uniform(5600000, 8000000)
        a, b = G.utm_to_geographic(e, n, 55), t.transform(e, n)
        assert G.haversine(*a, *b) < 1e-6


def test_utm_forward_matches_proj():
    t = pyproj.Transformer.from_crs(7844, 7854, always_xy=True)
    rnd = random.Random(2)
    for _ in range(200):
        lon, lat = rnd.uniform(138, 144), rnd.uniform(-44, -10)
        e, n, _, _ = G.geographic_to_utm(lon, lat, 54)
        e2, n2 = t.transform(lon, lat)
        assert abs(e - e2) < 1e-6 and abs(n - n2) < 1e-6


def test_gda94_to_gda2020_matches_proj_helmert():
    t = pyproj.Transformer.from_crs(4283, 7844, always_xy=True)
    rnd = random.Random(3)
    for _ in range(100):
        lon, lat = rnd.uniform(114, 153), rnd.uniform(-43, -11)
        assert G.haversine(*G.gda94_to_gda2020(lon, lat), *t.transform(lon, lat)) < 0.001


def test_gda94_shift_is_about_1_5_m_north_east():
    lon, lat = G.gda94_to_gda2020(144.96, -37.81)
    dist, brg = G.inverse(144.96, -37.81, lon, lat)
    assert 1.4 < dist < 1.9 and 0 < brg < 45


def test_geodesics_match_proj():
    geod = pyproj.Geod(ellps="GRS80")
    rnd = random.Random(4)
    for _ in range(200):
        lon, lat = rnd.uniform(113, 153), rnd.uniform(-43, -11)
        az, d = rnd.uniform(0, 360), rnd.uniform(1, 300000)
        x, y, _ = geod.fwd(lon, lat, az, d)
        assert G.haversine(*G.destination(lon, lat, az, d), x, y) < 0.001
        lon2, lat2 = rnd.uniform(113, 153), rnd.uniform(-43, -11)
        dist, _ = G.inverse(lon, lat, lon2, lat2)
        assert abs(dist - geod.inv(lon, lat, lon2, lat2)[2]) < 0.001


def test_mgrs_matches_reference():
    mgrs = pytest.importorskip("mgrs")
    m = mgrs.MGRS()
    rnd = random.Random(5)
    for _ in range(200):
        lon, lat = rnd.uniform(113, 153), rnd.uniform(-43, -11)
        s = m.toMGRS(lat, lon, MGRSPrecision=5)
        e, n, south, prec = G.mgrs_to_utm(int(s[:2]), s[2], s[3], s[4], s[5:10], s[10:15])
        lon2, lat2 = G.utm_to_geographic(e, n, int(s[:2]), south)
        assert G.haversine(lon, lat, lon2, lat2) < 1.0  # inside the 1 m cell
