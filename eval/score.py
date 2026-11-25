#!/usr/bin/env python3
"""Score Spatialise Anything output against the corpus.

    python eval/score.py --outputs runs/2026-11-25 [--offline] [--json]

--outputs holds one <item_id>.geojson per corpus item. --offline scores a
run made with no connector: mentions the manifest lists as needing the
network pass if they come back unresolved/ambiguous (or as a coarse
offline_only answer that is honest about its uncertainty).

Headline metric: the confident-wrong rate - features the run marked
resolved at or above the threshold whose position is further from the
reference than both the reference tolerance and the run's own stated
uncertainty. A confident answer that is wrong beyond its own error bar.

Outcome per expected mention:
  correct          right status, and within tolerance when placed
  imprecise        placed, outside tolerance but inside its own stated uncertainty (honest, loose)
  flagged          expected a placement, run said ambiguous - safe, but a miss
  not_attempted    offline run, network mention, correctly left unresolved
  unresolved_miss  expected a placement, run said unresolved
  confident_wrong  see above; also a resolved answer where ambiguous/unresolved was expected
  dropped          no feature for the mention at all (breaks "nothing is dropped")
Plus false_positive: run features matching no expected mention.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLAGS_THAT_MATTER = {"datum_assumed", "datum_unsupported", "tier4_disabled"}


# ---------------------------------------------------------------- geometry

def _m_per_deg(lat):
    return 111320.0 * math.cos(math.radians(lat)), 110574.0


def _xy(p, lat0):
    kx, ky = _m_per_deg(lat0)
    return p[0] * kx, p[1] * ky


def _seg_dist(p, a, b):
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if dx == dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def _ring_centroid(ring):
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
        c = x0 * y1 - x1 * y0
        a += c
        cx += (x0 + x1) * c
        cy += (y0 + y1) * c
    if a == 0:
        xs, ys = zip(*ring)
        return [sum(xs) / len(xs), sum(ys) / len(ys)]
    return [cx / (3 * a), cy / (3 * a)]


def family(g):
    if not g:
        return None
    t = g["type"].replace("Multi", "")
    return {"Point": "point", "LineString": "line", "Polygon": "polygon"}[t]


def rep_point(g):
    t = g["type"]
    c = g["coordinates"]
    if t == "Point":
        return c
    if t == "MultiPoint":
        return c[0]
    if t == "LineString":
        return c[len(c) // 2]
    if t == "MultiLineString":
        return c[0][len(c[0]) // 2]
    if t == "Polygon":
        return _ring_centroid(c[0])
    if t == "MultiPolygon":
        big = max(c, key=lambda p: len(p[0]))
        return _ring_centroid(big[0])
    raise ValueError(t)


def _lines(g):
    if g["type"] == "LineString":
        return [g["coordinates"]]
    if g["type"] == "MultiLineString":
        return g["coordinates"]
    return []


def distance_m(a, b):
    """Distance between two geometries for scoring: point/polygon by
    representative point; lines by the mean distance of each line's vertices
    to the other line (symmetric)."""
    lat0 = rep_point(a)[1]
    la, lb = _lines(a), _lines(b)
    if la and lb:
        def one_way(src, dst):
            ds = []
            for line in src:
                for p in line:
                    pp = _xy(p, lat0)
                    ds.append(min(_seg_dist(pp, _xy(s0, lat0), _xy(s1, lat0))
                                  for dl in dst for s0, s1 in zip(dl, dl[1:])))
            return sum(ds) / len(ds)
        return max(one_way(la, lb), one_way(lb, la))
    if la or lb:
        line, other = (la, b) if la else (lb, a)
        p = _xy(rep_point(other), lat0)
        return min(_seg_dist(p, _xy(s0, lat0), _xy(s1, lat0)) for ln in line for s0, s1 in zip(ln, ln[1:]))
    pa, pb = _xy(rep_point(a), lat0), _xy(rep_point(b), lat0)
    return math.hypot(pa[0] - pb[0], pa[1] - pb[1])


# ---------------------------------------------------------------- matching

def _overlap(a, b):
    sa, sb = a["properties"]["source"], b["properties"]["source"]
    if sa.get("doc") != sb.get("doc"):
        return 0
    if sa.get("row") is not None or sb.get("row") is not None:
        return 1 if sa.get("row") == sb.get("row") and sa.get("sheet") == sb.get("sheet") else 0
    if sa.get("page") is not None and sb.get("page") is not None and sa["page"] != sb["page"]:
        return 0
    if None not in (sa.get("char_start"), sa.get("char_end"), sb.get("char_start"), sb.get("char_end")):
        return max(0, min(sa["char_end"], sb["char_end"]) - max(sa["char_start"], sb["char_start"]))
    if a["properties"]["mention"].strip().lower() == b["properties"]["mention"].strip().lower():
        return 1
    if sa.get("char_start") is None and sb.get("char_start") is None:
        return 1  # whole-document sources, e.g. one photo per file
    return 0


def match(expected, actual):
    pairs, used = [], set()
    for e in expected:
        best, best_ov = None, 0
        for j, a in enumerate(actual):
            if j in used:
                continue
            ov = _overlap(e, a)
            if ov > best_ov:
                best, best_ov = j, ov
        if best is not None:
            used.add(best)
        pairs.append((e, actual[best] if best is not None else None))
    extras = [a for j, a in enumerate(actual) if j not in used]
    return pairs, extras


# ---------------------------------------------------------------- scoring

def judge(e, a, tol, threshold, network_offline):
    ep, es = e["properties"], e["properties"]["status"]
    if a is None:
        return "dropped", None
    ap, as_ = a["properties"], a["properties"]["status"]
    high = as_ == "resolved" and ap["confidence"] >= threshold
    dist = None
    if a.get("geometry") and e.get("geometry"):
        dist = distance_m(e["geometry"], a["geometry"])

    if network_offline:
        if as_ in ("unresolved", "ambiguous"):
            return "not_attempted", dist
        own = ap.get("uncertainty_m") or 0
        if dist is not None and dist <= tol:
            return "correct", dist
        if dist is not None and dist <= own and "offline_only" in ap.get("flags", []):
            return "not_attempted", dist
        return ("confident_wrong" if high else "imprecise"), dist

    if es == "resolved":
        if as_ == "resolved":
            own = ap.get("uncertainty_m") or 0
            if dist is None:
                return "confident_wrong", None
            if dist <= tol:
                return "correct", dist
            if dist <= own:
                return "imprecise", dist
            return ("confident_wrong" if high else "imprecise"), dist
        return ("flagged" if as_ == "ambiguous" else "unresolved_miss"), dist
    # expected ambiguous or unresolved
    if as_ in ("ambiguous", "unresolved"):
        return "correct", dist
    return ("confident_wrong" if high else "imprecise"), dist


def score(corpus: Path, outputs: Path, offline: bool, threshold: float | None, only: set | None = None):
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    if only:
        manifest["items"] = [i for i in manifest["items"] if i["id"] in only]
    threshold = threshold if threshold is not None else manifest["threshold"]
    floors = manifest["tolerance_floor_m"]
    per_tier = defaultdict(Counter)
    details = []
    high_conf = 0
    for item in manifest["items"]:
        exp = json.loads((corpus / "items" / item["id"] / "expected.geojson").read_text(encoding="utf-8"))["features"]
        out_path = outputs / f"{item['id']}.geojson"
        missing = not out_path.exists()
        act = [] if missing else json.loads(out_path.read_text(encoding="utf-8"))["features"]
        pairs, extras = match(exp, act)
        net = set(item.get("network_mentions", []))
        for e, a in pairs:
            ep = e["properties"]
            tier = ep["tier"]
            tol = max(ep.get("uncertainty_m") or 0, floors.get(tier, 50))
            network_offline = offline and ep["mention"] in net
            outcome, dist = judge(e, a, tol, threshold, network_offline)
            c = per_tier[tier]
            c["n"] += 1
            c[outcome] += 1
            if a is not None:
                ap = a["properties"]
                if ap["status"] == "resolved" and ap["confidence"] >= threshold:
                    high_conf += 1
                    c["high_conf"] += 1
                want = FLAGS_THAT_MATTER & set(ep.get("flags", []))
                if not network_offline and want - set(ap.get("flags", [])):
                    c["flag_miss"] += 1
                if e.get("geometry") and a.get("geometry") and not network_offline:
                    c["typed"] += 1
                    if family(e["geometry"]) == family(a["geometry"]):
                        c["type_correct"] += 1
            details.append({"item": item["id"], "mention": ep["mention"], "tier": tier, "outcome": outcome,
                            "distance_m": None if dist is None else round(dist, 1), "tolerance_m": round(tol, 1),
                            "missing_output": missing})
        for a in extras:
            tier = item["tier"] if item["tier"] != "none" else "none"
            per_tier[tier]["false_positive"] += 1
            ap = a["properties"]
            if ap["status"] == "resolved" and ap["confidence"] >= threshold:
                high_conf += 1
                per_tier[tier]["high_conf"] += 1
                per_tier[tier]["confident_wrong"] += 1
            details.append({"item": item["id"], "mention": ap["mention"], "tier": tier, "outcome": "false_positive"})
    total = Counter()
    for c in per_tier.values():
        total.update(c)
    cw_rate = total["confident_wrong"] / high_conf if high_conf else 0.0
    bars = check_bars(per_tier, {i["tier"] for i in manifest["items"]})
    return {"threshold": threshold, "offline": offline, "per_tier": {k: dict(v) for k, v in per_tier.items()},
            "total": dict(total), "confident_wrong_rate": round(cw_rate, 4), "bars": bars, "details": details}


def check_bars(per_tier, scored_tiers):
    """The plan's proposed bars, for the tiers that were scored. Proposals to
    calibrate on the first run, not commitments."""
    bars = {"every mention accounted for": sum(c["dropped"] for c in per_tier.values()) == 0}
    if "explicit" in per_tier:
        t0 = per_tier["explicit"]
        bars["explicit: no misses, no false positives"] = (
            t0["correct"] == t0["n"] and t0["false_positive"] == 0 and t0["flag_miss"] == 0)
    if "none" in scored_tiers:
        bars["no-places document: no features"] = per_tier.get("none", Counter())["false_positive"] == 0
    if "named" in per_tier:
        bars["named: confident-wrong zero"] = per_tier["named"]["confident_wrong"] == 0
    rel = per_tier.get("relative", Counter())
    if rel["typed"]:
        bars["relative: geometry type correct on most"] = rel["type_correct"] / rel["typed"] >= 0.75
    return bars


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", type=Path, default=ROOT / "corpus")
    ap.add_argument("--outputs", type=Path, required=True)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--threshold", type=float)
    ap.add_argument("--only", nargs="*", help="score only these item ids")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = score(a.corpus, a.outputs, a.offline, a.threshold, set(a.only) if a.only else None)
    if a.json:
        print(json.dumps(res, indent=2))
        return 0
    cols = ["n", "correct", "imprecise", "flagged", "not_attempted", "unresolved_miss", "confident_wrong",
            "dropped", "false_positive", "flag_miss"]
    print(f"threshold {res['threshold']}  mode {'offline' if res['offline'] else 'online'}")
    print("tier".ljust(11) + "".join(c[:9].rjust(10) for c in cols))
    for tier in ["explicit", "structured", "named", "relative", "implicit", "none"]:
        if tier in res["per_tier"]:
            c = res["per_tier"][tier]
            print(tier.ljust(11) + "".join(str(c.get(k, 0)).rjust(10) for k in cols))
    print(f"\nconfident-wrong rate: {res['confident_wrong_rate']:.2%} "
          f"({res['total'].get('confident_wrong', 0)} of {sum(c.get('high_conf', 0) for c in res['per_tier'].values())} "
          f"high-confidence features)")
    for k, v in res["bars"].items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    bad = [d for d in res["details"] if d["outcome"] not in ("correct", "not_attempted")]
    if bad:
        print("\nto look at:")
        for d in bad:
            dist = f"  {d['distance_m']} m (tol {d['tolerance_m']})" if d.get("distance_m") is not None else ""
            print(f"  {d['item']:<20} {d['outcome']:<16} {d['mention'][:60]!r}{dist}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
