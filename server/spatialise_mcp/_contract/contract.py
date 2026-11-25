"""The Spatialise Anything output contract.

Every mention in every input becomes one GeoJSON Feature with the same
properties. This module is the single source of truth for the property
names, enums and defaults that the other scripts share. The JSON Schema in
``../schema/feature.schema.json`` mirrors it for tools that want a schema.

Pure standard library: the skill runs in Claude's sandbox, which has no
ArcGIS licence and may have no third-party packages.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

CONTRACT_VERSION = "0.1.0"

TIERS = ("explicit", "structured", "named", "relative", "implicit")
STATUSES = ("resolved", "ambiguous", "unresolved")

# Known methods. The contract allows others, but the validator warns on them
# so that new methods get added here deliberately.
METHODS = (
    "coord_parse",
    "exif",
    "locator",
    "gazetteer",
    "boundary_join",
    "asset_lookup",
    "constructed",
    "trace_segment",
    "inference",
    "not_attempted",
)

# Flags record anything a reviewer must know that the other properties do
# not say on their own. Keep the list short and add to it deliberately.
FLAGS = (
    "datum_assumed",          # no datum stated; source_crs is an assumption
    "datum_unsupported",      # datum stated but not converted (e.g. AGD66)
    "offline_only",           # a network resolver would have done better
    "needs_network",          # not attempted: requires a locator or service
    "tier4_inferred",         # location inferred from an organisation/event
    "tier4_disabled",         # implicit mention left unresolved by policy
    "precision_limited",      # source precision coarser than the geometry implies
    "default_uncertainty",    # uncertainty_m is a default, not measured
    "outside_frame",          # resolved outside the document's frame region
    "anchor_approximate",     # relative geometry built from an approximate anchor
)

# Provisional. The plan says set the threshold from the corpus, not by feel;
# this value is a starting point for the first evaluation run.
DEFAULT_THRESHOLD = 0.7

# Output coordinates are GeoJSON (RFC 7946): longitude, latitude, WGS84.
# GDA2020 and WGS84 (G2139) differ by well under a metre in 2026 and are
# treated as the same frame; GDA94 inputs are transformed to GDA2020.
OUTPUT_CRS = "EPSG:4326"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def make_source(doc: str, **where: Any) -> dict:
    """Build a source object. ``where`` may hold page, row, column, sheet,
    char_start, char_end. None values are dropped."""
    src = {"doc": doc}
    for k in ("page", "sheet", "row", "column", "char_start", "char_end"):
        v = where.get(k)
        if v is not None:
            src[k] = v
    return src


def make_feature(
    *,
    geometry: dict | None,
    label: str | None,
    mention: str,
    source: dict,
    tier: str,
    method: str,
    resolver: dict,
    confidence: float,
    uncertainty_m: float | None,
    status: str,
    candidates: Iterable[dict] = (),
    source_crs: str = OUTPUT_CRS,
    flags: Iterable[str] = (),
) -> dict:
    """Return one contract feature. Applies the contract's invariants so
    callers cannot build a feature that breaks them by accident:

    * a feature that is not ``resolved`` carries no geometry;
    * ``confidence`` is clamped to [0, 1];
    * an unresolved feature has no positional uncertainty.
    """
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    if status != "resolved":
        geometry = None
        uncertainty_m = None
    props = {
        "label": label,
        "mention": mention,
        "source": source,
        "tier": tier,
        "method": method,
        "resolver": resolver,
        "confidence": round(clamp01(confidence), 3),
        "uncertainty_m": None if uncertainty_m is None else round(float(uncertainty_m), 1),
        "status": status,
        "candidates": list(candidates),
        "source_crs": source_crs,
    }
    flags = sorted(set(flags))
    if flags:
        props["flags"] = flags
    return {"type": "Feature", "geometry": geometry, "properties": props}


def make_collection(features: list[dict], inputs: Iterable[str] = (), **meta: Any) -> dict:
    """Wrap features in a FeatureCollection with a ``spatialise`` member
    (a GeoJSON foreign member) that summarises the run."""
    counts = {s: 0 for s in STATUSES}
    for f in features:
        counts[f["properties"]["status"]] = counts.get(f["properties"]["status"], 0) + 1
    info = {
        "contract_version": CONTRACT_VERSION,
        "generated": utc_now(),
        "inputs": list(inputs),
        "counts": counts,
    }
    info.update({k: v for k, v in meta.items() if v is not None})
    return {"type": "FeatureCollection", "spatialise": info, "features": features}


def point(lon: float, lat: float, ndigits: int = 7) -> dict:
    return {"type": "Point", "coordinates": [round(lon, ndigits), round(lat, ndigits)]}


def dump(obj: Any, fp=None) -> str | None:
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    if fp is None:
        return text
    fp.write(text + "\n")
    return None


def resolver(name: str, version: str | None = None) -> dict:
    r = {"name": name}
    if version:
        r["version"] = version
    return r
