# Output contract (v0.1.0)

Every mention in every input becomes one GeoJSON Feature with these
properties. The schema is closed: no other properties. `validate_features.py`
enforces it.

| Property | Type | Meaning |
|---|---|---|
| geometry | Point, LineString, Polygon (or Multi*), or null | Null unless `status` is `resolved` |
| label | string or null | The resolved name, as the resolver returned it |
| mention | string | The exact words from the source |
| source | object | `doc` plus any of `page`, `sheet`, `row`, `column`, `char_start`, `char_end` |
| tier | enum | `explicit`, `structured`, `named`, `relative`, `implicit` |
| method | string | `coord_parse`, `exif`, `locator`, `gazetteer`, `boundary_join`, `asset_lookup`, `constructed`, `trace_segment`, `inference`, `not_attempted` |
| resolver | object | `name` and `version` of the locator, gazetteer or dataset used |
| confidence | number 0-1 | The lower of: is this a place, and is it the right place |
| uncertainty_m | number or null | Positional uncertainty in metres, separate from confidence |
| status | enum | `resolved`, `ambiguous`, `unresolved` |
| candidates | array | Alternatives considered: `label`, `rejected` (why), optional `confidence`, `geometry`, `id` |
| source_crs | string | The datum the input stated, e.g. `EPSG:7844`; `... (assumed)` when assumed |
| flags | array (optional) | See below |

The FeatureCollection carries a `spatialise` member: `contract_version`,
`generated`, `inputs`, `counts`, `threshold`, optional `frame`.

## Cross-field rules

- `resolved` has geometry and `uncertainty_m`, and confidence >= threshold.
- `ambiguous` has null geometry, null uncertainty, confidence < threshold, and
  at least one candidate (with geometry where there is one, so a reviewer can
  pick).
- `unresolved` has null geometry and null uncertainty.
- Output coordinates are lon, lat in EPSG:4326. GDA2020 is treated as equal to
  WGS84 (they differ by well under a metre in 2026); GDA94 input is
  transformed to GDA2020; AGD66/AGD84 is not converted and is flagged.

## Flags

| Flag | When |
|---|---|
| `datum_assumed` | No datum stated; `source_crs` is an assumption |
| `datum_unsupported` | Datum stated but not converted (AGD66, AGD84, NAD...); +250 m uncertainty |
| `offline_only` | A network resolver would have done better; this is the offline answer |
| `needs_network` | Not attempted: needs a locator or a feature service |
| `tier4_inferred` | Location inferred from an organisation, facility or event |
| `tier4_disabled` | Implicit mention left unresolved by policy |
| `precision_limited` | Source precision coarser than 50 m |
| `default_uncertainty` | `uncertainty_m` is a default (e.g. photo GPS with no error recorded) |
| `outside_frame` | Resolved outside the document's frame; confidence capped at 0.5 |
| `anchor_approximate` | Relative geometry built from an anchor with wide uncertainty |

## Example

```json
{
  "type": "Feature",
  "geometry": { "type": "Point", "coordinates": [144.927033, -37.794302] },
  "properties": {
    "label": "Kensington, VIC",
    "mention": "the Kensington depot",
    "source": { "doc": "ops-report.pdf", "page": 4, "char_start": 1182, "char_end": 1200 },
    "tier": "named",
    "method": "gazetteer",
    "resolver": { "name": "bundled-gazetteer", "version": "2026-11" },
    "confidence": 0.82,
    "uncertainty_m": 825,
    "status": "resolved",
    "candidates": [
      { "label": "Kensington, NSW", "rejected": "document context is metropolitan Melbourne" }
    ],
    "source_crs": "EPSG:7844"
  }
}
```

## Decisions for assemble.py

Write one object per mention the scripts did not already turn into features.
`assemble.py --help` has the full forms; the common ones:

```json
[
  {"mention": "the Kensington depot", "source": {"doc": "ops.txt", "char_start": 1182, "char_end": 1202},
   "tier": "named", "pick": "locality:21327", "confidence": 0.82,
   "rejects": {"locality:12107": "document context is metropolitan Melbourne"}},

  {"mention": "postcode 3350", "source": {"doc": "ops.txt", "char_start": 40, "char_end": 53},
   "tier": "structured", "pick": "postcode:3350", "geometry": "polygon", "confidence": 0.95},

  {"mention": "Kensington", "source": {"doc": "memo.txt", "char_start": 5, "char_end": 15},
   "tier": "named", "status": "ambiguous", "confidence": 0.2,
   "candidates": {"locality:21327": "no state or nearby place in the memo",
                  "locality:12107": "no state or nearby place in the memo"}},

  {"mention": "about 4 km west of Wendouree", "source": {"doc": "note.txt", "char_start": 0, "char_end": 28},
   "tier": "relative", "constructed": {"geometry": {"type": "Point", "coordinates": [143.783, -37.531]},
   "uncertainty_m": 1838, "method": "constructed"}, "anchors": ["locality:22745"],
   "label": "~4 km W of Wendouree, VIC", "confidence": 0.75},

  {"mention": "45 Lydiard St North, Ballarat", "source": {"doc": "note.txt", "char_start": 50, "char_end": 79},
   "tier": "structured", "status": "unresolved", "method": "not_attempted", "flags": ["needs_network"]}
]
```

Gazetteer ids are `kind:code` as `gazetteer.py` returns them in `id`; the
ids above are illustrative - always use the ids a search returned.
