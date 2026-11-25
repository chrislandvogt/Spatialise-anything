# Spatialise Anything

Give it anything, get back features you can defend.

A Claude plugin that turns the places in any input - documents, tables, free
text, photo sets, email threads - into map-ready GeoJSON. Every mention comes
back as one feature in the same contract, with a confidence score, a
positional uncertainty in metres, the exact source span it came from, and the
candidates it was not.

The name borrows from Segment Anything and Depth Anything: one output
contract, many input adapters.

> **Status: v0.1.0 - phases 1 and 2 of 5.** The output contract, the
> evaluation corpus and the offline skill are built and tested. The MCP server
> (locators, feature services, publishing) is next. See [Roadmap](#roadmap).

## What it does

| You give it | You get |
|---|---|
| `-37.24712, 144.45291 (GDA2020)`, DMS, MGA grid refs, MGRS, WKT | Points, with the datum stated or flagged as assumed; GDA94 transformed; AGD66 flagged with a 250 m uncertainty |
| A photo set | One point per photo from EXIF, heading kept; photos with no GPS reported, not dropped |
| A spreadsheet | Location columns found by header and value pattern; one feature per row |
| Postcodes, LGA names | ABS boundary polygons, offline |
| "Richmond" in an inner-Melbourne report | Richmond, VIC - with Richmond NSW, QLD, SA and TAS kept as rejected candidates and why |
| "Richmond" on its own | `ambiguous`, null geometry, six candidates, and one question at the end |
| "about 5 km south-east of Castlemaine", "within 2 km of Kyneton", "north of Bendigo" | A point with honest uncertainty, a buffer, a sector |

Three rules hold throughout:

- **Nothing is dropped.** A mention it cannot resolve is still a feature, with
  null geometry and a status.
- **Confidence is not precision.** A suburb centroid can be the right place
  and still be 1.5 km loose; both numbers are reported.
- **Nothing is geocoded silently.** Offline by default; network resolvers only
  when connected and allowed, with the cost reported first.

## The output contract

```json
{
  "type": "Feature",
  "geometry": { "type": "Point", "coordinates": [145.002441, -37.820294] },
  "properties": {
    "label": "Richmond, VIC",
    "mention": "the Richmond depot",
    "source": { "doc": "ops-report.pdf", "page": 4, "char_start": 1182, "char_end": 1200 },
    "tier": "named",
    "method": "gazetteer",
    "resolver": { "name": "bundled-gazetteer", "version": "2026-11" },
    "confidence": 0.82,
    "uncertainty_m": 1188,
    "status": "resolved",
    "candidates": [{ "label": "Richmond, NSW", "rejected": "document context is metropolitan Melbourne" }],
    "source_crs": "EPSG:7844"
  }
}
```

Full definition: [`output-contract.md`](skills/spatialise-anything/references/output-contract.md)
and the JSON Schema [`feature.schema.json`](skills/spatialise-anything/schema/feature.schema.json).

## Install

**Claude Code** - this repository is also a plugin marketplace:

```
/plugin marketplace add chrislandvogt/spatialise-anything
/plugin install spatialise@spatialise-anything
```

Then `/spatialise:anything path/to/report.pdf`, or just ask Claude to map
the places in a file.

**claude.ai / Cowork** - build the zip and upload it:

```
python tools/package.py      # -> dist/spatialise-plugin-0.1.0.zip
```

The skill alone (`dist/spatialise-anything-skill-0.1.0.zip`) can also be
uploaded as a standalone skill.

## How it works

```
input --> skill (detect, tier, frame, disambiguate, choose geometry, score)
            |                                   |
            v                                   v
   offline scripts + bundled gazetteer    MCP server (phase 3: locators,
   (pure Python, no network)              feature services, publishing)
            \___________________  ____________/
                                \/
                        one output contract
```

The skill holds the judgement; scripts do the deterministic work and the
bookkeeping. All scripts are standard-library Python 3 so they run in
Claude's sandbox with no installs:

| Script | Job |
|---|---|
| `parse_coords.py` | Decimal degrees, DMS, MGA/UTM, MGRS, WKT -> features, with span, datum and uncertainty; ignores phone numbers, part numbers, money, ratios |
| `read_exif.py` | GPS position, heading, error and time from photos |
| `sniff_table.py` | Finds coordinate, address, postcode, LGA and asset columns; one mention per row |
| `gazetteer.py` | Offline localities, postcodes and LGAs; candidates ranked by state and by distance to places already resolved |
| `build_geometry.py` | Geodesic offsets, buffers, sectors and lines from an anchor, bearing and distance |
| `assemble.py` | Expands the skill's decisions into features, merges, applies lowest-tier-wins, validates |
| `validate_features.py` | Fails the run if any feature breaks the contract |
| `preview_map.py` | Self-contained HTML map, with a table of everything ambiguous or unresolved |

Geodesy (Krüger-series Transverse Mercator, MGRS, the GDA94 -> GDA2020
Helmert, Vincenty) is implemented from the published formulas and tested
against PROJ to the nanometre.

## Evaluation

The headline metric is the **confident-wrong rate**: features marked
resolved at or above the threshold that sit further from the reference than
both the reference tolerance and their own stated uncertainty.

`corpus/` holds 14 synthetic items (51 expected mentions) across all tiers,
plus a no-places document full of traps. Every expected position comes from
an independent reference - PROJ, the `mgrs` package, raw ABS shapefiles,
OpenStreetMap - never from the skill's own code. See [`corpus/README.md`](corpus/README.md).

```
python eval/run_scripted.py --out runs/scripted          # scripts only, no model
python eval/score.py --outputs runs/scripted --offline \
    --only t0-coords-mixed t0-datums t0-photos t1-assets none-traps
```

Current scripts-only result on the tier 0 items: 20/20 mentions correct, 0
false positives, 0 confident-wrong. Scoring a full Claude run, and comparing
against Claude without the skill, is described in [`eval/README.md`](eval/README.md).

## Development

```
pip install -r requirements-dev.txt
python -m pytest
```

Rebuild the gazetteer from ABS downloads with `tools/build_gazetteer.py`;
regenerate the corpus with `tools/make_corpus.py`.

## Roadmap

| Phase | Ships | Exit test | Status |
|---|---|---|---|
| 1 Contract and corpus | Feature schema, hand-checkable corpus | Schema validates every expected output | Done (3 items await a human hand-check) |
| 2 Skill v0, offline | Tier 0, offline postcodes, localities, LGAs, GeoJSON, preview | Meets the tier 0 bar in Claude with no connector | Scripts meet it; a recorded Claude run is next |
| 3 MCP server | `list_resolvers`, `geocode`, `search_gazetteer`, `lookup_boundary`, `publish_features` on ArcGIS Online then Enterprise | Addresses and ambiguous names land in a hosted feature layer via a nominated locator | Not started |
| 4 Relative tier and plugin | `trace_segment`, constructed geometry, packaged plugin | "Along the Calder between Kyneton and Castlemaine" returns a line on the freeway; zip installs on claude.ai | Constructed geometry and packaging done; `trace_segment` not started |
| 5 Customer-ready | Customer-locator-only config, evaluation report, demo script | End-to-end against an Enterprise with the public geocoder disabled | Not started |

## Limitations

- Offline coverage is Australian localities, postal areas and LGAs only.
  Street addresses, roads, rivers, lakes, facilities and asset IDs need the
  MCP server and come back `unresolved` with `needs_network` until then.
- A conversational tool, not a bulk pipeline.
- The confidence threshold (0.7) is provisional until set from a full
  evaluation run.

## Data and licences

Code: Apache 2.0.

The bundled gazetteer is derived from the Australian Bureau of Statistics,
Australian Statistical Geography Standard Edition 3 (SAL 2021, POA 2021,
LGA 2025), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/);
simplified, and not endorsed by the ABS. Details in
[`ATTRIBUTION.md`](skills/spatialise-anything/data/ATTRIBUTION.md).

A few corpus reference positions are from OpenStreetMap,
© OpenStreetMap contributors, [ODbL](https://opendatacommons.org/licenses/odbl/).
