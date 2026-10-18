# Drawing to CAD review extraction

Use the uploaded engineering PDF as the authoritative source and compare it with every DXF returned by the drawing conversion MCP tool.

Return a JSON object with these top-level fields:

- `features`: an array of dimensions, geometry, materials, tolerances, holes, slots, radii, and other manufacturable features found in the drawing.
- `review_list`: an array of ambiguous or unconfirmed items that need a human decision.
- `confidence`: a number from 0 to 1 for each extracted feature or review item.
- `units`: the drawing units, or `null` when they cannot be determined.
- `source_notes`: concise notes about drawing regions that were unclear or conflicting.

Each feature should include a stable `id`, a short `item` name, a `value` or geometry description, `confidence`, and a `source` description such as a sheet, view, dimension label, or DXF entity. Do not invent missing dimensions. Preserve uncertainty explicitly. The returned JSON must be valid and contain no Markdown fences.

When the user edits and saves the JSON, use the edited values as the source of truth for regenerating the CAD model. Do not discard their edits or silently replace them with a fresh extraction.
