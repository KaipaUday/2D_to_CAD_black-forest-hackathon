# Drawing2CAD MCP server

PDF drawing -> `load_drawing` (DXF + drawing JSON + page image) -> AI writes the part JSON
-> validate -> build (CadQuery) -> check -> preview PNG -> confirm unsure values -> STEP.
No AI inside the server: the AI reads the drawing and writes the JSON, the server is deterministic.
Agent-facing summary: [../skills/drawing2cad-convert/SKILL.md](../skills/drawing2cad-convert/SKILL.md);
the server sends the same rules as its MCP instructions (`guide.py`). Clients: Claude Desktop or the web
UI in [../UI-Server](../UI-Server/README.md).

## Pipeline
1. `load_drawing(pdf_path, page=1, scale=None)` runs `pdf2dxf.py`, saves `<name>.dxf` in OUTPUT_DIR and returns
   - a drawing JSON: sheet size, frame, title block area, scale (stated text + measured from dimension text vs. geometry),
     views (clustered visible outlines, kind hint orthographic / section / isometric, alignment between views),
     per view circles (Ø), arcs (R), lines (hidden flag) in view-local coordinates (x `scale`),
     dimension lines (sheet length, linked circle Ø / arc R, linked text or text region), texts / text regions;
   - the page as PNG with the views boxed V1, V2, ...
   PDFs with outlined lettering (all CAx/BCT samples except the 1_xxx sheets) have no extractable text:
   the JSON says so (`text_mode: outlined`) and the AI reads values from the image (`zoom_drawing`).
2. `zoom_drawing(view | region)` returns a close-up image for small text and symbols.
3. `get_part_schema()` gives the workflow, a drawing-reading guide (views, sections, symbols, general notes),
   the conventions, the JSON schema (every field described in drawing terms) and a worked example (Fuss).
4. `validate_part_json` / `build_from_json` build first, confirm after: the result has the model check, the
   review list, a short **confirm** list (assumptions with confidence medium/low, flagged or low-confidence
   features), `status` draft/final and a PNG preview (isometric, top, front).
5. `confirm_items(ids)` records the user's yes; a different value means: edit the JSON and rebuild.
6. `export_step(name)` writes `<name>.step` (or `<name>_draft.step` while confirm items are open),
   `<name>_review.json`, `<name>_preview.png` and the model view DXFs.

### DXF in every response
Every tool response has `"dxf": {"drawing": <path>, "model": {...}}`:
- `drawing`: the DXF of the last `load_drawing` (sheet mm, layers VISIBLE / HIDDEN / CENTER / DIMENSION ...).
- `model`: after `build_from_json` (`<part name>_*.dxf`) and again after `export_step` (`<step name>_*.dxf`):
  `views` (front, top below it, left view to its right, iso top right, ISO first angle) plus one file per view
  (`front`, `top`, `left`, `iso`). Part mm 1:1, hidden-line removal, layers VISIBLE and HIDDEN (dashed), `views_dxf.py`.
  A failed DXF export is reported as `dxf_error`, it never fails the build.

### Request log
Every tool call is appended to `OUTPUT_DIR/mcp_log.jsonl`, one JSON per line:
`ts` (local time, ms), `seq`, `pid`, `tool`, `request` (arguments; a part JSON sent as a string is stored as JSON),
`files_in` (files named in the request), `files_out` (files written by this call), `summary`
(ok / status / stage / errors), `response` (without images; responses over 50k chars are not stored) and `duration_ms`.
`tail -f output/mcp_log.jsonl` to watch live, or `pandas.read_json(path, lines=True)`.
`DRAWING2CAD_LOG=<path>` moves it, `DRAWING2CAD_LOG=0` switches it off.

### pdf2dxf.py (also a CLI: `python pdf2dxf.py in.pdf out.dxf` or folders)
- closed ring of cubic Beziers with a square bbox -> CIRCLE; every Bezier that fits a circle -> ARC;
  arcs covering 360° -> CIRCLE; flattened curves (chains of short segments on a circle) -> ARC/CIRCLE
- dash patterns written as separate segments -> one line on layer HIDDEN or CENTER
- arrowheads (filled or stroked slim triangles) dropped; lines ending at an arrow tip -> DIMENSION
- extension and hatch lines on their own layers; real text per line (`get_text` lines), outlined text as TEXT_REGIONS

## Files
- `schema.py`      Pydantic models = the JSON format (+ conventions in the docstring), extra=forbid everywhere
- `outline.py`     exact 2D outlines: per-vertex radius/chamfer, circle hulls (lines + arcs)
- `threads.py`     ISO metric thread table (nominal, coarse pitch, tap drill)
- `builder.py`     Part -> CadQuery solid (plain functions, testable without MCP)
- `normalize.py`   forgiving input: fixes AI format slips ('THRU', {"x","y"} points, confidence words, note fields,
                   type aliases, part sent as a JSON string) and lists them as `format_fixes`; readable schema errors
- `validation.py`  schema, plausibility, warnings, defaulted fields, review list, model probes, STEP re-import
- `pdf2dxf.py`     PDF page -> clean entities (circles, arcs, hidden/centre/dimension lines, texts) -> DXF
- `drawing.py`     entities -> drawing JSON (frame, title block, views, dimensions, scale) + page PNG
- `preview.py`     built model -> PNG (isometric, top, front)
- `views_dxf.py`   built model -> DXF views with hidden lines (front, top, left, iso + combined sheet)
- `guide.py`       server instructions, workflow, drawing-reading guide, worked example
- `server.py`      MCP wiring (7 tools), DXF paths in every response, JSONL request log
- `run_json.py`    test JSON files without MCP: `python run_json.py examples/pad_fuss.json` (exit 1 on failure)
- `test_all.py`    builds every example (+ view DXFs), checks that 23 bad JSONs are rejected, the confirm list,
                   arc/circle detection, the MCP tools end to end (DXF paths, request log), and with
                   `DRAWINGS_DIR=<folder with fuss.pdf grundplatte.pdf lagerschaft.pdf>` the PDF reading.
                   The Docker build runs it, so keep it.
- `examples/`      one JSON per feature; `test_plate.json` = all cut types, `lagerschaft_ai_full.json` = a full
                   AI-written answer (title block, modeling plan, verification) that must still build

## Features
| Kind | Types |
|---|---|
| body | `box`, `polygon` (vertex `r` radius / `c` chamfer, convex or concave), `circles_hull` (exact tangent hull), `revolve` (turned parts, axis z or x, profile with `r`/`c`) |
| holes | `hole`, `counterbore_hole`, `countersink_hole` (from the top, `from_z`), `side_hole` (front/back/left/right/bottom face), `axis_hole` (any direction) |
| threads | `thread: "M8"` or `{"designation": "M10x1", "depth": 12, "tolerance": "6H"}` on any hole (tap drill from the ISO table, diameter optional) or on a circle `pad` (external). Cosmetic: recorded in the review JSON |
| cuts | `slot`, `pocket`, `pocket_polygon`, `cavity` with islands `boss` / `rib` / `tab` |
| added material | `pad` (circle / rect / polygon, `z0`, `height`, `taper_deg`, `bore_diameter` + `bore_depth`), `bottom_disc` |
| edges | `fillet` (radius) / `chamfer` (size, 45°) on `top_outline`, `bottom_outline`, `holes` (targets + side entry/exit/both), `pad_top`, `pad_root` |

Build order: body -> cavity + islands -> pads -> bottom discs -> holes/cuts -> hole chamfers (exact cones) -> fillets/chamfers.
If the CadQuery kernel cannot do a fillet/chamfer, the model is kept without it and the item goes on the review list as `not_modelled`.

## Checks
- Before the build: schema (types, positive dims, unknown keys), radius fits the adjacent edges, features inside the outline,
  depths, counterbore/countersink sizes, pad on the part / bore and taper fit, thread vs. diameter, side-hole position on the face,
  fillet smaller than thickness / pad / hole, targets exist.
- Review list: assumptions, flags, low confidence, warnings, cosmetic threads, not-modelled items and every field that was left out and **defaulted**.
- After the build: valid single solid, bounding box and z range, every hole probed (air inside the wall, material outside,
  air down to the bottom, material below a blind hole), round bosses/discs/pads found, fillet/chamfer results.
- `export_step` refuses a model that failed the check and writes `<name>_review.json` (review list + model check + STEP re-import).

## Tools
1. `load_drawing(pdf_path, page, scale)`  DXF + drawing JSON + page image (AI calls this first)
2. `zoom_drawing(view | region)`          close-up image
3. `get_part_schema()`                    workflow, reading guide, conventions, JSON schema, worked example
4. `validate_part_json(part)`             errors to fix + review list + confirm list, BEFORE building
5. `build_from_json(part)`                builds, checks, confirm list, preview PNG, model view DXFs
6. `confirm_items(ids)`                   user agreed to confirm items
7. `export_step(filename)`                STEP (draft/final) + re-import check + review JSON + preview PNG + DXFs

## Install (Python 3.10+)
    python -m pip install -r requirements.txt   # mcp>=2 cadquery pydantic shapely pymupdf ezdxf
    python test_all.py                          # the same suite the root Docker build runs

Docker (from the repo root): `docker build -t drawing2cad .` builds the image and runs `test_all.py`;
a failing check fails the build.

## Claude Desktop config
    "drawing2cad": {
      "command": "/Users/xx/GitHub/2D_to_CAD_black-forest-hackathon/.venv/bin/python",
      "args": ["/Users/xx/GitHub/2D_to_CAD_black-forest-hackathon/MCP/server.py"],
      "env": {"OUTPUT_DIR": "/Users/xx/GitHub/2D_to_CAD_black-forest-hackathon/output",
              "INPUT_DIR": "/Users/xx/Drawings"}
    }

## Conventions
mm; origin = lower-left corner of the top view on the bottom face; cuts from the top face down
(depth from the plate top unless `from_z`); missing values -> `assumptions` with a reason.

## Env
`OUTPUT_DIR` (STEP, review JSON, DXF, previews, `mcp_log.jsonl`), `DRAWING2CAD_LOG` (log path or `0`), `INPUT_DIR` (relative PDF paths), `DRAWING2CAD_NOTIFY=0` turns off the
macOS notification (`osascript`, skipped automatically on other systems, e.g. in Docker).
