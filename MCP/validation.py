"""Deterministic checks. Every function returns plain dicts (machine-readable)."""
from __future__ import annotations

import cadquery as cq
from pydantic import ValidationError
from shapely.geometry import LineString, Point as SPoint, Polygon, box as sbox

from builder import body_polygon, disc_height, inner_wall_coordinate
from schema import (Boss, BottomDisc, Cavity, CounterboreHole, CountersinkHole,
                    Hole, Part, Pocket, PolygonPocket, Rib, Slot, Tab)

TOL = 0.05  # mm
ROUND_HOLES = (Hole, CounterboreHole, CountersinkHole)


def parse(part_json: dict) -> tuple[Part | None, list[dict]]:
    """Schema check. Returns (part, errors)."""
    try:
        return Part.model_validate(part_json), []
    except ValidationError as e:
        return None, [{"check": "schema", "where": ".".join(map(str, err["loc"])),
                       "problem": err["msg"]} for err in e.errors()]


def _cavity(part: Part) -> Cavity | None:
    return next((f for f in part.features if isinstance(f, Cavity)), None)


def _footprint(f, part: Part):
    """2D shape of a feature in the top view (None if not applicable)."""
    if isinstance(f, ROUND_HOLES):
        r = max(f.diameter, getattr(f, "cbore_diameter", 0), getattr(f, "csk_diameter", 0)) / 2
        return [SPoint(p).buffer(r) for p in f.positions]
    if isinstance(f, Slot):
        return [LineString([f.start, f.end]).buffer(f.width / 2)]
    if isinstance(f, Pocket):
        (cx, cy), (sx, sy) = f.center, f.size
        return [sbox(cx - sx / 2, cy - sy / 2, cx + sx / 2, cy + sy / 2)]
    if isinstance(f, (Boss, BottomDisc)):
        return [SPoint(f.center).buffer(f.diameter / 2)]
    if isinstance(f, Rib):
        (cx, cy) = f.center
        sx, sy = (f.length, f.width) if f.direction == "x" else (f.width, f.length)
        return [sbox(cx - sx / 2, cy - sy / 2, cx + sx / 2, cy + sy / 2)]
    return None


def plausibility(part: Part) -> list[dict]:
    """Engineering sanity checks BEFORE building."""
    errs = []
    t = part.body.thickness
    outline = body_polygon(part.body)
    cav = _cavity(part)

    def err(check, where, problem):
        errs.append({"check": check, "where": where, "problem": problem})

    if not outline.is_valid:
        err("outline", "body", "Outline polygon intersects itself")

    ids = [f.id for f in part.features]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        err("ids", dup, "Duplicate feature id")

    cavities = [f for f in part.features if isinstance(f, Cavity)]
    if len(cavities) > 1:
        err("cavity", "features", "Only one cavity is supported")

    inner = None
    if cav:
        if cav.depth >= t - TOL:
            err("cavity", cav.id, f"Cavity depth {cav.depth} must be less than thickness {t}")
        if cav.floor_thickness is not None and abs(cav.floor_thickness - (t - cav.depth)) > TOL:
            err("cavity", cav.id, f"floor_thickness {cav.floor_thickness} != thickness - depth = {t - cav.depth}")
        inner = outline.buffer(-cav.wall_thickness, join_style=2)
        if inner.is_empty or inner.geom_type != "Polygon":
            err("cavity", cav.id, "Wall thickness too large: cavity disappears or splits")
            inner = None
        for ref in cav.islands:
            if ref not in ids:
                err("cavity", cav.id, f"Island '{ref}' does not exist")

    for f in part.features:
        # islands need a cavity and must sit inside it
        if isinstance(f, (Boss, Rib, Tab)):
            if not cav:
                err("island", f.id, f"{f.type} needs a cavity feature")
                continue
            if f.top_z is not None and not (t - cav.depth < f.top_z <= t + TOL):
                err("island", f.id, f"top_z {f.top_z} must be between cavity floor {t - cav.depth} and top {t}")
            if inner is not None and isinstance(f, (Boss, Rib)):
                if not inner.buffer(TOL).contains(_footprint(f, part)[0]):
                    err("inside_cavity", f.id, "Feature is not fully inside the cavity")
            if inner is not None and isinstance(f, Tab):
                try:
                    inner_wall_coordinate(part, cav, f)
                except Exception:
                    err("tab", f.id, "Tab position does not hit the cavity wall")
                span = inner.bounds[2] - inner.bounds[0] if f.wall in ("left", "right") \
                    else inner.bounds[3] - inner.bounds[1]
                if f.length_from_inner_wall >= span:
                    err("tab", f.id, "Tab is longer than the cavity")

        # depths
        d = getattr(f, "depth", None)
        if isinstance(f, ROUND_HOLES + (Slot, Pocket, PolygonPocket)) and isinstance(d, (int, float)) \
                and d > t + disc_height(part) + TOL:
            err("depth", f.id, f"Depth {d} > total thickness; use 'through'?")
        if isinstance(f, CounterboreHole):
            if f.cbore_diameter <= f.diameter:
                err("counterbore", f.id, f"Counterbore Ø{f.cbore_diameter} must be larger than hole Ø{f.diameter}")
            if f.cbore_depth >= t:
                err("counterbore", f.id, f"Counterbore depth {f.cbore_depth} >= thickness {t}")
        if isinstance(f, CountersinkHole) and f.csk_diameter <= f.diameter:
            err("countersink", f.id, f"Countersink Ø{f.csk_diameter} must be larger than hole Ø{f.diameter}")
        if isinstance(f, Pocket) and f.corner_radius * 2 > min(f.size):
            err("pocket", f.id, "Corner radius too large for pocket size")

        # inside the part outline
        if isinstance(f, ROUND_HOLES + (Slot, Pocket, BottomDisc)):
            for shp in _footprint(f, part):
                if not outline.buffer(TOL).contains(shp):
                    err("inside_part", f.id, "Feature extends outside the part outline")
    return errs


def warnings(part: Part) -> list[dict]:
    """Not errors, but geometry a human should look at."""
    out = []
    cuts = [f for f in part.features if isinstance(f, ROUND_HOLES + (Slot, Pocket))]
    for i, a in enumerate(cuts):
        for b in cuts[i + 1:]:
            for sa in _footprint(a, part):
                for sb in _footprint(b, part):
                    inter = sa.intersection(sb).area
                    item = {"check": "partial_overlap", "where": f"{a.id} / {b.id}",
                            "problem": "Features partly overlap; check positions"}
                    if 1e-3 < inter < min(sa.area, sb.area) - 1e-3 and item not in out:
                        out.append(item)
    return out


def not_modelled(part: Part) -> list[dict]:
    """Parameters that were read but are not in the geometry yet."""
    out = []
    for f in part.features:
        if isinstance(f, Cavity):
            if f.draft_deg:
                out.append({"item": f.id, "reason": f"Draft {f.draft_deg}° not modelled (walls vertical)"})
            if f.floor_fillet:
                out.append({"item": f.id, "reason": f"Floor fillet R{f.floor_fillet} not modelled"})
        if isinstance(f, Rib) and f.end_slope_deg:
            out.append({"item": f.id, "reason": f"Rib end slope {f.end_slope_deg}° not modelled (vertical ends)"})
        if isinstance(f, Hole) and f.thread:
            out.append({"item": f.id, "reason": f"Thread {f.thread} not modelled (core hole only)"})
    return [dict(o, kind="not_modelled") for o in out]


def review_list(part: Part, threshold: float = 0.8) -> list[dict]:
    items = [{"item": a.item, "value": a.value, "reason": a.reason, "kind": "assumption"}
             for a in part.assumptions]
    items += [{"item": w["where"], "reason": w["problem"], "kind": "warning"} for w in warnings(part)]
    items += not_modelled(part)
    for obj in [part.body, *part.features]:
        if obj.review:
            items.append({"item": obj.id, "reason": obj.review, "kind": "flagged"})
        elif obj.confidence < threshold:
            items.append({"item": obj.id, "reason": f"Low confidence {obj.confidence}",
                          "kind": "low_confidence"})
    return items


def _cylinders(wp: cq.Workplane) -> list[dict]:
    out = []
    for f in wp.faces("%CYLINDER").vals():
        circ = next((e for e in f.Edges() if e.geomType() == "CIRCLE"), None)
        if circ is not None:
            c = circ.arcCenter()  # true circle centre (Center() is wrong for partial arcs)
            out.append({"d": round(circ.radius() * 2, 3), "x": round(c.x, 3), "y": round(c.y, 3)})
    return out


def _found(cyl: list[dict], d: float, x: float, y: float) -> bool:
    return any(abs(c["d"] - d) < TOL and abs(c["x"] - x) < TOL and abs(c["y"] - y) < TOL
               for c in cyl)


def check_model(wp: cq.Workplane, part: Part) -> dict:
    """Compare the built solid against the spec."""
    solid = wp.val()
    bb = solid.BoundingBox()
    minx, miny, maxx, maxy = body_polygon(part.body).bounds
    t = part.body.thickness
    tops = [f.top_z for f in part.features if getattr(f, "top_z", None) is not None]
    z_top = max([t, *tops])
    expected = [maxx - minx, maxy - miny, z_top + disc_height(part)]
    size = [round(bb.xlen, 3), round(bb.ylen, 3), round(bb.zlen, 3)]
    cyl = _cylinders(wp)

    round_features = []
    for f in part.features:
        if isinstance(f, ROUND_HOLES):
            pts, d = f.positions, f.diameter
        elif isinstance(f, (Boss, BottomDisc)):
            pts, d = [f.center], f.diameter
        else:
            continue
        for (x, y) in pts:
            round_features.append({"feature": f.id, "type": f.type,
                                   "expected": {"d": d, "x": x, "y": y},
                                   "found": _found(cyl, d, x, y)})

    checks = {
        "valid_solid": solid.isValid(),
        "single_solid": len(wp.solids().vals()) == 1,
        "size_ok": all(abs(a - b) < TOL for a, b in zip(size, expected)),
        "all_round_features_found": all(r["found"] for r in round_features),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "size_xyz": size,
        "expected_size_xyz": [round(v, 3) for v in expected],
        "z_range": [round(bb.zmin, 3), round(bb.zmax, 3)],
        "volume": round(solid.Volume(), 2),
        "round_features": round_features,
    }


def check_step(path: str) -> dict:
    try:
        re = cq.importers.importStep(path)
        return {"reimport_ok": re.val().isValid(), "solids": len(re.solids().vals())}
    except Exception as e:  # report, never crash
        return {"reimport_ok": False, "error": str(e)}