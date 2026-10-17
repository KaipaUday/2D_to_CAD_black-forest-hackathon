# Drawing2CAD MCP server

JSON part description -> validate -> build (CadQuery) -> check -> STEP.
No AI inside the server: the AI writes the JSON, the server is deterministic.

## Files
- `schema.py`      Pydantic models = the JSON format (+ conventions in the docstring)
- `builder.py`     Part -> CadQuery solid (plain functions, testable without MCP)
- `validation.py`  schema, plausibility, overlap warnings, review list, model checks, STEP re-import
- `server.py`      MCP wiring only (4 tools)
- `examples/test_plate.json`  example using every feature type

## Tools
1. `get_part_schema()`          JSON schema + conventions (AI calls this first)
2. `validate_part_json(part)`   errors to fix + review list, BEFORE building
3. `build_from_json(part)`      builds and checks: valid solid, size, every hole found
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
mm; origin = lower-left corner of the top view on the bottom face;
all cuts go from the top face down; missing values -> `assumptions` with a reason.
