"""Minimal Drawing2CAD MCP server: 3 tools + validation.

Run in Claude Desktop (config) or with agent.py.
Requires: pip install cadquery "mcp>=2"
"""
import os

import cadquery as cq
from mcp.server.mcpserver import MCPServer

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.expanduser("~/drawing2cad_output"))
os.makedirs(OUTPUT_DIR, exist_ok=True)

mcp = MCPServer("drawing2cad")
state = {"part": None}


def validate() -> dict:
    """Check the current part and return machine-readable results."""
    part = state["part"]
    if part is None:
        return {"ok": False, "error": "No part yet. Call create_box first."}
    solid = part.val()
    bb = solid.BoundingBox()
    holes = []
    for f in part.faces("%CYLINDER").vals():
        circle = next(e for e in f.Edges() if e.geomType() == "CIRCLE")
        c = f.Center()  # lies on the hole axis
        holes.append({"diameter": round(circle.radius() * 2, 3),
                      "x": round(c.x, 3), "y": round(c.y, 3)})
    holes.sort(key=lambda h: (h["x"], h["y"]))
    return {
        "ok": solid.isValid(),
        "valid_solid": solid.isValid(),
        "size_xyz": [round(bb.xlen, 3), round(bb.ylen, 3), round(bb.zlen, 3)],
        "holes_found": holes,
    }


@mcp.tool()
def create_box(length: float, width: float, thickness: float) -> dict:
    """Create the base body: a rectangular block in mm.

    length = X (left-right in top view), width = Y (bottom-top in top view),
    thickness = Z. The origin is the lower-left corner of the top view, on
    the bottom face. Replaces any existing part.
    """
    state["part"] = cq.Workplane("XY").box(length, width, thickness,
                                           centered=False)
    return validate()


@mcp.tool()
def add_hole(x: float, y: float, diameter: float) -> dict:
    """Drill a THROUGH hole from the top face.

    x, y in mm from the lower-left corner as seen in the top view.
    Call once per hole (for '2x Ø10', call twice).
    """
    if state["part"] is None:
        return {"ok": False, "error": "No part yet. Call create_box first."}
    top_z = state["part"].val().BoundingBox().zmax
    state["part"] = (state["part"].faces(">Z")
                     .workplane(origin=(0, 0, top_z))
                     .pushPoints([(x, y)]).hole(diameter))
    return validate()


@mcp.tool()
def export_step(filename: str) -> dict:
    """Export the current part as a STEP file and check it re-imports.

    filename without path, e.g. 'plate.step'.
    """
    if state["part"] is None:
        return {"ok": False, "error": "No part yet. Call create_box first."}
    path = os.path.join(OUTPUT_DIR, os.path.basename(filename))
    cq.exporters.export(state["part"], path)
    try:
        reimported = cq.importers.importStep(path)
        reimport_ok = reimported.val().isValid()
    except Exception as e:  # report, don't crash
        return {"ok": False, "path": path, "reimport_error": str(e)}
    return {"ok": reimport_ok, "path": path, "reimport_ok": reimport_ok}


if __name__ == "__main__":
    mcp.run()
