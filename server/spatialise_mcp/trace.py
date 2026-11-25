"""trace_segment: two anchors and a named road, river or asset -> the line between them.

"Along the Calder between Kyneton and Castlemaine" is a line, not a point,
and this is what draws it. It needs no network-analysis service: any polyline
layer with a name field will do, which is what makes it work on a customer's
own pipelines and channels as well as on roads.

    1. fetch every segment whose name matches, in a corridor around the anchors
    2. join segments that share an end point into a graph
    3. snap each anchor to the nearest point on any matched segment
    4. take the shortest path along the graph between the two snap points

It reports what it had to assume, because each assumption is a way to be
wrong: how far each anchor was from the line (a town centre is rarely on the
freeway), any gaps it bridged (a highway often changes name through a town),
and which names the path actually used.

Pure functions over coordinates; the tool in resolvers.py does the fetching.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Any

from .esri import haversine_m

Position = list[float]
GAP_PENALTY = 3.0  # a bridged gap costs three times its length, so real segments win when they exist
GAP_NAME = "(gap)"


@dataclass
class Edge:
    u: int
    v: int
    coords: list[Position]
    length_m: float
    name: str
    dead: bool = False  # replaced by its two halves after an anchor was snapped onto it


@dataclass
class Graph:
    nodes: list[Position] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    adj: dict[int, list[int]] = field(default_factory=dict)  # node -> edge indexes
    _index: dict[tuple[int, int], int] = field(default_factory=dict)

    def node(self, p: Position, snap_decimals: int = 5) -> int:
        """End points within about a metre are the same junction."""
        key = (round(p[0] * 10 ** snap_decimals), round(p[1] * 10 ** snap_decimals))
        if key not in self._index:
            self._index[key] = len(self.nodes)
            self.nodes.append([p[0], p[1]])
            self.adj[self._index[key]] = []
        return self._index[key]

    def new_node(self, p: Position) -> int:
        self.nodes.append([p[0], p[1]])
        self.adj[len(self.nodes) - 1] = []
        return len(self.nodes) - 1

    def add_edge(self, u: int, v: int, coords: list[Position], name: str, length_m: float | None = None) -> int:
        length = line_length_m(coords) if length_m is None else length_m
        self.edges.append(Edge(u, v, coords, length, name))
        i = len(self.edges) - 1
        self.adj[u].append(i)
        self.adj[v].append(i)
        return i


def line_length_m(coords: list[Position]) -> float:
    return sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(coords, coords[1:]))


def build_graph(lines: list[tuple[list[Position], str]]) -> Graph:
    g = Graph()
    for coords, name in lines:
        coords = [[float(p[0]), float(p[1])] for p in coords]
        if len(coords) < 2:
            continue
        u, v = g.node(coords[0]), g.node(coords[-1])
        if u == v and len(coords) == 2:
            continue
        g.add_edge(u, v, coords, name)
    return g


def components(g: Graph) -> list[int]:
    """Component id per node."""
    comp = [-1] * len(g.nodes)
    c = 0
    for start in range(len(g.nodes)):
        if comp[start] != -1:
            continue
        stack = [start]
        while stack:
            n = stack.pop()
            if comp[n] != -1:
                continue
            comp[n] = c
            for ei in g.adj[n]:
                e = g.edges[ei]
                stack.append(e.v if e.u == n else e.u)
        c += 1
    return comp


def bridge_gaps(g: Graph, tolerance_m: float) -> int:
    """Join components whose loose ends are within the tolerance. Returns how
    many bridges were added. Only dead ends are candidates: a gap in a named
    line is where the name stops, not somewhere along it."""
    if tolerance_m <= 0:
        return 0
    comp = components(g)
    ends = [n for n in range(len(g.nodes)) if len(g.adj[n]) == 1]
    added = 0
    best: dict[tuple[int, int], tuple[float, int, int]] = {}
    for i, a in enumerate(ends):
        pa = g.nodes[a]
        for b in ends[i + 1:]:
            if comp[a] == comp[b]:
                continue
            pb = g.nodes[b]
            if abs(pa[1] - pb[1]) * 111_000 > tolerance_m:
                continue
            d = haversine_m(pa[0], pa[1], pb[0], pb[1])
            if d <= tolerance_m:
                key = (min(comp[a], comp[b]), max(comp[a], comp[b]))
                if key not in best or d < best[key][0]:
                    best[key] = (d, a, b)
    for d, a, b in best.values():
        g.add_edge(a, b, [g.nodes[a], g.nodes[b]], GAP_NAME, length_m=d * GAP_PENALTY)
        added += 1
    return added


@dataclass
class Snap:
    edge: int
    index: int        # the snap point lies between coords[index] and coords[index + 1]
    point: Position
    distance_m: float


def snap(g: Graph, p: Position) -> Snap | None:
    """Nearest point on any real edge. Planar within a segment, which is
    exact enough over the length of one road segment."""
    kx = math.cos(math.radians(p[1]))
    best: Snap | None = None
    best_d2 = float("inf")
    for ei, e in enumerate(g.edges):
        if e.name == GAP_NAME or e.dead:
            continue
        c = e.coords
        for i in range(len(c) - 1):
            ax, ay = (c[i][0] - p[0]) * kx, c[i][1] - p[1]
            bx, by = (c[i + 1][0] - p[0]) * kx, c[i + 1][1] - p[1]
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
            qx, qy = ax + t * dx, ay + t * dy
            d2 = qx * qx + qy * qy
            if d2 < best_d2:
                best_d2 = d2
                point = [c[i][0] + t * (c[i + 1][0] - c[i][0]), c[i][1] + t * (c[i + 1][1] - c[i][1])]
                best = Snap(ei, i, point, 0.0)
    if best is not None:
        best.distance_m = haversine_m(p[0], p[1], best.point[0], best.point[1])
    return best


def insert(g: Graph, s: Snap) -> int:
    """Make the snap point a node: replace its edge with the two halves either
    side of it. Replacing matters when both anchors land on the same segment -
    the second then snaps onto a half, and the path runs directly between them
    instead of out to a junction and back."""
    e = g.edges[s.edge]
    e.dead = True
    g.adj[e.u].remove(s.edge)
    if e.v != e.u:
        g.adj[e.v].remove(s.edge)
    n = g.new_node(s.point)
    first = e.coords[: s.index + 1] + [s.point]
    second = [s.point] + e.coords[s.index + 1:]
    if len(first) >= 2 and line_length_m(first) > 0:
        g.add_edge(e.u, n, first, e.name)
    else:
        g.add_edge(e.u, n, [g.nodes[e.u], s.point], e.name, length_m=0.0)
    if len(second) >= 2 and line_length_m(second) > 0:
        g.add_edge(n, e.v, second, e.name)
    else:
        g.add_edge(n, e.v, [s.point, g.nodes[e.v]], e.name, length_m=0.0)
    return n


def shortest_path(g: Graph, source: int, target: int) -> list[tuple[int, int]] | None:
    """Dijkstra. Returns [(edge index, node arrived at)] from source to target."""
    dist = {source: 0.0}
    prev: dict[int, tuple[int, int]] = {}
    heap = [(0.0, source)]
    done: set[int] = set()
    while heap:
        d, n = heapq.heappop(heap)
        if n in done:
            continue
        if n == target:
            break
        done.add(n)
        for ei in g.adj[n]:
            e = g.edges[ei]
            m = e.v if e.u == n else e.u
            nd = d + e.length_m
            if nd < dist.get(m, float("inf")):
                dist[m] = nd
                prev[m] = (ei, n)
                heapq.heappush(heap, (nd, m))
    if target not in dist:
        return None
    steps = []
    n = target
    while n != source:
        ei, before = prev[n]
        steps.append((ei, n))
        n = before
    return steps[::-1]


def simplify(coords: list[Position], tolerance_m: float) -> list[Position]:
    """Douglas-Peucker, iterative, in metres."""
    if len(coords) < 3 or tolerance_m <= 0:
        return coords
    lat0 = coords[0][1]
    kx, ky = 111_320 * math.cos(math.radians(lat0)), 110_574
    pts = [(p[0] * kx, p[1] * ky) for p in coords]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        worst, worst_i = 0.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if seg2 == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
                d = math.hypot(px - ax - t * dx, py - ay - t * dy)
            if d > worst:
                worst, worst_i = d, i
        if worst > tolerance_m:
            keep[worst_i] = True
            stack += [(a, worst_i), (worst_i, b)]
    return [c for c, k in zip(coords, keep) if k]


def trace(lines: list[tuple[list[Position], str]], start: Position, end: Position, *, max_snap_m: float,
          gap_tolerance_m: float, max_vertices: int) -> dict[str, Any]:
    """Trace between two anchors along the given named lines.

    Returns {"status": "ok", "coords": [...], ...} or a status that says why
    there is no line: "no_match", "too_far" or "no_path".
    """
    g = build_graph(lines)
    if not g.edges:
        return {"status": "no_match"}
    bridges = bridge_gaps(g, gap_tolerance_m)
    s0, s1 = snap(g, start), snap(g, end)
    assert s0 is not None and s1 is not None
    offsets = {"start_offset_m": round(s0.distance_m), "end_offset_m": round(s1.distance_m)}
    if s0.distance_m > max_snap_m or s1.distance_m > max_snap_m:
        return {"status": "too_far", **offsets, "nearest_names": [g.edges[s0.edge].name, g.edges[s1.edge].name]}
    near = [g.edges[s0.edge].name, g.edges[s1.edge].name]
    a = insert(g, s0)
    s1 = snap(g, end)  # again: the first insert may have split the segment this one lands on
    assert s1 is not None
    b = insert(g, s1)
    steps = shortest_path(g, a, b)
    if steps is None:
        return {"status": "no_path", **offsets, "bridged_gaps": bridges, "nearest_names": near}
    coords: list[Position] = [list(g.nodes[a])]
    names: dict[str, float] = {}
    gaps: list[int] = []
    at = a
    for ei, arrived in steps:
        e = g.edges[ei]
        part = e.coords if e.u == at and e.v == arrived else e.coords[::-1]
        if e.u == e.v:  # a loop: direction does not matter
            part = e.coords
        for p in part[1:] if _same(part[0], coords[-1]) else part:
            if not _same(p, coords[-1]):
                coords.append([p[0], p[1]])
        if e.name == GAP_NAME:
            gaps.append(round(e.length_m / GAP_PENALTY))
        else:
            names[e.name] = names.get(e.name, 0.0) + e.length_m
        at = arrived
    length = line_length_m(coords)
    tolerance = 0.0
    out = coords
    if len(out) > max_vertices:
        tolerance = 5.0
        while True:
            out = simplify(coords, tolerance)
            if len(out) <= max_vertices or tolerance > 5000:
                break
            tolerance *= 2
    return {
        "status": "ok", "coords": [[round(p[0], 6), round(p[1], 6)] for p in out], "length_m": round(length),
        "names": {k: round(v) for k, v in sorted(names.items(), key=lambda kv: -kv[1])}, "gaps_m": gaps,
        "simplified_m": round(tolerance), **offsets,
        "straight_line_m": round(haversine_m(start[0], start[1], end[0], end[1])),
    }


def _same(a: Position, b: Position) -> bool:
    return abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9
