---
description: Turn the places in a file, table, text or photo set into GeoJSON features with confidence and provenance
argument-hint: "[file(s) or text] [--offline] [--tier4] [--basemap none]"
---

Use the `spatialise-anything` skill to spatialise the following input:

$ARGUMENTS

If no input is given above, use the files or text the user has shared in this conversation, and ask once which ones if it is not obvious.

Options the user may have included:
- `--offline` - do not call the MCP server even if it is connected.
- `--tier4` - allow implicit (tier 4) inference from organisations, facilities and events. Off by default; every inferred feature is flagged.
- `--basemap none` - the material is sensitive; build the preview without public map tiles.

Follow the skill's pipeline and rules. Finish with: the GeoJSON file, the map preview, counts by status, every ambiguous and unresolved mention with its reason, and at most one batched question if an answer would change the map. Do not publish anywhere unless the user explicitly asks and names the layer.
