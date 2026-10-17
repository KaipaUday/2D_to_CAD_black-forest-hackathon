"""Drawing2CAD MCP server: JSON spec -> validate -> build -> check -> STEP.

Requires: pip install "mcp>=2" cadquery pydantic shapely
Claude Desktop config: command = <venv>/bin/python, args = [<path>/server.py]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cadquery as cq  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

import builder  # noqa: E402
import schema  # noqa: E402
import validation as V  # noqa: E402

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.expanduser("~/drawing2cad_output"))
os.makedirs(OUTPUT_DIR, exist_ok=True)

mcp = MCPServer("drawing2cad")
state: dict = {"part": None, "model": None}


@mcp.tool()
def get_part_schema() -> dict:
    """Return the JSON schema and conventions for a part description.
    Call this FIRST, then write the part JSON for the drawing."""
    return {"conventions": schema.__doc__,
            "json_schema": schema.Part.model_json_schema()}


@mcp.tool()
def validate_part_json(part: dict) -> dict:
    """Check a part description BEFORE building: schema, plausibility
    (features inside the part, depths, counterbore sizes...) and the review list.
    Fix every error and call again until ok is true."""
    parsed, errors = V.parse(part)
    if parsed is None:
        return {"ok": False, "errors": errors}
    errors = V.plausibility(parsed)
    return {"ok": not errors, "errors": errors, "review_list": V.review_list(parsed)}


@mcp.tool()
def build_from_json(part: dict) -> dict:
    """Validate the part JSON, then build the 3D model and check it against the
    spec (valid solid, overall size, every hole found at the right place).
    Returns all check results. Call export_step afterwards if ok is true."""
    parsed, errors = V.parse(part)
    if parsed is None:
        return {"ok": False, "stage": "schema", "errors": errors}
    errors = V.plausibility(parsed)
    if errors:
        return {"ok": False, "stage": "plausibility", "errors": errors}
    try:
        model = builder.build(parsed)
    except Exception as e:
        return {"ok": False, "stage": "build", "errors": [{"problem": str(e)}]}
    state["part"], state["model"] = parsed, model
    result = V.check_model(model, parsed)
    result["stage"] = "model_check"
    result["review_list"] = V.review_list(parsed)
    return result


@mcp.tool()
def export_step(filename: str) -> dict:
    """Export the last built model as STEP (filename without path, e.g.
    'grundplatte.step'), re-import it, and save the review list next to it."""
    if state["model"] is None:
        return {"ok": False, "error": "Nothing built yet. Call build_from_json first."}
    name = os.path.basename(filename)
    if not name.lower().endswith((".step", ".stp")):
        name += ".step"
    path = os.path.join(OUTPUT_DIR, name)
    cq.exporters.export(state["model"], path)
    step = V.check_step(path)
    review_path = os.path.splitext(path)[0] + "_review.json"
    with open(review_path, "w", encoding="utf-8") as f:
        json.dump(V.review_list(state["part"]), f, indent=2, ensure_ascii=False)
    return {"ok": step["reimport_ok"], "path": path, "review_file": review_path, **step}


if __name__ == "__main__":
    mcp.run()
