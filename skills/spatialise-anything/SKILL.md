---
name: spatialise-anything
description: Turn the places in any input into map features. Use when the user wants to map, geocode, locate or spatialise what is in a document, table, text, photo set or email thread, or asks where the things in a file are. Outputs GeoJSON with confidence and provenance on every feature. Do not use just because a document mentions a place - the user has to want a map or features.
---

# Spatialise Anything

Give it anything, get back features you can defend. Every mention becomes one
GeoJSON feature in the same contract, with a confidence, a positional
uncertainty, its source span, and the candidates it was not.

Scripts are in `scripts/`, pure Python 3 standard library, no network.
Run them with `python3 scripts/<name>.py --help` for full options.

## Rules that never bend

1. **Nothing is dropped.** A mention you cannot resolve is still a feature:
   `status: unresolved`, `geometry: null`. A mention you cannot separate from
   its rivals is `ambiguous`, with the rivals as candidates. Never a best guess
   on the map.
2. **Confidence is not precision.** `confidence` = the lower of *is this a
   place* and *is it the right place*. `uncertainty_m` = how far off the
   geometry could be. A suburb centroid can be certain and 1.5 km loose.
3. **Keep the rejects.** Every candidate you did not pick goes in `candidates`
   with the reason (`rejected`).
4. **State the datum.** Coordinates with no stated datum are flagged
   `datum_assumed`, never silently treated as WGS84.
5. **Lowest tier wins.** A coordinate beats a place name in the same sentence.
6. **Ask once, only when it changes the map.** Batch ambiguous mentions into
   one question at the end. Never interrupt per mention.
7. **Publish only on explicit approval**, to a layer the user names.

## Pipeline

1. **Ingest.** Get text and structure, keeping page, row and character
   offsets (`source.char_start/char_end` are offsets into the text you pass a
   script). Photos: run `read_exif.py` before looking at the picture. Tables:
   run `sniff_table.py --rows` first.
2. **Detect.** Find every location mention and its span. Over-detect; later
   steps reject. `parse_coords.py` finds every coordinate format for you.
3. **Classify** each mention into a tier - see `references/tiers.md`:
   `explicit` (coordinates) < `structured` (address, postcode, LGA, asset ID)
   < `named` (place names) < `relative` ("2 km north of X", "along A between
   B and C") < `implicit` (an organisation or facility - **off unless the user
   turns it on**; record as unresolved with flag `tier4_disabled`).
4. **Frame.** Decide the document's country, state and region, and collect
   points already resolved. That frame is the prior for everything ambiguous.
5. **Resolve.** Ask for candidates, never one answer:
   - explicit: `parse_coords.py FILE --frame AU [--zone N] [--datum D]`
   - structured offline: `gazetteer.py postcode 3350 --geometry polygon`,
     `gazetteer.py lga "City of Ballarat" --geometry polygon`
   - named: `gazetteer.py search "Kensington" --state VIC --near LON,LAT ...`
     (or `gazetteer.py batch` for many). Offline covers localities, postcodes
     and LGAs only. Lakes, roads, rivers, facilities and addresses need the
     connector: `geocode`, `search_gazetteer`, `lookup_boundary`.
   - relative: resolve the anchors first, then
     `build_geometry.py offset|buffer|sector|line ...`; for "along R
     between X and Y" the connector's `trace_segment` (never a straight line)
6. **Disambiguate.** Rank by the frame, by distance to mentions already
   resolved, and by feature type. Resolved neighbours pull ambiguous ones.
   Details and worked cases: `references/disambiguation.md`.
7. **Choose geometry** from the words - `references/geometry.md`:
   "at" point; "along"/"between" line; "across"/"throughout" polygon;
   "near" point with wide uncertainty; "within N km of" buffer.
8. **Score.** Set confidence and uncertainty - `references/scoring.md`.
   Below the threshold (0.7, provisional) the status is `ambiguous`.
9. **Emit.** Write your decisions as compact JSON and let `assemble.py` build
   the features, merge script outputs, apply lowest-tier-wins and validate:
   ```
   python3 scripts/assemble.py --features coords.geojson photos.geojson \
       --decisions decisions.json -o spatialised.geojson
   python3 scripts/preview_map.py spatialised.geojson -o preview.html [--basemap none]
   ```
   The decision formats are in `assemble.py --help` and
   `references/output-contract.md`. If `assemble.py` reports errors, fix them -
   do not hand over an invalid file.
10. **Report** in a few lines: counts by status, then every ambiguous and
    unresolved mention with why, then the one batched question if an answer
    would change the map. Give the GeoJSON and the preview.

## With and without the connector

The skill works with no MCP server. Offline it covers tier 0, postcodes,
LGAs and localities (ABS ASGS boundaries, bundled), and relative geometry
built from those anchors. Everything else - street addresses, asset IDs,
roads, rivers, lakes, facilities, "along the X" lines - is reported as
`unresolved` with flag `needs_network` and method `not_attempted`, and you
say plainly which mentions you could not attempt. Where a coarse offline
answer is still useful (an address's locality), you may emit that instead
with flag `offline_only` and the locality's uncertainty - say so.

With the Spatialise connector attached (tools `list_resolvers`, `geocode`,
`search_gazetteer`, `lookup_boundary`, `trace_segment`, `reverse_geocode`,
`publish_features`), read `references/connector.md` before the first call.
In short: do the offline tiers first; call `list_resolvers` and use only what
it allows (the customer's own locator before any other, a public geocoder
only if enabled); every tool returns candidates and you choose; a batch
geocode and every publish return a dry run first - report the count,
destination and cost to the user and repeat the call with the
`confirm_token`. Pass chosen candidates to `assemble.py` as the tool returned
them (`"candidate": {...}`), never retyped.

## Sensitive material

The basemap in `preview_map.py` fetches public tiles, which reveals the area
viewed: use `--basemap none` when the user says the material is sensitive.
Place strings sent to a geocoder leave the sandbox; offline resolution does
not.

## References (load when needed)

- `references/output-contract.md` - every property, the rules, examples
- `references/tiers.md` - classifying mentions, edge cases
- `references/disambiguation.md` - framing, ranking, worked examples
- `references/geometry.md` - words to shapes, construction recipes
- `references/scoring.md` - confidence and uncertainty values
- `references/connector.md` - using the MCP server: order of work, confirmations, publishing
- `schema/feature.schema.json` - the contract as JSON Schema
