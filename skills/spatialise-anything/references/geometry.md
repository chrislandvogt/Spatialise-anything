# Choosing and building geometry

Follow the words, not the resolver's default.

| Words | Geometry | Build with |
|---|---|---|
| "at X", "in X" (small place), a coordinate | Point | resolver point |
| "in X" where X is a postcode, LGA or region | Polygon | `gazetteer.py ... --geometry polygon`, `"geometry": "polygon"` in the decision |
| "across X", "throughout X", "all of X" | Polygon | as above |
| "near X", "outside X", "on the edge of X" | Point at X, uncertainty widened | anchor point; uncertainty = anchor radius + 2 km (town) or + 5 km (region) |
| "N km DIR of X" | Point | `build_geometry.py offset --from X --bearing DIR --distance "N km"` |
| "DIR of X" (no distance) | Sector | `build_geometry.py sector --from X --bearing DIR --max "<sensible reach>"` |
| "within N km of X", "N km radius" | Polygon | `build_geometry.py buffer --from X --radius "N km"` |
| "along R between X and Y" | LineString on R | Resolve X and Y, then the connector's `trace_segment`. Offline: unresolved, `needs_network` |
| "between X and Y" (no route named) | LineString | `build_geometry.py line --points X Y` - say it is straight |

## Uncertainty from the construction

`build_geometry.py` returns `uncertainty_m` from the words:

- distance taken as approximate (+/-25%) unless `--exact` (surveyed, +/-5%)
- compass bearings carry their own sector: "north" +/-22.5 deg, "NNE"
  +/-11.25 deg, a numeric bearing +/-2 deg
- add the anchor's uncertainty: pass `--anchor-uncertainty` (a locality's
  `radius_m`) or list the anchor in the decision's `anchors` and
  `assemble.py` adds it

## Sector reach

"West of Ballan" with no distance needs a reach. Use the scale of the
document: about 10 km around a town, 30 km around a regional centre, the
extent of the frame otherwise. State the reach you used in the label
("west of Ballan, within ~10 km").

## Lines along a named feature

With the connector attached and a network listed by `list_resolvers`:

1. Resolve the two anchors first, as their own lookups (usually towns: the
   offline gazetteer has them). Do not emit them as extra features unless the
   text also mentions them in their own right.
2. `trace_segment(name=R, start=[lon, lat], end=[lon, lat])`. Give the name
   as written, without "the": prefix matching lets "Western" find both
   WESTERN HIGHWAY and WESTERN FREEWAY, which a road that changes class needs.
3. Read the result before using it:
   - `names_m` - is it the road the text means? If `matched_names` shows a
     different road joined in, call again with the full name and
     `match: "exact"`.
   - `start_offset_m`, `end_offset_m` - how far each anchor is from the
     line. A town centre a few kilometres off a bypass is normal. A large
     offset on one end means the stretch stops well short of that town: say
     so in the label ("to the X turn-off").
   - `gaps_m`, `detour_note` - mention either in your report.
4. Pass the candidate to `assemble.py` unchanged, with `"tier": "relative"`.
   The flag `anchor_approximate` is added for you when the ends are loose.

`no_match`, `too_far` and `no_path` all mean there is no line: the mention
is unresolved. Tell the user which, in the tool's own words.

## Lines offline

A straight line between two towns is not "along the highway". Offline, the
honest output for "along R between X and Y" is unresolved with
`needs_network`, plus the two anchors resolved as their own features if they
are mentions in their own right. Use `build_geometry.py line` only when the
text describes a straight line (a flight path, a line of sight, "a line
from X to Y").
