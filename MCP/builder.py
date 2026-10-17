"""Deterministic geometry: Part -> CadQuery solid. No AI, no MCP.

Build order: body -> cavity with islands -> holes/slots/pockets -> bottom discs.
"""
from __future__ import annotations

import math

import cadquery as cq
from shapely.geometry import LineString, Polygon

from schema import (Boss, BottomDisc, BoxBody, Cavity, CounterboreHole,
                    CountersinkHole, Hole, Part, Pocket, PolygonBody,
                    PolygonPocket, Rib, Slot, Tab)

EPS = 0.01


def body_polygon(body) -> Polygon:
    if isinstance(body, BoxBody):
        return Polygon([(0, 0), (body.length, 0), (body.length, body.width), (0, body.width)])
    return Polygon(body.points)


def disc_height(part: Part) -> float:
    return max([f.height for f in part.features if isinstance(f, BottomDisc)], default=0.0)


def _depth(d, part: Part) -> float:
    """'through' cuts everything incl. bottom discs, plus a margin."""
    if d == "through":
        return part.body.thickness + disc_height(part) + 1.0
    return float(d)


def _top(t: float) -> cq.Workplane:
    """Workplane on the top surface, global XY origin (lower-left corner)."""
    return cq.Workplane("XY", origin=(0, 0, t))


# ---------- body ----------
def build_body(body) -> cq.Workplane:
    t = body.thickness
    if isinstance(body, BoxBody):
        wp = cq.Workplane("XY").box(body.length, body.width, t, centered=False)
    elif isinstance(body, PolygonBody):
        wp = cq.Workplane("XY").polyline(body.points).close().extrude(t)
    else:
        raise ValueError(f"unknown body type {body.type}")
    if body.corner_radius > 0:
        wp = wp.edges("|Z").fillet(body.corner_radius)
    return wp


# ---------- cavity + islands ----------
def inner_wall_coordinate(part: Part, cav: Cavity, tab: Tab) -> float:
    """Where the inner wall face is, at the tab's position."""
    inner = body_polygon(part.body).buffer(-cav.wall_thickness, join_style=2)
    if tab.wall in ("top", "bottom"):
        line = LineString([(tab.center_x, -1e4), (tab.center_x, 1e4)])
        b = inner.intersection(line).bounds  # (minx, miny, maxx, maxy)
        return b[3] if tab.wall == "top" else b[1]
    line = LineString([(-1e4, tab.center_y), (1e4, tab.center_y)])
    b = inner.intersection(line).bounds
    return b[2] if tab.wall == "right" else b[0]


def island_solid(f, part: Part, cav: Cavity, floor_z: float) -> cq.Workplane:
    t = part.body.thickness
    top = f.top_z if getattr(f, "top_z", None) is not None else t
    h = top - floor_z + EPS
    base = cq.Workplane("XY", origin=(0, 0, floor_z - EPS))
    if isinstance(f, Boss):
        return base.center(*f.center).circle(f.diameter / 2).extrude(h)
    if isinstance(f, Rib):
        sx, sy = (f.length, f.width) if f.direction == "x" else (f.width, f.length)
        return base.center(*f.center).rect(sx, sy).extrude(h)
    if isinstance(f, Tab):
        wall_pos = inner_wall_coordinate(part, cav, f)
        reach = cav.wall_thickness + 1.0   # extend into the wall so it merges
        L = f.length_from_inner_wall
        if f.wall == "top":
            cx, cy, sx, sy = f.center_x, wall_pos - L + (L + reach) / 2, f.width, L + reach
        elif f.wall == "bottom":
            cx, cy, sx, sy = f.center_x, wall_pos + L - (L + reach) / 2, f.width, L + reach
        elif f.wall == "right":
            cx, cy, sx, sy = wall_pos - L + (L + reach) / 2, f.center_y, L + reach, f.width
        else:
            cx, cy, sx, sy = wall_pos + L - (L + reach) / 2, f.center_y, L + reach, f.width
        s = base.center(cx, cy).rect(sx, sy).extrude(h)
        if f.tip_corner_radius > 0:
            s = s.edges("|Z").fillet(min(f.tip_corner_radius, f.width / 2 - EPS))
        return s
    raise ValueError(f"{f.type} is not an island type")


def apply_cavity(wp: cq.Workplane, part: Part, cav: Cavity) -> cq.Workplane:
    t = part.body.thickness
    floor_z = t - cav.depth
    outer = wp.faces("<Z").val().outerWire()
    inner = outer.offset2D(-cav.wall_thickness, "arc")
    if len(inner) != 1:
        raise ValueError("Wall offset split the cavity into several regions")
    wire = inner[0].translate(cq.Vector(0, 0, floor_z))
    cutter = cq.Workplane("XY").add(
        cq.Solid.extrudeLinear(cq.Face.makeFromWires(wire), cq.Vector(0, 0, cav.depth + 1)))
    for f in part.features:
        if isinstance(f, (Boss, Rib, Tab)):
            cutter = cutter.cut(island_solid(f, part, cav, floor_z))
    return wp.cut(cutter)


# ---------- cutting features ----------
def apply_cut(wp: cq.Workplane, f, part: Part) -> cq.Workplane:
    t = part.body.thickness
    if isinstance(f, Hole):
        return wp.cut(_top(t).pushPoints(f.positions).circle(f.diameter / 2)
                      .extrude(-_depth(f.depth, part)))

    if isinstance(f, CounterboreHole):
        hole = _top(t).pushPoints(f.positions).circle(f.diameter / 2).extrude(-_depth(f.depth, part))
        cbore = _top(t).pushPoints(f.positions).circle(f.cbore_diameter / 2).extrude(-f.cbore_depth)
        return wp.cut(hole).cut(cbore)

    if isinstance(f, CountersinkHole):
        result = wp.cut(_top(t).pushPoints(f.positions).circle(f.diameter / 2)
                        .extrude(-_depth(f.depth, part)))
        r_top = f.csk_diameter / 2
        h = (r_top - f.diameter / 2) / math.tan(math.radians(f.csk_angle / 2))
        for (x, y) in f.positions:
            cone = cq.Solid.makeCone(f.diameter / 2, r_top, h, pnt=cq.Vector(x, y, t - h),
                                     dir=cq.Vector(0, 0, 1))
            result = result.cut(cq.Workplane().add(cone))
        return result

    if isinstance(f, Slot):
        (x1, y1), (x2, y2) = f.start, f.end
        length = math.hypot(x2 - x1, y2 - y1) + f.width  # slot2D length = overall
        angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
        return wp.cut(_top(t).center((x1 + x2) / 2, (y1 + y2) / 2)
                      .slot2D(length, f.width, angle).extrude(-_depth(f.depth, part)))

    if isinstance(f, Pocket):
        cutter = _top(t).center(*f.center).rect(*f.size).extrude(-f.depth)
        if f.corner_radius > 0:
            cutter = cutter.edges("|Z").fillet(f.corner_radius)
        return wp.cut(cutter)

    if isinstance(f, PolygonPocket):
        cutter = cq.Workplane("XY", origin=(0, 0, t - f.depth)).polyline(f.points).close() \
                   .extrude(f.depth + 1)
        if f.corner_radius > 0:
            cutter = cutter.edges("|Z").fillet(f.corner_radius)
        return wp.cut(cutter)

    raise ValueError(f"unknown cutting feature {f.type}")


def build(part: Part) -> cq.Workplane:
    wp = build_body(part.body)
    for f in part.features:
        if isinstance(f, Cavity):
            wp = apply_cavity(wp, part, f)
    for f in part.features:
        if isinstance(f, (Hole, CounterboreHole, CountersinkHole, Slot, Pocket, PolygonPocket)):
            wp = apply_cut(wp, f, part)
    for f in part.features:
        if isinstance(f, BottomDisc):
            disc = (cq.Workplane("XY", origin=(0, 0, -f.height)).center(*f.center)
                    .circle(f.diameter / 2).extrude(f.height + EPS))
            wp = wp.union(disc)
    return wp