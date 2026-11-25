# Scoring

`confidence = min(is_place, right_place)`. `uncertainty_m` is separate: how
far off the geometry could be if it is the right place.

The threshold between `resolved` and `ambiguous` is **0.7, provisional** -
the plan says set it from the corpus, not by feel. When the evaluation sets a
new value, change `contract.DEFAULT_THRESHOLD` and say so in the output.

## is_place - is this text a location at all?

| Situation | is_place |
|---|---|
| Coordinates with hemisphere letters, DMS marks, labels or grid letters | 0.95-0.98 (set by `parse_coords.py`) |
| Bare decimal pair, >= 4 decimals | 0.90 (+0.05 with a word like "GPS" nearby) |
| Bare decimal pair, 3 decimals | 0.85 |
| Place name with a locative ("at", "in", "near", "north of") | 0.95 |
| Place name as an adjective or organisation ("the Ballarat team") | 0.6-0.8, judge from use |
| Could be a person's name ("Adelaide", "Darwin") | 0.3-0.6 unless the sentence makes it a place |

## right_place - is it the right one?

| Situation | right_place |
|---|---|
| Parsed coordinates | 0.99 (0.6 if lat/lon order is undeterminable) |
| Postcode or LGA code | 0.98 |
| Gazetteer pick | start from `right_place_hint`, adjust for context |
| Relative construction | the anchor's right_place, minus 0.05-0.15 for loose wording |
| Inferred (tier 4) | at most 0.75 |
| Outside the document frame | capped at 0.5 |

## uncertainty_m

| Source | Value |
|---|---|
| Decimal degrees | half the last digit: 4 dp = 5.6 m, 3 dp = 56 m, 2 dp = 557 m |
| DMS | half the last unit: whole seconds = 15 m, 0.1" = 1.5 m, whole minutes = 927 m |
| Grid (whole metres) | 1 m |
| MGRS | half-diagonal of the cell: 10-digit 0.7 m, 8-digit 7 m, 6-digit 71 m |
| Datum assumed | +2 m (WGS84 vs GDA94 is ~1.8 m in Australia) |
| AGD66/AGD84 unconverted | +250 m |
| Photo GPS | `GPSHPositioningError` if present, else 10 m (`default_uncertainty`) |
| Locality / postcode / LGA as a point | `radius_m` - radius of a circle of the same area |
| Same as a polygon | 0 - the polygon is the answer (simplified to ~200 m) |
| Constructed | from `build_geometry.py`, plus the anchor's uncertainty |

Never shrink uncertainty to look precise. A big honest number is the point.
