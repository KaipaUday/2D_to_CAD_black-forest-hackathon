---
name: drawing2cad-convert
description: Convert a 2D technical drawing PDF into a validated 3D STEP model with the drawing2cad MCP server. Use when asked to turn a drawing (PDF/DXF) into CAD, STEP or a 3D model.
---

# Drawing PDF -> STEP with the drawing2cad MCP server

You never write geometry or CAD code. You read the drawing and write a **part JSON**; the server
validates it, builds the solid with CadQuery, checks the model and exports STEP plus a review JSON.
Use only the MCP tools: your own sandbox cannot see the server's files.
Setup (Docker image + Claude Desktop config, or the web UI): repo `README.md`, sections 1-3.
If the `drawing2cad` tools are missing, the server is not connected; say so instead of improvising.

## Flow
1. `load_drawing(pdf_path)`: page image with views boxed V1, V2, ... + drawing JSON (views, circles Ø,
   arcs R, hidden/centre lines, dimension lines, texts) + `<name>.dxf`. Multi-page PDF: load every page.
   The DXF geometry is the primary source for positions, the image and the dimension texts for values.
2. Scale: dimension value / dimension `sheet_length`. Sheets often say 1:1 but are drawn at 1:2; reload
   with `scale=<factor>` so view geometry is in part mm. Written dimensions always win over measurements.
3. `zoom_drawing(view | region)` for small text, symbols, threads, tolerances.
4. `get_part_schema()`: conventions, the JSON schema and a worked example. Read it before writing JSON.
5. Write the part JSON: base body (`box` | `polygon` | `circles_hull` | `revolve`), then holes, threads,
   slots, pockets, cavity + islands, pads, bottom discs, side/axis holes, fillets/chamfers.
   A feature the schema cannot express goes in as the nearest feature with `review` set. Never stop
   without building.
6. `validate_part_json` until ok, then `build_from_json`. Compare the preview PNG and the model view
   DXFs with the drawing views.
7. Ask the user only the short **confirm** list (one line each with your value). Agreed ->
   `confirm_items(ids)`; different value -> edit the JSON and rebuild.
8. `export_step("<part>.step")`. With open confirm items it is saved as `<part>_draft.step`.

## Rules
- Units mm. Origin = lower-left corner of the outline bounding box in the top view, z = 0 on the
  underside; X right, Y up (top view), Z up. Cuts go down from the top face unless `from_z` is set.
- Never invent a dimension silently: every value not written on the drawing goes into `assumptions`
  `{item, value, reason, confidence}` (high = follows from the drawing, medium = plausible, low = guess).
- Draft angles and cosmetic threads are recorded in the review JSON; a fillet the kernel cannot make is
  kept off the model and listed as `not_modelled`.

## Outputs (in OUTPUT_DIR)
`<part>.step` (or `_draft.step`), `<part>_review.json` (assumptions, review list, model check, STEP
re-import), `<part>_preview.png`, `<part>_views.dxf` + `_front/_top/_left/_iso.dxf`, `mcp_log.jsonl`.

The same instructions are sent by the server itself (`MCP/guide.py`, `SERVER_INSTRUCTIONS`), so any MCP
client gets them without this file.
