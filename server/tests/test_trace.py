"""The tracing algorithm on its own: graphs, snapping, gaps, shortest paths."""

from spatialise_mcp import trace as T
from spatialise_mcp.esri import haversine_m

Y = -37.0


def road(x0, x1, name="MAIN HIGHWAY", y=Y, n=5):
    return ([[x0 + (x1 - x0) * i / n, y] for i in range(n + 1)], name)


def run(lines, start, end, **kw):
    kw = {"max_snap_m": 10_000, "gap_tolerance_m": 250, "max_vertices": 300, **kw}
    return T.trace(lines, start, end, **kw)


def test_follows_connected_segments_between_snapped_anchors():
    lines = [road(144.0, 144.1), road(144.1, 144.2), road(144.2, 144.3)]
    out = run(lines, [144.05, Y + 0.01], [144.25, Y - 0.02])
    assert out["status"] == "ok" and out["names"] == {"MAIN HIGHWAY": out["length_m"]}
    assert abs(out["coords"][0][0] - 144.05) < 1e-6 and abs(out["coords"][-1][0] - 144.25) < 1e-6
    assert all(abs(c[1] - Y) < 1e-9 for c in out["coords"])                 # it stays on the road
    assert abs(out["length_m"] - haversine_m(144.05, Y, 144.25, Y)) < 2
    assert 1050 < out["start_offset_m"] < 1150 and 2150 < out["end_offset_m"] < 2300
    assert out["gaps_m"] == [] and out["simplified_m"] == 0
    xs = [c[0] for c in out["coords"]]
    assert xs == sorted(xs)                                                  # in order, no doubling back


def test_segments_digitised_in_either_direction_join_up():
    a, b, c = road(144.0, 144.1), road(144.2, 144.1), road(144.2, 144.3)   # the middle one runs backwards
    out = run([a, b, c], [144.01, Y], [144.29, Y])
    xs = [p[0] for p in out["coords"]]
    assert out["status"] == "ok" and xs == sorted(xs)
    assert run([a, b, c], [144.29, Y], [144.01, Y])["coords"] == out["coords"][::-1]


def test_both_anchors_on_one_segment_run_directly_between_them():
    out = run([road(144.0, 144.5, n=50)], [144.21, Y + 0.001], [144.24, Y - 0.001])
    assert out["status"] == "ok"
    assert abs(out["length_m"] - haversine_m(144.21, Y, 144.24, Y)) < 2      # not out to a junction and back
    assert 144.2099 < out["coords"][0][0] < 144.2101 and 144.2399 < out["coords"][-1][0] < 144.2401


def test_takes_the_shorter_way_round_and_ignores_unrelated_pieces():
    direct = road(144.0, 144.2)
    loop = ([[144.0, Y], [144.0, Y + 0.1], [144.2, Y + 0.1], [144.2, Y]], "MAIN HIGHWAY")
    elsewhere = road(150.0, 150.1, name="MAIN STREET")
    out = run([loop, direct, elsewhere], [144.0, Y], [144.2, Y])
    assert out["status"] == "ok" and all(abs(c[1] - Y) < 1e-9 for c in out["coords"])
    assert "MAIN STREET" not in out["names"]


def test_a_small_gap_is_bridged_and_reported():
    gap_deg = 0.001                                                          # about 89 m at 37 S
    lines = [road(144.0, 144.1), road(144.1 + gap_deg, 144.2, name="MAIN HIGHWAY")]
    out = run(lines, [144.01, Y], [144.19, Y])
    assert out["status"] == "ok" and len(out["gaps_m"]) == 1 and 80 < out["gaps_m"][0] < 95
    assert T.GAP_NAME not in out["names"]
    assert abs(out["length_m"] - haversine_m(144.01, Y, 144.19, Y)) < 2      # true length, not the penalised one
    strict = run(lines, [144.01, Y], [144.19, Y], gap_tolerance_m=0)
    assert strict["status"] == "no_path" and strict["bridged_gaps"] == 0


def test_a_real_segment_beats_a_gap():
    lines = [road(144.0, 144.1), road(144.1005, 144.2),
             ([[144.1, Y], [144.1, Y + 0.0003], [144.1005, Y + 0.0003], [144.1005, Y]], "MAIN HIGHWAY")]
    out = run(lines, [144.01, Y], [144.19, Y])
    assert out["status"] == "ok" and out["gaps_m"] == []


def test_no_path_too_far_and_no_match_say_so():
    apart = [road(144.0, 144.1), road(144.3, 144.4)]                        # 18 km between them
    out = run(apart, [144.01, Y], [144.39, Y])
    assert out["status"] == "no_path" and out["start_offset_m"] == 0
    far = run([road(144.0, 144.1)], [144.05, Y], [146.0, Y])
    assert far["status"] == "too_far" and far["end_offset_m"] > 100_000 and far["nearest_names"][0] == "MAIN HIGHWAY"
    assert run([], [144.0, Y], [144.1, Y]) == {"status": "no_match"}


def test_names_used_are_reported_by_length():
    lines = [road(144.0, 144.3, name="CALDER FREEWAY"), road(144.3, 144.35, name="CALDER HIGHWAY")]
    out = run(lines, [144.0, Y], [144.35, Y])
    assert list(out["names"]) == ["CALDER FREEWAY", "CALDER HIGHWAY"]
    assert abs(sum(out["names"].values()) - out["length_m"]) <= 2


def test_long_lines_are_simplified_to_the_vertex_budget_and_say_by_how_much():
    wiggle = ([[144.0 + i / 10000, Y + (0.00005 if i % 2 else 0)] for i in range(3000)], "WINDING ROAD")
    out = run([wiggle], [144.0, Y], [144.2999, Y], max_vertices=100)
    assert out["status"] == "ok" and len(out["coords"]) <= 100 and out["simplified_m"] >= 5
    full = run([wiggle], [144.0, Y], [144.2999, Y], max_vertices=5000)
    assert full["simplified_m"] == 0 and len(full["coords"]) == 3000
    assert abs(out["length_m"] - full["length_m"]) < 1                       # length is measured before simplifying


def test_simplify_keeps_shape_within_tolerance():
    line = [[144.0, Y], [144.05, Y + 0.00001], [144.1, Y], [144.1, Y + 0.1]]
    assert T.simplify(line, 50) == [[144.0, Y], [144.1, Y], [144.1, Y + 0.1]]
    assert T.simplify(line, 0) == line
