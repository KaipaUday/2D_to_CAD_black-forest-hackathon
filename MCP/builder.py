"""Deterministic geometry: Part -> CadQuery solid. No AI, no MCP."""
from __future__ import annotations

import math

import cadquery as cq

from schema import (BoxBody, CounterboreHole, CountersinkHole, Hole, Part,
                    Pocket, PolygonBody, Slot)


def _depth(d, thickness: float) -> float:
    # a little extra so "through" cuts never leave a skin
    return thickness + 1.0 if d == "through" else float(d)


def _top(part: cq.Workplane, thickness: float) -> cq.Workplane:
    """Workplane on the top face with the global origin (lower-left corner)."""
    return cq.Workplane("XY", origin=(0, 0, thickness))


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


def apply_feature(wp: cq.Workplane, f, t: float) -> cq.Workplane:
    solid = wp.val()
    if isinstance(f, Hole):
        cutter = (_top(wp, t).pushPoints(f.positions)
                  .circle(f.diameter / 2).extrude(-_depth(f.depth, t)))
        return wp.cut(cutter)

    if isinstance(f, CounterboreHole):
        hole = (_top(wp, t).pushPoints(f.positions)
                .circle(f.diameter / 2).extrude(-_depth(f.depth, t)))
        cbore = (_top(wp, t).pushPoints(f.positions)
                 .circle(f.cbore_diameter / 2).extrude(-f.cbore_depth))
        return wp.cut(hole).cut(cbore)

    if isinstance(f, CountersinkHole):
        hole = (_top(wp, t).pushPoints(f.positions)
                .circle(f.diameter / 2).extrude(-_depth(f.depth, t)))
        r_top = f.csk_diameter / 2
        h = (r_top - f.diameter / 2) / math.tan(math.radians(f.csk_angle / 2))
        result = wp.cut(hole)
        for (x, y) in f.positions:
            cone = cq.Solid.makeCone(f.diameter / 2, r_top, h,
                                     pnt=cq.Vector(x, y, t - h),
                                     dir=cq.Vector(0, 0, 1))
            result = result.cut(cq.Workplane().add(cone))
        return result

    if isinstance(f, Slot):
        (x1, y1), (x2, y2) = f.start, f.end
        length = math.hypot(x2 - x1, y2 - y1) + f.width  # slot2D length is overall
        angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
        cutter = (_top(wp, t).center((x1 + x2) / 2, (y1 + y2) / 2)
                  .slot2D(length, f.width, angle).extrude(-_depth(f.depth, t)))
        return wp.cut(cutter)

    if isinstance(f, Pocket):
        sk = _top(wp, t).center(*f.center).rect(*f.size)
        cutter = sk.extrude(-f.depth)
        if f.corner_radius > 0:
            cutter = cutter.edges("|Z").fillet(f.corner_radius)
        return wp.cut(cutter)

    raise ValueError(f"unknown feature type {f.type}")


def build(part: Part) -> cq.Workplane:
    wp = build_body(part.body)
    for f in part.features:
        wp = apply_feature(wp, f, part.body.thickness)
    return wp
