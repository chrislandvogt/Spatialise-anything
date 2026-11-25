import pytest

import build_geometry as B
import geodesy as G


@pytest.mark.parametrize("s,brg,half", [("N", 0, 22.5), ("north", 0, 22.5), ("north-east", 45, 22.5),
                                        ("NNE", 22.5, 11.25), ("south-westerly", 225, 22.5),
                                        ("north-north-west", 337.5, 11.25), ("045", 45, 2),
                                        ("N45E", 45, 2), ("S30W", 210, 2), ("135°", 135, 2)])
def test_bearings(s, brg, half):
    assert B.parse_bearing(s) == (brg, half)


@pytest.mark.parametrize("s,m", [("200 m", 200), ("2km", 2000), ("1.5 km", 1500), ("about 5 km", 5000),
                                 ("3 miles", 4828.032), ("2 nautical miles", 3704), ("500 metres", 500)])
def test_distances(s, m):
    assert abs(B.parse_distance(s) - m) < 1e-6


def test_offset_distance_and_uncertainty():
    out = B.offset((144.2183, -37.0587), "south-east", "5 km", anchor_unc=2511)
    lon, lat = out["geometry"]["coordinates"]
    d, brg = G.inverse(144.2183, -37.0587, lon, lat)
    assert abs(d - 5000) < 0.01 and abs(brg - 135) < 0.01
    assert out["uncertainty_m"] > 2511 + 2000


def test_buffer_is_closed_anticlockwise_and_round():
    out = B.buffer((144.46, -37.23), "2 km")
    ring = out["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1]
    area2 = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(ring, ring[1:]))
    assert area2 > 0  # anticlockwise
    for x, y in ring:
        assert abs(G.inverse(144.46, -37.23, x, y)[0] - 2000) < 1


def test_sector_and_line():
    s = B.sector((144.28, -36.76), "N", "30 km")
    assert s["geometry"]["type"] == "Polygon"
    ln = B.line([(144.46, -37.23), (144.22, -37.06)])
    assert ln["geometry"]["type"] == "LineString" and len(ln["geometry"]["coordinates"]) > 10
