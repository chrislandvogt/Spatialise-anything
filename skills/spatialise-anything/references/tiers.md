# Input tiers

Sort mentions by how the location is expressed, not by file type. A
spreadsheet and an email can both hold a tier 1 address.

| Tier | Examples | Resolved by | Typical geometry | Offline? |
|---|---|---|---|---|
| explicit | lat/long, DMS, MGA grid refs, MGRS, EXIF GPS, WKT | `parse_coords.py`, `read_exif.py` | as given | yes |
| structured | street address, postcode, lot/plan, SA2/LGA code, asset ID | locator, `gazetteer.py` boundary, customer asset layer | point or polygon | postcodes and LGAs yes; addresses and assets no |
| named | "Kensington", "Lake Wendouree", "the Western Freeway" | `gazetteer.py`, ranked by the frame | whatever the place is | localities yes; everything else no |
| relative | "200 m north of the bridge", "along the Western Highway between Ballan and Ballarat" | anchors first, then `build_geometry.py` or `trace_segment` | line, buffer, sector | when the anchors are offline |
| implicit | an organisation, facility or event | lookup, then inference | point, wide uncertainty | no - and off by default |

## Classification rules

- **Lowest tier wins.** "the pump at -37.5308, 143.8284 near Wendouree" is one
  explicit mention; Wendouree is context, not a second feature. If you do emit
  both with overlapping spans, `assemble.py` drops the higher tier.
  Non-overlapping mentions of the same place are separate features.
- **A mention is a span of text**, not a place. "Wendouree" twice in a document
  is two mentions and two features (resolved the same way).
- **Postcodes inside an address** belong to the address mention. A postcode
  standing alone ("properties in 3350") is a structured mention of its own.
- **Relative beats named** when the words carry an offset or extent:
  "4 km west of Wendouree" is one relative mention whose anchor is
  Wendouree - do not also emit Wendouree as a named feature for that span.
- **Implicit is opt-in.** "the shire's main works depot"
  is unresolved with `tier4_disabled` unless the user has asked for inference.
  When on: `tier4_inferred`, method `inference`, wide uncertainty, and never
  above 0.75 confidence.
- **Not a place**: people's names ("Adelaide Chen", "Darwin Lee"),
  products and ships named after places, organisations used as entities
  ("the Ballarat team approved the loan") unless they stand for a site. These are
  not mentions at all - they produce no feature. Being sure something is not
  a place is part of `is_place`; when unsure, emit it and let a low
  confidence make it ambiguous.
- **Document metadata** (letterhead addresses, sign-off locations, phone
  area codes) is not a mention unless the user wants it mapped. Say what you
  skipped.

## Edge cases

- Grid refs with no zone: unresolved unless the document states a zone once
  (pass `--zone`). Ask in the batched question.
- Bare decimal pairs with fewer than 3 decimals are ignored unless a word
  like "lat", "GPS" or "coordinates" is nearby. Values outside the frame are
  kept but capped at confidence 0.5 (ambiguous).
- A photo with no GPS is one unresolved feature (`photo <file>`), so the
  photo set always comes back complete.
- Tables: one mention per row (`sniff_table.py --rows`); `source.row` is the
  1-based spreadsheet row (header is row 1).
