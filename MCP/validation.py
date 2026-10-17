"""Deterministic checks. Every function returns plain dicts (machine-readable)."""
from __future__ import annotations

import cadquery as cq
from pydantic import ValidationError
from shapely.geometry import Point as SPoint, Polygon

from schema import (BoxBody, CounterboreHole, CountersinkHole, Hole, Part,
                    Pocket, PolygonBody, Slot)

TOL = 0.05  # mm


def parse(part_json: dict) -> tuple[Part | None, list[dict]]:
    """Schema check. Returns (part, errors)."""
    try:
        return Part.model_validate(part_json), []
    except ValidationError as e:
        return None, [{"check": "schema", "where": ".".join(map(str, err["loc"])),
                       "problem": err["msg"]} for err in e.errors()]


def _outline(body) -> Polygon:
    if isinstance(body, BoxBody):
        return Polygon([(0, 0), (body.length, 0), (body.length, body.width), (0, body.width)])
    return Polygon(body.points)


def plausibility(part: Part) -> list[dict]:
    """Engineering sanity checks BEFORE building."""
    errs = []
    t = part.body.thickness
    outline = _outline(part.body)
    if not outline.is_valid:
        errs.append({"check": "outline", "where": "body",
                     "problem": "Outline polygon intersects itself"})
    ids = [f.id for f in part.features]
    for dup in {i for i in ids if ids.count(i) > 1}:
        errs.append({"check": "ids", "where": dup, "problem": "Duplicate feature id"})

    for f in part.features:
        # depth vs thickness
        d = getattr(f, "depth", None)
        if isinstance(d, (int, float)) and d > t + TOL:
            errs.append({"check": "depth", "where": f.id,
                         "problem": f"Depth {d} > part thickness {t}; use 'through'?"})
        # diameters
        if isinstance(f, CounterboreHole):
            if f.cbore_diameter <= f.diameter:
                errs.append({"check": "counterbore", "where": f.id,
                             "problem": f"Counterbore Ø{f.cbore_diameter} must be larger than hole Ø{f.diameter}"})
            if f.cbore_depth >= t:
                errs.append({"check": "counterbore", "where": f.id,
                             "problem": f"Counterbore depth {f.cbore_depth} >= thickness {t}"})
        if isinstance(f, CountersinkHole) and f.csk_diameter <= f.diameter:
            errs.append({"check": "countersink", "where": f.id,
                         "problem": f"Countersink Ø{f.csk_diameter} must be larger than hole Ø{f.diameter}"})
        # inside the part (with full diameter / size)
        shapes = []
        if isinstance(f, (Hole, CounterboreHole, CountersinkHole)):
            r = max(f.diameter, getattr(f, "cbore_diameter", 0), getattr(f, "csk_diameter", 0)) / 2
            shapes = [(p, SPoint(p).buffer(r)) for p in f.positions]
        elif isinstance(f, Slot):
            from shapely.geometry import LineString
            shapes = [((f.start, f.end), LineString([f.start, f.end]).buffer(f.width / 2))]
        elif isinstance(f, Pocket):
            (cx, cy), (sx, sy) = f.center, f.size
            shapes = [(f.center, Polygon([(cx - sx/2, cy - sy/2), (cx + sx/2, cy - sy/2),
                                          (cx + sx/2, cy + sy/2), (cx - sx/2, cy + sy/2)]))]
            if f.corner_radius * 2 > min(sx, sy):
                errs.append({"check": "pocket", "where": f.id,
                             "problem": "Corner radius too large for pocket size"})
        for where, shp in shapes:
            if not outline.buffer(TOL).contains(shp):
                errs.append({"check": "inside_part", "where": f"{f.id} at {where}",
                             "problem": "Feature extends outside the part outline"})
    return errs


def _footprints(f) -> list:
    from shapely.geometry import LineString
    if isinstance(f, (Hole, CounterboreHole, CountersinkHole)):
        return [SPoint(p).buffer(f.diameter / 2) for p in f.positions]
    if isinstance(f, Slot):
        return [LineString([f.start, f.end]).buffer(f.width / 2)]
    if isinstance(f, Pocket):
        (cx, cy), (sx, sy) = f.center, f.size
        return [Polygon([(cx - sx/2, cy - sy/2), (cx + sx/2, cy - sy/2),
                         (cx + sx/2, cy + sy/2), (cx - sx/2, cy + sy/2)])]
    return []


def warnings(part: Part) -> list[dict]:
    """Not errors, but unusual geometry a human should look at:
    features that partly overlap (e.g. a hole cutting through a pocket wall)."""
    out = []
    feats = part.features
    for i, a in enumerate(feats):
        for b in feats[i + 1:]:
            for sa in _footprints(a):
                for sb in _footprints(b):
                    inter = sa.intersection(sb).area
                    item = {"check": "partial_overlap", "where": f"{a.id} / {b.id}",
                            "problem": "Features partly overlap; check positions"}
                    if 1e-3 < inter < min(sa.area, sb.area) - 1e-3 and item not in out:
                        out.append(item)
    return out


def review_list(part: Part, threshold: float = 0.8) -> list[dict]:
    items = [{"item": a.item, "value": a.value, "reason": a.reason, "kind": "assumption"}
             for a in part.assumptions]
    items += [{"item": w["where"], "reason": w["problem"], "kind": "warning"}
              for w in warnings(part)]
    for obj in [part.body, *part.features]:
        oid = getattr(obj, "id", "body")
        if obj.review:
            items.append({"item": oid, "reason": obj.review, "kind": "flagged"})
        elif obj.confidence < threshold:
            items.append({"item": oid, "reason": f"Low confidence {obj.confidence}",
                          "kind": "low_confidence"})
    return items


def _cylinders(wp: cq.Workplane) -> list[dict]:
    out = []
    for f in wp.faces("%CYLINDER").vals():
        circ = next((e for e in f.Edges() if e.geomType() == "CIRCLE"), None)
        if circ is None:
            continue
        c = circ.arcCenter()  # true circle centre (Center() is wrong for partial arcs)
        out.append({"d": round(circ.radius() * 2, 3), "x": round(c.x, 3), "y": round(c.y, 3)})
    return out


def check_model(wp: cq.Workplane, part: Part) -> dict:
    """Compare the built solid against the spec."""
    solid = wp.val()
    bb = solid.BoundingBox()
    outline = _outline(part.body)
    minx, miny, maxx, maxy = outline.bounds
    expected_size = [maxx - minx, maxy - miny, part.body.thickness]
    size = [round(bb.xlen, 3), round(bb.ylen, 3), round(bb.zlen, 3)]
    cyl = _cylinders(wp)

    holes = []
    for f in part.features:
        if not isinstance(f, (Hole, CounterboreHole, CountersinkHole)):
            continue
        for (x, y) in f.positions:
            found = any(abs(c["d"] - f.diameter) < TOL and abs(c["x"] - x) < TOL
                        and abs(c["y"] - y) < TOL for c in cyl)
            holes.append({"feature": f.id, "expected": {"d": f.diameter, "x": x, "y": y},
                          "found": found})

    checks = {
        "valid_solid": solid.isValid(),
        "single_solid": len(wp.solids().vals()) == 1,
        "size_ok": all(abs(a - b) < TOL for a, b in zip(size, expected_size)),
        "all_holes_found": all(h["found"] for h in holes),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "size_xyz": size,
        "expected_size_xyz": [round(v, 3) for v in expected_size],
        "volume": round(solid.Volume(), 2),
        "holes": holes,
    }


def check_step(path: str) -> dict:
    try:
        re = cq.importers.importStep(path)
        return {"reimport_ok": re.val().isValid(), "solids": len(re.solids().vals())}
    except Exception as e:  # report, never crash
        return {"reimport_ok": False, "error": str(e)}
