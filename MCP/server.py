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
state: dict = {"part": None, "model": None, "notes": [], "check": None}


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
    spec (valid solid, overall size, every hole probed for diameter and depth,
    fillets/chamfers applied or listed as not modelled).
    Returns all check results. Call export_step afterwards if ok is true."""
    parsed, errors = V.parse(part)
    if parsed is None:
        return {"ok": False, "stage": "schema", "errors": errors}
    errors = V.plausibility(parsed)
    if errors:
        return {"ok": False, "stage": "plausibility", "errors": errors}
    notes: list = []
    try:
        model = builder.build(parsed, notes)
    except Exception as e:
        state.update(part=None, model=None, notes=[], check=None)
        return {"ok": False, "stage": "build", "errors": [{"problem": str(e)}]}
    result = V.check_model(model, parsed, notes)
    state.update(part=parsed, model=model, notes=notes, check=result)
    result["stage"] = "model_check"
    result["review_list"] = V.review_list(parsed, notes)
    return result


@mcp.tool()
def export_step(filename: str) -> dict:
    """Export the last built model as STEP (filename without path, e.g.
    'grundplatte.step'), re-import it, and save <name>_review.json next to it
    (review list + model check). Refuses if the last model check failed."""
    if state["model"] is None:
        return {"ok": False, "error": "Nothing built yet. Call build_from_json first."}
    if not state["check"]["ok"]:
        failed = [k for k, v in state["check"]["checks"].items() if not v]
        return {"ok": False, "error": f"Last build failed the model check {failed}; fix the JSON and rebuild."}
    name = os.path.basename(filename)
    if not name.lower().endswith((".step", ".stp")):
        name += ".step"
    path = os.path.join(OUTPUT_DIR, name)
    cq.exporters.export(state["model"], path)
    step = V.check_step(path)
    review_path = os.path.splitext(path)[0] + "_review.json"
    with open(review_path, "w", encoding="utf-8") as f:
        json.dump({"part": state["part"].name, "step_file": name,
                   "validated": state["check"]["ok"] and step["reimport_ok"],
                   "model_check": state["check"], "step_check": step,
                   "review_list": V.review_list(state["part"], state["notes"])},
                  f, indent=2, ensure_ascii=False)
    return {"ok": step["reimport_ok"], "path": path, "review_file": review_path, **step}


if __name__ == "__main__":
    mcp.run()
