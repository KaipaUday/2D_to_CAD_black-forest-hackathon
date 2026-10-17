"""Deterministic geometry: Part -> CadQuery solid. No AI, no MCP.

Build order: body -> cavity with islands -> pads -> bottom discs ->
holes/slots/pockets -> hole chamfers -> edge fillets/chamfers.
Bottom discs and pads come before the cuts so through holes go through them.
"""
from __future__ import annotations

import math

import cadquery as cq
from shapely.geometry import LineString, Point as SPoint, Polygon, box as sbox

import outline as O
from schema import (ALL_HOLES, AxisHole, Boss, BottomDisc, BoxBody, Cavity, Chamfer,
                    CirclesHullBody, CounterboreHole, CountersinkHole, Fillet, Hole,
                    Pad, Part, Pocket, PolygonBody, PolygonPocket, RevolveBody, Rib,
                    SideHole, Slot, Tab, vdicts)

EPS = 0.01
CUTS = (Hole, CounterboreHole, CountersinkHole, SideHole, AxisHole, Slot, Pocket, PolygonPocket)


# ---------- outlines ----------
def body_segments(body):
    """Exact top-view outline of a prismatic body as line/arc segments."""
    if isinstance(body, BoxBody):
        L, W = body.length, body.width
        r = body.corner_radius or None
        return O.segments([{"x": 0, "y": 0, "r": r}, {"x": L, "y": 0, "r": r},
                           {"x": L, "y": W, "r": r}, {"x": 0, "y": W, "r": r}])
    if isinstance(body, PolygonBody):
        return O.segments(vdicts(body.points, body.corner_radius))
    if isinstance(body, CirclesHullBody):
        return O.arc_segments_from_circles(hull_circles(body))[0]
    raise ValueError(f"{body.type} has no top-view outline")


def hull_circles(body: CirclesHullBody) -> list[dict]:
    return [{"x": c.center[0], "y": c.center[1], "r": c.diameter / 2} for c in body.circles]


def body_polygon(body) -> Polygon:
    """Top-view footprint (dense polygon) used by the checks."""
    if isinstance(body, RevolveBody):
        R, L = body.radius, body.length
        return SPoint(R, R).buffer(R, 128) if body.axis == "z" else sbox(0, 0, L, 2 * R)
    return O.polygon(body_segments(body))


def pad_points(f: Pad) -> list[dict]:
    if f.shape == "rect":
        (cx, cy), (sx, sy) = f.center, f.size
        r = f.corner_radius or None
        return [{"x": cx - sx / 2, "y": cy - sy / 2, "r": r}, {"x": cx + sx / 2, "y": cy - sy / 2, "r": r},
                {"x": cx + sx / 2, "y": cy + sy / 2, "r": r}, {"x": cx - sx / 2, "y": cy + sy / 2, "r": r}]
    return vdicts(f.points, f.corner_radius)


def pad_polygon(f: Pad) -> Polygon:
    if f.shape == "circle":
        return SPoint(f.center).buffer(f.diameter / 2, 128)
    return O.polygon(O.segments(pad_points(f)))


def pad_z0(f: Pad, part: Part) -> float:
    return part.top if f.z0 is None else f.z0


def disc_height(part: Part) -> float:
    return max([f.height for f in part.features if isinstance(f, BottomDisc)], default=0.0)


def z_range(part: Part) -> tuple[float, float]:
    """Lowest and highest z of the uncut part."""
    tops = [part.top]
    tops += [pad_z0(f, part) + f.height for f in part.features if isinstance(f, Pad)]
    tops += [f.top_z for f in part.features if isinstance(f, (Boss, Rib, Tab)) and f.top_z is not None]
    return -disc_height(part), max(tops)


def footprint_bounds(part: Part) -> tuple[float, float, float, float]:
    """Top-view bounds of body + pads + bottom discs."""
    shapes = [body_polygon(part.body)]
    shapes += [pad_polygon(f) for f in part.features if isinstance(f, Pad)]
    shapes += [SPoint(f.center).buffer(f.diameter / 2) for f in part.features if isinstance(f, BottomDisc)]
    b = [s.bounds for s in shapes]
    return min(x[0] for x in b), min(x[1] for x in b), max(x[2] for x in b), max(x[3] for x in b)


def _top(z: float) -> cq.Workplane:
    """Workplane at height z, global XY origin (lower-left corner)."""
    return cq.Workplane("XY", origin=(0, 0, z))


# ---------- body ----------
def build_body(body) -> cq.Workplane:
    if isinstance(body, RevolveBody):
        R = body.radius
        pts = vdicts(body.points)
        if body.axis == "z":   # local (radius, axial) in XZ, revolve about global Z
            pts = [dict(p, x=p["y"], y=p["x"]) for p in pts]
            wp = O.draw(cq.Workplane("XZ"), O.segments(pts)).revolve(360, (0, 0, 0), (0, 1, 0))
            return wp.translate((R, R, 0))
        wp = O.draw(cq.Workplane("XZ"), O.segments(pts)).revolve(360, (0, 0, 0), (1, 0, 0))
        return wp.translate((0, R, R))
    return O.draw(cq.Workplane("XY"), body_segments(body)).extrude(body.thickness)


# ---------- cavity + islands ----------
def inner_wall_coordinate(part: Part, cav: Cavity, tab: Tab) -> float:
    """Where the inner wall face is, at the tab's position."""
    inner = body_polygon(part.body).buffer(-cav.wall_thickness, join_style=2)
    if tab.wall in ("top", "bottom"):
        line = LineString([(tab.center_x, -1e4), (tab.center_x, 1e4)])
    else:
        line = LineString([(-1e4, tab.center_y), (1e4, tab.center_y)])
    hit = inner.intersection(line)
    if hit.is_empty:  # shapely 2 returns NaN bounds instead of raising
        raise ValueError(f"tab {tab.id} does not hit the cavity wall")
    b = hit.bounds  # (minx, miny, maxx, maxy)
    return {"top": b[3], "bottom": b[1], "right": b[2], "left": b[0]}[tab.wall]


def island_solid(f, part: Part, cav: Cavity, floor_z: float) -> cq.Workplane:
    t = part.top
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
    t = part.top
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


# ---------- pads ----------
def pad_solid(f: Pad, part: Part) -> cq.Workplane:
    z0 = pad_z0(f, part)
    base = cq.Workplane("XY", origin=(0, 0, z0 - EPS))
    h = f.height + EPS
    if f.shape == "circle":
        s = base.center(*f.center).circle(f.diameter / 2).extrude(h, taper=f.taper_deg or None)
    else:
        s = O.draw(base, O.segments(pad_points(f))).extrude(h, taper=f.taper_deg or None)
    return s


def apply_pad_bore(wp: cq.Workplane, f: Pad, part: Part) -> cq.Workplane:
    top = pad_z0(f, part) + f.height
    zmin, _ = z_range(part)
    depth = top - zmin + 1.0 if f.bore_depth == "through" else f.bore_depth
    cx, cy = f.center
    cyl = cq.Solid.makeCylinder(f.bore_diameter / 2, depth + 1.0, cq.Vector(cx, cy, top + 1.0),
                                cq.Vector(0, 0, -1))
    return wp.cut(cq.Workplane().add(cyl))


# ---------- holes (any direction) ----------
def hole_instances(f, part: Part) -> list[dict]:
    """Every drilled hole as entry point + unit direction + depth (mm, or None = through).
    The entry point lies on the entry surface."""
    zmin, zmax = z_range(part)
    x0, y0, x1, y1 = footprint_bounds(part)
    out = []
    if isinstance(f, (Hole, CounterboreHole, CountersinkHole)):
        z = part.top if f.from_z is None else f.from_z
        for (x, y) in f.positions:
            out.append({"entry": (x, y, z), "dir": (0, 0, -1), "pos": (x, y)})
    elif isinstance(f, SideHole):
        for (u, v) in f.positions:
            e, d = {"front": ((u, y0, v), (0, 1, 0)), "back": ((u, y1, v), (0, -1, 0)),
                    "left": ((x0, u, v), (1, 0, 0)), "right": ((x1, u, v), (-1, 0, 0)),
                    "bottom": ((u, v, zmin), (0, 0, 1))}[f.face]
            out.append({"entry": e, "dir": d, "pos": (u, v)})
    elif isinstance(f, AxisHole):
        n = math.sqrt(sum(c * c for c in f.direction))
        out.append({"entry": tuple(f.entry), "dir": tuple(c / n for c in f.direction), "pos": tuple(f.entry)})
    span = math.dist((x0, y0, zmin), (x1, y1, zmax)) + 2.0
    for h in out:
        h["through"] = f.depth == "through"
        h["depth"] = span if f.depth == "through" else float(f.depth)
    return out


def _v(p):
    return cq.Vector(*p)


def _along(p, d, s):
    return (p[0] + d[0] * s, p[1] + d[1] * s, p[2] + d[2] * s)


def hole_tool(f, h) -> cq.Solid:
    """Cutting tool for one hole instance: core + optional counterbore/countersink."""
    e, d = h["entry"], h["dir"]
    r = f.diameter / 2
    back = 1.0 if not h["through"] else h["depth"]   # through: also extend behind the entry
    start = _along(e, d, -back)
    tool = cq.Solid.makeCylinder(r, h["depth"] + back, _v(start), _v(d))
    cb_d = getattr(f, "cbore_diameter", None)
    if cb_d:
        cb = cq.Solid.makeCylinder(cb_d / 2, f.cbore_depth + 1.0, _v(_along(e, d, -1.0)), _v(d))
        tool = tool.fuse(cb)
    cs_d = getattr(f, "csk_diameter", None)
    if cs_d:
        ang = f.csk_angle
        R = cs_d / 2
        hgt = (R - r) / math.tan(math.radians(ang / 2))
        # cone from Ø csk (slightly above the surface) down to Ø core, no coplanar faces
        top_r = R + EPS * math.tan(math.radians(ang / 2))
        cone = cq.Solid.makeCone(top_r, r - EPS, hgt + 2 * EPS, _v(_along(e, d, -EPS)), _v(d))
        tool = tool.fuse(cone)
    return tool


def chamfer_cone(center, d, r, size) -> cq.Solid:
    """45° hole-edge chamfer: cone from r+size at the surface to r at depth size."""
    return cq.Solid.makeCone(r + size + EPS, r - EPS, size + 2 * EPS, _v(_along(center, d, -EPS)), _v(d))


def entry_radius(f) -> float:
    return max(f.diameter, getattr(f, "cbore_diameter", None) or 0, getattr(f, "csk_diameter", None) or 0) / 2


# ---------- other cuts ----------
def apply_cut(wp: cq.Workplane, f, part: Part) -> cq.Workplane:
    t = part.top
    zmin, zmax = z_range(part)
    through = zmax - zmin + 2.0
    if isinstance(f, ALL_HOLES):
        for h in hole_instances(f, part):
            wp = wp.cut(cq.Workplane().add(hole_tool(f, h)))
        return wp

    if isinstance(f, Slot):
        (x1, y1), (x2, y2) = f.start, f.end
        length = math.hypot(x2 - x1, y2 - y1) + f.width  # slot2D length = overall
        angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
        if f.depth == "through":
            return wp.cut(_top(zmax + 1).center((x1 + x2) / 2, (y1 + y2) / 2)
                          .slot2D(length, f.width, angle).extrude(-through - 1))
        return wp.cut(_top(t).center((x1 + x2) / 2, (y1 + y2) / 2)
                      .slot2D(length, f.width, angle).extrude(-f.depth))

    if isinstance(f, Pocket):
        cutter = _top(t).center(*f.center).rect(*f.size).extrude(-f.depth)
        if f.corner_radius > 0:
            cutter = cutter.edges("|Z").fillet(f.corner_radius)
        return wp.cut(cutter)

    if isinstance(f, PolygonPocket):
        segs = O.segments(vdicts(f.points, f.corner_radius))
        cutter = O.draw(cq.Workplane("XY", origin=(0, 0, t - f.depth)), segs).extrude(f.depth + 1)
        return wp.cut(cutter)

    raise ValueError(f"unknown cutting feature {f.type}")


# ---------- edge selection for fillets / chamfers ----------
def _edge_pts(e):
    return [e.positionAt(s) for s in (0.0, 0.25, 0.5, 0.75, 1.0)]


def select_edges(solid, f, part: Part, tol: float = 0.02) -> list:
    """Edges of the built solid for a fillet/chamfer feature (geometric filter)."""
    edges = solid.Edges()
    if f.edges in ("top_outline", "bottom_outline", "vertical_corners"):
        if not isinstance(part.body, (BoxBody, PolygonBody, CirclesHullBody)):
            raise ValueError("outline edges only exist on prismatic bodies; put the radius in the revolve profile")
        ring = body_polygon(part.body).exterior
        z = part.top if f.edges == "top_outline" else 0.0
        out = []
        for e in edges:
            pts = _edge_pts(e)
            on_ring = all(ring.distance(SPoint(p.x, p.y)) < max(tol, 0.05) for p in pts)
            if not on_ring:
                continue
            if f.edges == "vertical_corners":
                if abs(pts[0].x - pts[-1].x) < tol and abs(pts[0].y - pts[-1].y) < tol and abs(pts[0].z - pts[-1].z) > tol:
                    out.append(e)
            elif all(abs(p.z - z) < tol for p in pts):
                out.append(e)
        return out
    if f.edges in ("pad_top", "pad_root"):
        out = []
        for pid in f.targets:
            pad = next(p for p in part.features if getattr(p, "id", None) == pid)
            z0 = pad_z0(pad, part)
            z = z0 + pad.height if f.edges == "pad_top" else z0
            ring = pad_polygon(pad).exterior
            if f.edges == "pad_top" and pad.taper_deg:
                ring = pad_polygon(pad).buffer(-pad.height * math.tan(math.radians(pad.taper_deg))).exterior
            # a root edge that lies on the body outline is the outer wall, not a root
            outer = body_polygon(part.body).exterior if f.edges == "pad_root" and \
                isinstance(part.body, (BoxBody, PolygonBody, CirclesHullBody)) else None
            for e in edges:
                pts = _edge_pts(e)
                if all(abs(p.z - z) < tol and ring.distance(SPoint(p.x, p.y)) < 0.05 for p in pts):
                    if outer is not None and all(outer.distance(SPoint(p.x, p.y)) < 0.05 for p in pts):
                        continue
                    out.append(e)
        return out
    raise ValueError(f"edge set {f.edges} is handled elsewhere")


def hole_rim_edges(solid, part: Part, f, exits: dict, tol: float = 0.02) -> list:
    """Circular rim edges of the target holes at entry / exit."""
    out = []
    for hid in f.targets:
        hf = next(p for p in part.features if getattr(p, "id", None) == hid)
        for i, h in enumerate(hole_instances(hf, part)):
            h["exit_s"] = exits.get(hid, [None] * (i + 1))[i]
            ends = []
            if f.side in ("entry", "both"):
                ends.append((h["entry"], entry_radius(hf)))
            if f.side in ("exit", "both"):
                s = h.get("exit_s")
                if s is not None:
                    ends.append((_along(h["entry"], h["dir"], s), hf.diameter / 2))
            for (c, r) in ends:
                d = h["dir"]
                for e in solid.Edges():
                    if e.geomType() != "CIRCLE":
                        continue
                    pts = _edge_pts(e)
                    if all(abs(sum((p.toTuple()[i] - c[i]) * d[i] for i in range(3))) < tol and
                           abs(math.dist(p.toTuple(), c) - r) < tol for p in pts):
                        out.append(e)
    return out


def apply_hole_chamfer(wp: cq.Workplane, f: Chamfer, part: Part, exits: dict, notes: list) -> cq.Workplane:
    for hid in f.targets:
        hf = next(p for p in part.features if getattr(p, "id", None) == hid)
        for i, h in enumerate(hole_instances(hf, part)):
            h["exit_s"] = exits.get(hid, [None] * (i + 1))[i]
            d = h["dir"]
            if f.side in ("entry", "both"):
                wp = wp.cut(cq.Workplane().add(chamfer_cone(h["entry"], d, entry_radius(hf), f.size)))
            if f.side in ("exit", "both"):
                s = h.get("exit_s")
                if s is None:
                    notes.append({"item": f.id, "kind": "not_modelled",
                                  "reason": f"Exit chamfer on {hid} at {h['pos']}: hole has no exit (blind)"})
                    continue
                c = _along(h["entry"], d, s)
                back = tuple(-x for x in d)
                wp = wp.cut(cq.Workplane().add(chamfer_cone(c, back, hf.diameter / 2, f.size)))
    return wp


def _exit_s(solid, h) -> float | None:
    """Axial distance from the entry to where a through hole leaves the part."""
    if not h["through"]:
        return None
    e, d = h["entry"], h["dir"]
    from validation import classifier
    inside = classifier(solid)
    # probe the axis of the UNCUT solid: last point still in material
    best = None
    n = 300
    step = h["depth"] / n
    for k in range(n + 1):
        if inside(_along(e, d, k * step - 1.0)):
            best = k * step - 1.0
    if best is None:
        return None
    lo, hi = best, best + step   # refine the surface crossing
    for _ in range(30):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if inside(_along(e, d, mid)) else (lo, mid)
    return round(lo, 4)


def apply_edge_op(wp: cq.Workplane, f, part: Part, exits: dict, notes: list) -> cq.Workplane:
    solid = wp.val()
    what = f"R{f.radius}" if isinstance(f, Fillet) else f"{f.size}x45°"
    label = f"{'Fillet' if isinstance(f, Fillet) else 'Chamfer'} {what} on {f.edges}" + \
        (f" {f.targets}" if f.targets else "")
    def attempt(solid, op):
        edges = hole_rim_edges(solid, part, op, exits) if op.edges == "holes" else select_edges(solid, op, part)
        if not edges:
            raise ValueError("no matching edges found")
        new = solid.fillet(op.radius, edges) if isinstance(op, Fillet) else solid.chamfer(op.size, None, edges)
        if not new.isValid():
            raise ValueError("result is not a valid solid")
        return new, len(edges)

    try:
        new, n = attempt(solid, f)
        notes.append({"item": f.id, "kind": "modelled", "reason": f"{label}: {n} edges",
                      "volume_change": round(new.Volume() - solid.Volume(), 3)})
        return cq.Workplane().add(new)
    except Exception as e:  # never crash on kernel failures: record and continue
        if len(f.targets) < 2:
            notes.append({"item": f.id, "kind": "not_modelled",
                          "reason": f"{label} not modelled, CadQuery failed: {e}"})
            return wp
    # several targets: keep the ones that work, report the rest
    for tid in f.targets:
        one = f.model_copy(update={"targets": [tid]})
        try:
            new, n = attempt(solid, one)
            notes.append({"item": f.id, "kind": "modelled", "reason": f"{label}: {tid} ({n} edges)",
                          "volume_change": round(new.Volume() - solid.Volume(), 3)})
            solid = new
        except Exception as e:
            notes.append({"item": f.id, "kind": "not_modelled",
                          "reason": f"{label}: {tid} not modelled, CadQuery failed: {e}"})
    return cq.Workplane().add(solid)


def build_uncut(part: Part) -> cq.Workplane:
    """Body + cavity + pads + bottom discs, before any hole or cut."""
    wp = build_body(part.body)
    for f in part.features:
        if isinstance(f, Cavity):
            wp = apply_cavity(wp, part, f)
    for f in part.features:
        if isinstance(f, Pad):
            wp = wp.union(pad_solid(f, part))
    for f in part.features:
        if isinstance(f, BottomDisc):
            disc = (cq.Workplane("XY", origin=(0, 0, -f.height)).center(*f.center)
                    .circle(f.diameter / 2).extrude(f.height + EPS))
            wp = wp.union(disc)
    return wp


def build(part: Part, notes: list | None = None) -> cq.Workplane:
    """Build the solid. Build notes (fillets applied / failed) go into `notes`."""
    notes = [] if notes is None else notes
    wp = build_uncut(part)
    uncut = wp.val()
    for f in part.features:
        if isinstance(f, Pad) and f.bore_diameter:
            wp = apply_pad_bore(wp, f, part)
    for f in part.features:
        if isinstance(f, CUTS):
            wp = apply_cut(wp, f, part)
    # exit points of through holes (needed for exit-side chamfers / fillets)
    exits = {}
    for f in part.features:
        if isinstance(f, (Fillet, Chamfer)) and f.edges == "holes" and f.side in ("exit", "both"):
            for hid in f.targets:
                hf = next(p for p in part.features if getattr(p, "id", None) == hid)
                exits[hid] = [_exit_s(uncut, h) for h in hole_instances(hf, part)]
    for f in part.features:
        if isinstance(f, Chamfer) and f.edges == "holes":
            wp = apply_hole_chamfer(wp, f, part, exits, notes)
            notes.append({"item": f.id, "kind": "modelled",
                          "reason": f"Hole chamfer {f.size}x45° ({f.side}) on {f.targets} as exact cones"})
    for f in part.features:
        if isinstance(f, Fillet) or (isinstance(f, Chamfer) and f.edges != "holes"):
            wp = apply_edge_op(wp, f, part, exits, notes)
    if len(wp.solids().vals()) == 1:
        wp = cq.Workplane().add(wp.solids().val().clean())
    wp.uncut = uncut   # kept for the post-build probes in validation.check_model
    return wp
