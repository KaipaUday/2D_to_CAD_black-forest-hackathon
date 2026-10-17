# Drawing2CAD MCP server

JSON part description -> validate -> build (CadQuery) -> check -> STEP.
No AI inside the server: the AI writes the JSON, the server is deterministic.

## Files
- `schema.py`      Pydantic models = the JSON format (+ conventions in the docstring), extra=forbid everywhere
- `outline.py`     exact 2D outlines: per-vertex radius/chamfer, circle hulls (lines + arcs)
- `threads.py`     ISO metric thread table (nominal, coarse pitch, tap drill)
- `builder.py`     Part -> CadQuery solid (plain functions, testable without MCP)
- `validation.py`  schema, plausibility, warnings, defaulted fields, review list, model probes, STEP re-import
- `server.py`      MCP wiring only (4 tools)
- `run_json.py`    test JSON files without MCP: `python run_json.py examples/pad_fuss.json` (exit 1 on failure)
- `test_all.py`    builds every example and checks that 23 bad JSONs are rejected

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
1. `get_part_schema()`          JSON schema + conventions (AI calls this first)
2. `validate_part_json(part)`   errors to fix + review list, BEFORE building
3. `build_from_json(part)`      builds and checks
4. `export_step(filename)`      STEP + re-import check + `<name>_review.json`

## Install (Python 3.10+)
    python -m pip install "mcp>=2" cadquery pydantic shapely

## Claude Desktop config
    "drawing2cad": {
      "command": "/Users/xx/GitHub/2D_to_CAD_black-forest-hackathon/.venv/bin/python",
      "args": ["/Users/xx/GitHub/2D_to_CAD_black-forest-hackathon/MCP/server.py"],
      "env": {"OUTPUT_DIR": "/Users/xx/GitHub/2D_to_CAD_black-forest-hackathon/output"}
    }

## Conventions
mm; origin = lower-left corner of the top view on the bottom face; cuts from the top face down
(depth from the plate top unless `from_z`); missing values -> `assumptions` with a reason.
