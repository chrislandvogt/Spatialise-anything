# Evaluation corpus

Synthetic inputs, each with the output a correct run should produce. Public
and synthetic so it can be shared; every document says "synthetic test
document" in its header.

```
corpus/
  manifest.json            items, tiers, frames, network-only mentions, review state, tolerance floors
  items/<id>/
    <input files>          .txt, .csv, .jpg
    expected.geojson       the expected features, in the output contract
```

Regenerate with `python tools/make_corpus.py --abs <dir of ABS shapefiles>`.

## Where the expected answers come from

Never from the skill's own scripts, so the corpus can catch them being wrong.

| Kind | Reference |
|---|---|
| Coordinate conversions | PROJ via pyproj, real datum transformations only (ballpark transforms are refused) |
| MGRS | the `mgrs` package |
| Localities, postcodes, LGAs | raw ABS ASGS Edition 3 shapefiles (centroids, equal-area radius) |
| Constructed geometry | `pyproj.Geod` |
| Landmarks, lake, river | OpenStreetMap, © OpenStreetMap contributors, ODbL |

## Items

| Item | Tier | Tests |
|---|---|---|
| t0-coords-mixed | explicit | 8 formats among phone numbers, part numbers, money, dates, ratios, measurements |
| t0-datums | explicit | AGD66 (unconverted, honest 250 m), GDA94 (transformed), WGS84, none (flagged) |
| t0-photos | explicit | EXIF with heading and error; GPS only; no GPS |
| t1-assets | structured | Lat/lon rows, MGA2020 rows (datum in header), a row with no location |
| t1-areas | structured | Postcode and LGA polygons, "City of ..." naming, a PO-box postcode with no area |
| t1-addresses | structured | Landmark addresses in prose - needs a locator |
| t2-melbourne | named | Richmond, Collingwood, Abbotsford, Cremorne - each exists in other states |
| t2-hawkesbury | named | Windsor and Richmond are neighbours in Melbourne *and* NSW; the river decides |
| t2-richmond-alone | named | No context: must be ambiguous |
| t2-bendigo | named | Suburbs (two exist elsewhere) and a lake the offline gazetteer lacks |
| t2-springfield | named | Five-state name settled by Ipswich; Springfield Lakes kept separate |
| t3-relative | relative | Offset, buffer, sector, and "along the Calder between ..." |
| t4-implicit | implicit | An organisation's facility: off by default |
| none-traps | none | People named Victoria and Sydney, versions, money, a coordinate-shaped budget line |

## Hand-check

Items marked `"review": "needs_hand_check"` in the manifest contain
judgement or approximate reference geometry: `t1-addresses`,
`t2-hawkesbury`, `t3-relative`. A person should open each `expected.geojson`
(the map preview helps: `preview_map.py expected.geojson`), confirm or
correct it, and set `"review": "checked <initials> <date>"`. Edits made by
hand should also go into `tools/make_corpus.py` so regeneration keeps them.

## Scoring rules

`eval/score.py` matches each expected mention to a run feature by source span
(or row, or document for photos), then judges it. Tolerance is the larger of
the expected `uncertainty_m` and the tier floor in the manifest. Mentions
listed in `network_mentions` pass an offline run if left unresolved or
ambiguous.
