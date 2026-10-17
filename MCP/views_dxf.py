"""Model -> DXF views with hidden-line removal (OCP HLR), so the built part can
be laid over the drawing DXF from load_drawing in any CAD/DXF viewer.

Per view one DXF (<stem>_<view>.dxf) plus <stem>_views.dxf with all views laid
out like an ISO first-angle sheet (top view below the front view, view from
the left to the right of it, isometric top right). Part mm, 1:1. Layers
VISIBLE and HIDDEN (dashed), the same names pdf2dxf uses for the drawing.
Tangent (smooth) edges are left out, as on a drawing."""
from __future__ import annotations

import math
import os

import cadquery as cq
import ezdxf
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Line
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape

STEP = 0.2  # polyline sampling of free curves, mm
GAP = 20.0  # space between views on the combined sheet, mm

# name: (view normal pointing at the viewer, view x axis); view y = normal x xdir
VIEWS = {
    "front": ((0, -1, 0), (1, 0, 0)),
    "top": ((0, 0, 1), (1, 0, 0)),
    "left": ((-1, 0, 0), (0, -1, 0)),
    "iso": ((1, -1, 1), (1, 1, 0)),
}


def project(shape, normal, xdir) -> list[dict]:
    algo = HLRBRep_Algo()
    algo.Add(shape)
    algo.Projector(HLRAlgo_Projector(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(*normal), gp_Dir(*xdir))))
    algo.Update()
    algo.Hide()
    h = HLRBRep_HLRToShape(algo)
    out = []
    for kind, comps in (("VISIBLE", (h.VCompound(), h.OutLineVCompound())),
                        ("HIDDEN", (h.HCompound(), h.OutLineHCompound()))):
        for comp in comps:
            if comp is None or comp.IsNull():
                continue
            for e in cq.Shape.cast(comp).Edges():
                r = _record(e, kind)
                if r:
                    out.append(r)
    return out


def _record(e, layer) -> dict | None:
    L = e.Length()
    if L < 1e-6:
        return None
    ad = BRepAdaptor_Curve(e.wrapped)
    t = ad.GetType()
    p0, p1 = e.startPoint(), e.endPoint()
    if t == GeomAbs_Line:
        return {"layer": layer, "type": "line", "pts": [(p0.x, p0.y), (p1.x, p1.y)]}
    if t == GeomAbs_Circle:
        c = ad.Circle()
        cx, cy, r = c.Location().X(), c.Location().Y(), c.Radius()
        if abs(L - 2 * math.pi * r) < 1e-3:
            return {"layer": layer, "type": "circle", "c": (cx, cy), "r": r}
        a0 = math.degrees(math.atan2(p0.y - cy, p0.x - cx))
        a1 = math.degrees(math.atan2(p1.y - cy, p1.x - cx))
        m = e.positionAt(0.5)
        am = math.degrees(math.atan2(m.y - cy, m.x - cx))
        if (am - a0) % 360 > (a1 - a0) % 360:  # midpoint not on the ccw arc a0 -> a1
            a0, a1 = a1, a0
        return {"layer": layer, "type": "arc", "c": (cx, cy), "r": r, "a": (a0, a1)}
    n = max(2, int(math.ceil(L / STEP)) + 1)
    return {"layer": layer, "type": "poly",
            "pts": [(p.x, p.y) for p in e.positions([i / (n - 1) for i in range(n)])]}


def _bbox(recs):
    xs, ys = [], []
    for r in recs:
        if r["type"] in ("circle", "arc"):
            (cx, cy), R = r["c"], r["r"]
            xs += [cx - R, cx + R]
            ys += [cy - R, cy + R]
        else:
            xs += [p[0] for p in r["pts"]]
            ys += [p[1] for p in r["pts"]]
    return (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 0, 0)


def _new_doc():
    doc = ezdxf.new(units=ezdxf.units.MM, setup=["linetypes"])
    doc.layers.add("VISIBLE")
    doc.layers.add("HIDDEN", linetype="DASHED")
    return doc


def _add(msp, recs, dx=0.0, dy=0.0):
    for r in recs:
        a = {"layer": r["layer"]}
        if r["type"] == "line":
            (x0, y0), (x1, y1) = r["pts"]
            msp.add_line((x0 + dx, y0 + dy), (x1 + dx, y1 + dy), dxfattribs=a)
        elif r["type"] == "circle":
            msp.add_circle((r["c"][0] + dx, r["c"][1] + dy), r["r"], dxfattribs=a)
        elif r["type"] == "arc":
            msp.add_arc((r["c"][0] + dx, r["c"][1] + dy), r["r"], r["a"][0], r["a"][1], dxfattribs=a)
        else:
            msp.add_lwpolyline([(x + dx, y + dy) for x, y in r["pts"]], dxfattribs=a)


def export(model, out_dir: str, stem: str) -> dict:
    """Write the view DXFs. Returns {"views": path, "front": path, "top": ..., "left": ..., "iso": ...}."""
    shape = (model.val() if isinstance(model, cq.Workplane) else model).wrapped
    proj = {name: project(shape, *nd) for name, nd in VIEWS.items()}
    files = {}
    for name, recs in proj.items():
        doc = _new_doc()
        _add(doc.modelspace(), recs)
        files[name] = os.path.join(out_dir, f"{stem}_{name}.dxf")
        doc.saveas(files[name])

    # first-angle layout around the front view; views keep their own coordinates
    bb = {k: _bbox(v) for k, v in proj.items()}
    fx0, fy0, fx1, fy1 = bb["front"]
    off = {"front": (0.0, 0.0),
           "top": (fx0 - bb["top"][0], fy0 - GAP - bb["top"][3]),
           "left": (fx1 + GAP - bb["left"][0], fy0 - bb["left"][1])}
    right = max(fx1, bb["left"][2] + off["left"][0])
    off["iso"] = (right + GAP - bb["iso"][0], fy1 + GAP - bb["iso"][1])
    doc = _new_doc()
    doc.layers.add("LABEL")
    msp = doc.modelspace()
    h = max(2.5, 0.015 * max(fx1 - fx0, fy1 - fy0, 1))
    for name, recs in proj.items():
        dx, dy = off[name]
        _add(msp, recs, dx, dy)
        x0, _, _, y1 = bb[name]
        msp.add_text(name, height=h, dxfattribs={"layer": "LABEL"}).set_placement((x0 + dx, y1 + dy + 0.5 * h))
    files["views"] = os.path.join(out_dir, f"{stem}_views.dxf")
    doc.saveas(files["views"])
    return {"views": files.pop("views"), **files}
