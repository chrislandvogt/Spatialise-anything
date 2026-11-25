# Using the Spatialise connector (MCP server)

The connector resolves what the offline gazetteer cannot: street addresses,
lakes, rivers, roads, facilities, SA2s and the organisation's own zones. It
also publishes to hosted feature layers. It returns candidates; you decide.

Its tools appear as `list_resolvers`, `geocode`, `reverse_geocode`,
`search_gazetteer`, `lookup_boundary`, `trace_segment` and
`publish_features`. If they are not
in your tool list, the connector is not connected: work offline and say which
mentions you could not attempt.

## Order of work

1. **Offline first.** Coordinates, EXIF, postcodes, LGAs and localities need
   no network. Do them with the scripts, and keep the resolved points as
   hints for step 3.
2. **`list_resolvers`** once per run. It tells you which locators,
   gazetteers, boundary sets and line networks exist, which are blocked and why, and whether
   publishing is available. Use only what it lists. A blocked resolver is
   blocked: do not look for a way around it.
3. **Resolve what is left**, passing the frame as hints:
   - addresses -> `geocode(queries=[...], near=[lon, lat])`
   - named features -> `search_gazetteer(names=[...], near=[lon, lat])`
   - codes and zone names -> `lookup_boundary(boundary=..., codes=[...])`
   - "along R between X and Y" -> resolve X and Y, then
     `trace_segment(name=R, start=..., end=...)` (see `geometry.md`)
   - a label for a bare coordinate or photo -> `reverse_geocode`
4. **Decide**, then hand the chosen candidates to `assemble.py` (below).
5. **Publish** only if the user asked, to the target they named.

## Nothing is sent silently

A `geocode` or `reverse_geocode` call with more queries than the server's
`confirm_batches_over` (from `list_resolvers`), and every
`publish_features` call, first returns `status: confirmation_required`
with a `confirm_token` and has done nothing.

Tell the user what it reports - how many place strings, to which host,
stored or not, estimated credits; or for a publish, the target, portal,
account and row counts - and wait for their answer. Then repeat the call with
**exactly the same arguments** plus `confirm_token`. Changing any argument
invalidates the token; that is deliberate.

Put all the addresses for a document in one `geocode` call, so the user is
asked once.

## Choosing between candidates

Every candidate has `label`, `kind`, `point`, `uncertainty_m` with
`uncertainty_basis`, `resolver` and `method`. Locator candidates add
`score` (0-1); gazetteer and boundary candidates add `id`; any call with
`near` adds `distance_km`.

- **Several candidates with close scores, and nothing in the text to separate
  them: ambiguous.** Do not take the first.
- **One name, several kinds of thing.** A gazetteer returns the locality, the
  railway station and the post office of the same name. The sentence decides:
  "sampling at Lake X" is the lake, "residents of Lake X" is the locality.
  Reject the others with that reason.
- **Distance is evidence, not proof.** A candidate 3 km from the document's
  other places beats one 300 km away, but say so in the reason.
- **Score is the locator's opinion of the text match**, not of the place. A
  0.98 match to the wrong town is still the wrong town.
- **No candidates: unresolved.** Do not fall back to memory. If another
  resolver is allowed (the result says), you may try it; otherwise the mention
  is unresolved with flag `needs_network` only when no resolver was tried,
  and no flag when one was tried and found nothing.

## Handing candidates to assemble.py

Copy the candidate object **as the tool returned it** into the decision. Do
not retype coordinates or rebuild it: `assemble.py` takes the geometry, label,
uncertainty, resolver and method from it unchanged.

```json
[
  {"mention": "45 Lydiard St North, Ballarat", "source": {"doc": "notice.txt", "char_start": 50, "char_end": 79},
   "tier": "structured", "confidence": 0.9,
   "candidate": {"label": "45 Lydiard Street North, Ballarat Central, Victoria, 3350", "kind": "PointAddress",
                 "point": [143.8589, -37.5601], "score": 0.98, "uncertainty_m": 15,
                 "uncertainty_basis": "typical for a PointAddress match", "method": "locator",
                 "resolver": {"name": "agency-addresses", "version": "G-NAF 2026-11"}},
   "others": [{"candidate": {"label": "45 Lydiard Street, Soldiers Hill, Victoria, 3350", "kind": "PointAddress",
                             "point": [143.86, -37.54], "score": 0.91},
               "why": "the notice says Ballarat Central"}]},

  {"mention": "1 Main Street", "source": {"doc": "note.txt", "char_start": 11, "char_end": 24},
   "tier": "structured", "status": "ambiguous", "confidence": 0.5,
   "others": [{"candidate": {"...": "first candidate"}, "why": "same score; the note names no town"},
              {"candidate": {"...": "second candidate"}, "why": "same score; the note names no town"}]}
]
```

- `"geometry": "point"` in the decision places an area candidate (an SA2, a
  lake polygon) at its point; the uncertainty becomes the area's radius.
  Without it, a candidate that carries a `geometry` is placed as that shape.
- A candidate whose `uncertainty_basis` mentions `default_uncertainty`
  gets that flag automatically: the gazetteer is a point layer and cannot say
  how big the place is.
- Confidence is still yours to set: `min(is_place, right_place)`. For a
  locator, start `right_place` from the score, then lower it if the town or
  feature type is in doubt.

## Geometry size

Ask for what the words need. `lookup_boundary` defaults to a simplified
polygon of at most 300 vertices, which is right for display. `geometry:
"point"` is enough when the mention only needs placing. Raise
`max_vertices` only when the user needs the detail: every vertex passes
through this conversation.

## Publishing

Only when the user asks, and only to the target they name - a title for a new
layer (`create`) or the FeatureServer URL of a layer this tool made earlier
(`url`). Never pick a name or a layer for them.

1. Pass the whole FeatureCollection from the `.geojson` file as `features`.
2. Show the user the plan the dry run returns.
3. On their approval, repeat the call with the token.

A published service has layers `points`, `lines`, `polygons` and a table
`unplaced` holding every ambiguous and unresolved mention. New layers are
private to the user. Report the `item_url`, the rows written per layer, and
any `partial` status in full.

## Sensitive material

`list_resolvers` shows each resolver's host. Place strings go to that host
and nowhere else. If the user has said the material is sensitive, tell them
which host a call would send text to before you call it - even for a single
query the server would not ask about.

## Errors

Tool errors say what to do. Common ones:

- *needs your ArcGIS sign-in / sign-in has expired* - ask the user to
  reconnect the Spatialise connector; meanwhile those mentions are unresolved
  with `needs_network`.
- *disabled by server policy* - the operator blocked that resolver. Not a
  problem to solve; report it.
- *field ... is not in the layer*, *not a queryable layer* - the server's
  configuration is wrong. Tell the user so they can tell the operator.
