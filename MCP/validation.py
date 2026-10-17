"""Deterministic checks. Every function returns plain dicts (machine-readable)."""
from __future__ import annotations

import math

import cadquery as cq
from pydantic import BaseModel, ValidationError
from shapely.geometry import LineString, Point as SPoint, box as sbox

import outline as O
from builder import (body_polygon, disc_height, footprint_bounds, hole_instances,
                     hull_circles, inner_wall_coordinate, pad_points, pad_polygon,
                     pad_z0, z_range, _along, _v)
from schema import (ALL_HOLES, PRISMATIC, AxisHole, Base, Boss, BottomDisc, Cavity,
                    Chamfer, CirclesHullBody, CounterboreHole, CountersinkHole, Fillet,
                    Hole, Pad, Part, Pocket, PolygonBody, PolygonPocket, RevolveBody,
                    Rib, ROUND_HOLES, SideHole, Slot, Tab, vdicts)

TOL = 0.05  # mm
META = {"source", "confidence", "review", "derivation", "description", "id", "type"}


def parse(part_json: dict) -> tuple[Part | None, list[dict]]:
    """Schema check. Returns (part, errors)."""
    try:
        return Part.model_validate(part_json), []
    except ValidationError as e:
        return None, [{"check": "schema", "where": ".".join(map(str, err["loc"])),
                       "problem": err["msg"]} for err in e.errors()]


def _cavity(part: Part) -> Cavity | None:
    return next((f for f in part.features if isinstance(f, Cavity)), None)


def _by_id(part: Part, fid: str):
    return next((f for f in part.features if f.id == fid), None)


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
    if isinstance(f, Pad):
        return [pad_polygon(f)]
    return None


def _outline_problems(part: Part) -> list[str]:
    b = part.body
    if isinstance(b, PolygonBody):
        return O.check(vdicts(b.points, b.corner_radius))
    if isinstance(b, RevolveBody):
        return O.check(vdicts(b.points))
    if isinstance(b, CirclesHullBody):
        try:
            O.arc_segments_from_circles(hull_circles(b))
        except ValueError as e:
            return [str(e)]
        hull = body_polygon(b)
        return [f"circle {i} (Ø{c.diameter} at {c.center}) does not touch the hull; it adds nothing (use a pad?)"
                for i, c in enumerate(b.circles)
                if hull.exterior.distance(SPoint(c.center)) > c.diameter / 2 + TOL]
    return []


def plausibility(part: Part) -> list[dict]:
    """Engineering sanity checks BEFORE building."""
    errs = []
    t = part.top
    zmin, zmax = z_range(part)
    cav = _cavity(part)

    def err(check, where, problem):
        errs.append({"check": check, "where": where, "problem": problem})

    for p in _outline_problems(part):
        err("outline", "body", p)
    if any(e["check"] == "outline" for e in errs):
        return errs  # nothing else can be checked without a valid outline
    outline = body_polygon(part.body)
    if not outline.is_valid:
        err("outline", "body", "Outline intersects itself (check vertex order and radii)")
        return errs
    if isinstance(part.body, RevolveBody):
        from shapely.geometry import Polygon
        prof = O.polygon(O.segments(vdicts(part.body.points)))
        if not prof.is_valid or prof.area <= 0:
            err("outline", "body", "Revolve profile intersects itself")

    ids = [f.id for f in part.features]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        err("ids", dup, "Duplicate feature id")

    cavities = [f for f in part.features if isinstance(f, Cavity)]
    if len(cavities) > 1:
        err("cavity", "features", "Only one cavity is supported")

    inner = None
    if cav:
        if not isinstance(part.body, PRISMATIC):
            err("cavity", cav.id, "A cavity needs a box/polygon/circles_hull body")
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
                err("island", f.id, f"{f.type} needs a cavity feature (for a standing boss use a pad)")
                continue
            if f.top_z is not None and not (t - cav.depth < f.top_z <= t + TOL):
                err("island", f.id, f"top_z {f.top_z} must be between cavity floor {t - cav.depth} and top {t}")
            if inner is not None and isinstance(f, (Boss, Rib)):
                if not inner.buffer(TOL).contains(_footprint(f, part)[0]):
                    err("inside_cavity", f.id, "Feature is not fully inside the cavity")
            if inner is not None and isinstance(f, Tab):
                try:
                    inner_wall_coordinate(part, cav, f)
                except ValueError:
                    err("tab", f.id, "Tab position does not hit the cavity wall")
                span = inner.bounds[2] - inner.bounds[0] if f.wall in ("left", "right") \
                    else inner.bounds[3] - inner.bounds[1]
                if f.length_from_inner_wall >= span:
                    err("tab", f.id, "Tab is longer than the cavity")

        # pads: must sit on material, bore and taper must fit
        if isinstance(f, Pad):
            z0 = pad_z0(f, part)
            fp = pad_polygon(f)
            if f.points is not None:
                for p in O.check(pad_points(f)):
                    err("pad", f.id, p)
            if not fp.is_valid:
                err("pad", f.id, "Pad outline intersects itself")
            if z0 < zmin - TOL or z0 > t + TOL:
                err("pad", f.id, f"z0 {z0} is not on the part (z {zmin}..{t}); the pad would float")
            elif isinstance(part.body, PRISMATIC) and fp.intersection(outline).area < 1e-3:
                err("pad", f.id, "Pad footprint does not touch the body outline; it would float")
            if f.taper_deg:
                shrink = f.height * math.tan(math.radians(f.taper_deg))
                if fp.buffer(-shrink).is_empty:
                    err("pad", f.id, f"Taper {f.taper_deg}° over height {f.height} closes the pad top")
                elif f.bore_diameter and f.shape == "circle" and f.diameter / 2 - shrink <= f.bore_diameter / 2:
                    err("pad", f.id, "Tapered pad top is smaller than the bore")
            if f.bore_depth not in (None, "through") and f.bore_depth > z0 + f.height - zmin + TOL:
                err("pad", f.id, f"Bore depth {f.bore_depth} goes below the part; use 'through'")

        # holes: depth, heads
        if isinstance(f, ALL_HOLES):
            hs = hole_instances(f, part)
            for h in hs:
                if isinstance(f, ROUND_HOLES):
                    ez = h["entry"][2]
                    if ez > zmax + TOL or ez < zmin - TOL:
                        err("hole", f.id, f"from_z {ez} is outside the part (z {zmin}..{zmax})")
                    if not h["through"] and h["depth"] > ez - zmin + TOL:
                        err("depth", f.id, f"Depth {h['depth']} from z={ez} goes below the part; use 'through'?")
            cb_d, cs_d = getattr(f, "cbore_diameter", None), getattr(f, "csk_diameter", None)
            if cb_d is not None and cb_d <= f.diameter:
                err("counterbore", f.id, f"Counterbore Ø{cb_d} must be larger than hole Ø{f.diameter}")
            if cb_d is not None and f.depth != "through" and f.cbore_depth >= f.depth:
                err("counterbore", f.id, f"Counterbore depth {f.cbore_depth} >= hole depth {f.depth}")
            if isinstance(f, CounterboreHole) and f.cbore_depth >= t:
                err("counterbore", f.id, f"Counterbore depth {f.cbore_depth} >= thickness {t}")
            if cs_d is not None and cs_d <= f.diameter:
                err("countersink", f.id, f"Countersink Ø{cs_d} must be larger than hole Ø{f.diameter}")
            if isinstance(f, SideHole):
                x0, y0, x1, y1 = footprint_bounds(part)
                ur, vr = {"front": ((x0, x1), (zmin, zmax)), "back": ((x0, x1), (zmin, zmax)),
                          "left": ((y0, y1), (zmin, zmax)), "right": ((y0, y1), (zmin, zmax)),
                          "bottom": ((x0, x1), (y0, y1))}[f.face]
                extent = {"front": y1 - y0, "back": y1 - y0, "left": x1 - x0, "right": x1 - x0,
                          "bottom": zmax - zmin}[f.face]
                r = max(f.diameter, cb_d or 0, cs_d or 0) / 2
                for (u, v) in f.positions:
                    if not (ur[0] + r - TOL <= u <= ur[1] - r + TOL and vr[0] + r - TOL <= v <= vr[1] - r + TOL):
                        err("side_hole", f.id, f"Position {(u, v)} with Ø{2 * r:g} is not inside the {f.face} face "
                                                f"(u {ur[0]:g}..{ur[1]:g}, v {vr[0]:g}..{vr[1]:g})")
                if f.depth != "through" and f.depth > extent + TOL:
                    err("depth", f.id, f"Depth {f.depth} > part extent {extent:g} from the {f.face} face; use 'through'?")
            if isinstance(f, AxisHole):
                x0, y0, x1, y1 = footprint_bounds(part)
                ex, ey, ez = f.entry
                if not (x0 - TOL <= ex <= x1 + TOL and y0 - TOL <= ey <= y1 + TOL and zmin - TOL <= ez <= zmax + TOL):
                    err("axis_hole", f.id, f"Entry {f.entry} is outside the part bounding box")

        # straight cuts: depth
        if isinstance(f, (Slot, Pocket, PolygonPocket)) and isinstance(getattr(f, "depth", None), (int, float)) \
                and f.depth > t - zmin + TOL:
            err("depth", f.id, f"Depth {f.depth} > total thickness; use 'through'?")
        if isinstance(f, Pocket) and f.corner_radius * 2 > min(f.size):
            err("pocket", f.id, "Corner radius too large for pocket size")
        if isinstance(f, PolygonPocket):
            for p in O.check(vdicts(f.points, f.corner_radius)):
                err("pocket", f.id, p)

        # inside the part outline (top-view features only)
        if isinstance(f, ROUND_HOLES + (Slot, Pocket, BottomDisc)):
            allowed = outline.union(*[pad_polygon(p) for p in part.features if isinstance(p, Pad)]) \
                if any(isinstance(p, Pad) for p in part.features) else outline
            for shp in _footprint(f, part):
                if not allowed.buffer(TOL).contains(shp):
                    err("inside_part", f.id, "Feature extends outside the part outline")

        # fillets / chamfers
        if isinstance(f, (Fillet, Chamfer)):
            errs.extend(_edge_op_checks(f, part, outline))
    return errs


def _edge_op_checks(f, part: Part, outline) -> list[dict]:
    out = []
    size = f.radius if isinstance(f, Fillet) else f.size
    t = part.top

    def err(problem):
        out.append({"check": f.type, "where": f.id, "problem": problem})

    if f.edges in ("top_outline", "bottom_outline", "vertical_corners"):
        if not isinstance(part.body, PRISMATIC):
            err("Outline edges need a prismatic body; put fillets/chamfers into the revolve profile (vertex r/c)")
            return out
        if f.edges != "vertical_corners" and size >= t - TOL:
            err(f"{size} must be smaller than the plate thickness {t}")
        if f.edges == "vertical_corners":
            out += [{"check": f.type, "where": f.id,
                     "problem": "Plan-view corner radii: give r on the outline vertices instead (exact and checked)"}]
        cav = _cavity(part)
        if cav and f.edges == "top_outline" and size >= cav.wall_thickness - TOL:
            err(f"{size} must be smaller than the cavity wall {cav.wall_thickness}")
    for tid in f.targets:
        tf = _by_id(part, tid)
        if tf is None:
            err(f"target '{tid}' does not exist")
            continue
        if f.edges == "holes":
            if not isinstance(tf, ALL_HOLES):
                err(f"target '{tid}' is a {tf.type}, not a hole")
                continue
            if f.side in ("exit", "both") and tf.depth != "through":
                err(f"target '{tid}' is a blind hole; it has no exit edge")
            r_entry = max(tf.diameter, getattr(tf, "cbore_diameter", None) or 0,
                          getattr(tf, "csk_diameter", None) or 0) / 2
            if isinstance(f, Chamfer) and isinstance(tf, ROUND_HOLES):
                for p in tf.positions:
                    if not outline.buffer(-TOL).contains(SPoint(p).buffer(r_entry + size)):
                        err(f"Chamfer {size} on {tid} at {p} breaks into the outer edge")
            if tf.depth != "through" and size >= tf.depth:
                err(f"{size} must be smaller than the depth of {tid} ({tf.depth})")
            if isinstance(f, Fillet) and size >= r_entry * 2:
                err(f"Fillet R{size} is too large for Ø{2 * r_entry:g}")
        else:
            if not isinstance(tf, Pad):
                err(f"target '{tid}' is a {tf.type}, not a pad")
                continue
            if size >= tf.height - TOL and f.edges == "pad_top":
                err(f"{size} must be smaller than the pad height {tf.height}")
            fp = pad_polygon(tf)
            minw = min(fp.bounds[2] - fp.bounds[0], fp.bounds[3] - fp.bounds[1])
            if f.edges == "pad_top" and size * 2 >= minw:
                err(f"{size} is too large for the pad width {minw:g}")
            if tf.bore_diameter and f.edges == "pad_top" and tf.shape == "circle" and \
                    size >= (tf.diameter - tf.bore_diameter) / 2:
                err(f"{size} is larger than the pad wall {(tf.diameter - tf.bore_diameter) / 2:g}")
    return out


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
    for f in part.features:
        th = getattr(f, "thread", None)
        if th is not None and isinstance(f, ALL_HOLES):
            info = th.info
            if "diameter" in f.model_fields_set and abs(f.diameter - info["tap_drill"]) > TOL:
                out.append({"check": "thread", "where": f.id,
                            "problem": f"Ø{f.diameter} differs from the ISO tap drill Ø{info['tap_drill']} "
                                       f"for {info['designation']}"})
        if isinstance(f, Pad) and isinstance(part.body, PRISMATIC) and \
                not body_polygon(part.body).buffer(TOL).contains(pad_polygon(f)):
            out.append({"check": "pad_overhang", "where": f.id,
                        "problem": "Pad sticks out past the body outline; check that this is intended"})
    return out


def not_modelled(part: Part) -> list[dict]:
    """Parameters that were read but are not in the geometry (cosmetic or not supported)."""
    out = []
    for f in part.features:
        if isinstance(f, Cavity):
            if f.draft_deg:
                out.append({"item": f.id, "reason": f"Draft {f.draft_deg}° not modelled (walls vertical)"})
            if f.floor_fillet:
                out.append({"item": f.id, "reason": f"Floor fillet R{f.floor_fillet} not modelled"})
        if isinstance(f, Rib) and f.end_slope_deg:
            out.append({"item": f.id, "reason": f"Rib end slope {f.end_slope_deg}° not modelled (vertical ends)"})
        th = getattr(f, "thread", None)
        if th is not None:
            info = th.info
            length = th.depth if th.depth is not None else \
                (getattr(f, "depth", None) if not isinstance(f, Pad) else f.height)
            kind = "external" if isinstance(f, Pad) else "internal"
            geo = f"plain cylinder Ø{info['nominal']:g}" if isinstance(f, Pad) else f"core hole Ø{f.diameter:g}"
            out.append({"item": f.id, "reason": f"Cosmetic {kind} thread {info['designation']} "
                                                f"(pitch {info['pitch']:g}, length {length}"
                                                f"{', ' + th.tolerance if th.tolerance else ''}); modelled as {geo}",
                        "thread": {"designation": info["designation"], "nominal": info["nominal"],
                                   "pitch": info["pitch"], "tap_drill": info["tap_drill"],
                                   "length": length, "internal": kind == "internal",
                                   "tolerance": th.tolerance}})
    return [dict(o, kind="not_modelled") for o in out]


def defaulted(part: Part) -> list[dict]:
    """Fields the JSON left out that got a default value (never silent)."""
    out = []

    def walk(obj: BaseModel, owner: str, path: str):
        cls = type(obj)
        flag = set(getattr(cls, "FLAG_IF_UNSET", ()))
        for name, info in cls.model_fields.items():
            if name in META:
                continue
            val = getattr(obj, name)
            if name not in obj.model_fields_set:
                d = info.default
                if name in flag or (d is not None and d not in ([], 0, False, "") and not callable(d)):
                    shown = val if val is not None else "(convention, see schema)"
                    if name == "z0" or name == "top_z" or name == "from_z":
                        shown = "plate top"
                    if name == "depth" and isinstance(obj, Base) and type(obj).__name__ == "ThreadSpec":
                        shown = "full length"
                    out.append({"item": f"{owner}.{path}{name}", "value": shown,
                                "reason": f"'{name}' not given, default used", "kind": "defaulted"})
            elif isinstance(val, Base):
                walk(val, owner, f"{path}{name}.")

    for obj in [part.body, *part.features]:
        walk(obj, obj.id, "")
    return out


def review_list(part: Part, build_notes: list | None = None, threshold: float = 0.8) -> list[dict]:
    items = [{"item": a.item, "value": a.value, "reason": a.reason, "kind": "assumption"}
             for a in part.assumptions]
    items += [{"item": w["where"], "reason": w["problem"], "kind": "warning"} for w in warnings(part)]
    items += not_modelled(part)
    items += defaulted(part)
    items += [n for n in (build_notes or []) if n["kind"] == "not_modelled"]
    for obj in [part.body, *part.features]:
        if obj.review:
            items.append({"item": obj.id, "reason": obj.review, "kind": "flagged"})
        elif obj.confidence < threshold:
            items.append({"item": obj.id, "reason": f"Low confidence {obj.confidence}",
                          "kind": "low_confidence"})
        if obj.derivation and isinstance(obj, ALL_HOLES) and obj.thread is not None \
                and "diameter" not in obj.model_fields_set:
            items.append({"item": obj.id, "value": obj.diameter, "reason": obj.derivation.strip(),
                          "kind": "derived"})
    return items


# ---------- after the build ----------
def _cylinders(wp: cq.Workplane) -> list[dict]:
    """Every cylindrical face with a vertical axis: diameter and axis position."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    out = []
    for f in wp.faces("%CYLINDER").vals():
        cyl = BRepAdaptor_Surface(f.wrapped).Cylinder()
        ax = cyl.Axis()
        d, loc = ax.Direction(), ax.Location()
        if abs(abs(d.Z()) - 1) < 1e-6:
            out.append({"d": round(cyl.Radius() * 2, 3), "x": round(loc.X(), 3), "y": round(loc.Y(), 3)})
    return out


def _found(cyl: list[dict], d: float, x: float, y: float) -> bool:
    return any(abs(c["d"] - d) < TOL and abs(c["x"] - x) < TOL and abs(c["y"] - y) < TOL
               for c in cyl)


def _perp(d):
    """Two unit vectors perpendicular to d."""
    a = (1, 0, 0) if abs(d[0]) < 0.9 else (0, 1, 0)
    u = (d[1] * a[2] - d[2] * a[1], d[2] * a[0] - d[0] * a[2], d[0] * a[1] - d[1] * a[0])
    n = math.sqrt(sum(c * c for c in u))
    u = tuple(c / n for c in u)
    w = (d[1] * u[2] - d[2] * u[1], d[2] * u[0] - d[0] * u[2], d[0] * u[1] - d[1] * u[0])
    return u, w


def classifier(solid):
    """Fast repeated point-in-solid test (one OCC classifier per solid)."""
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_IN, TopAbs_ON
    clf = BRepClass3d_SolidClassifier(solid.wrapped)

    def inside(p):
        clf.Perform(gp_Pnt(*p), 1e-6)
        return clf.State() in (TopAbs_IN, TopAbs_ON)
    return inside


def probe_hole(inside, f, h, in_uncut) -> dict:
    """Probe the built solid along and around a hole axis.
    depth: air on a ring at 0.8 r wherever the uncut part had material, from the
    entry to the bottom; material just below a blind hole.
    diameter: air just inside the wall (r - 0.1), material just outside (r + 0.2)."""
    e, d = h["entry"], h["dir"]
    r = f.diameter / 2
    head = getattr(f, "cbore_depth", None) or 0
    if getattr(f, "csk_diameter", None):
        head = (f.csk_diameter - f.diameter) / 2 / math.tan(math.radians(f.csk_angle / 2))
    length = h["depth"]
    u, w = _perp(d)
    dirs = (u, w, tuple(-x for x in u), tuple(-x for x in w))
    off = lambda p, m, k: tuple(a + b * k for a, b in zip(p, m))  # noqa: E731
    n = 30
    air_ok, mats = True, []
    for k in range(1, n):
        s = length * k / n
        c = _along(e, d, s)
        for m in dirs:
            p = off(c, m, 0.8 * r)
            if in_uncut(p):
                mats.append(s)
                if inside(p):
                    air_ok = False
    bottom_ok = True
    if not h["through"]:
        p = _along(e, d, length + 0.3)
        bottom_ok = (not in_uncut(p)) or inside(p)
    body = sorted({s for s in mats if s > head + 0.3})
    diameter_ok = None
    if body:
        c = _along(e, d, body[len(body) // 2])
        wall_air = not any(inside(off(c, m, r - 0.1)) for m in dirs)
        wall_mat = any(inside(off(c, m, r + 0.2)) for m in dirs)
        diameter_ok = wall_air and wall_mat
    return {"feature": f.id, "type": f.type, "position": list(h["pos"]),
            "expected": {"d": round(f.diameter, 3), "depth": "through" if h["through"] else length},
            "depth_ok": air_ok and bottom_ok and bool(mats),
            "diameter_ok": bool(diameter_ok)}


def check_model(wp: cq.Workplane, part: Part, build_notes: list | None = None, uncut=None) -> dict:
    """Compare the built solid against the spec."""
    solid = wp.val()
    bb = solid.BoundingBox()
    x0, y0, x1, y1 = footprint_bounds(part)
    zmin, zmax = z_range(part)
    expected = [x1 - x0, y1 - y0, zmax - zmin]
    size = [round(bb.xlen, 3), round(bb.ylen, 3), round(bb.zlen, 3)]
    cyl = _cylinders(wp)
    if uncut is None:
        uncut = getattr(wp, "uncut", None)
    if uncut is None:
        from builder import build_uncut
        uncut = build_uncut(part).val()
    inside, in_uncut = classifier(solid), classifier(uncut)

    holes = []
    for f in part.features:
        if isinstance(f, ALL_HOLES):
            for h in hole_instances(f, part):
                res = probe_hole(inside, f, h, in_uncut)
                if isinstance(f, ROUND_HOLES):
                    res["cylinder_face_found"] = _found(cyl, f.diameter, *h["pos"])
                holes.append(res)

    round_features = []
    for f in part.features:
        if isinstance(f, (Boss, BottomDisc)) or (isinstance(f, Pad) and f.shape == "circle" and not f.taper_deg):
            round_features.append({"feature": f.id, "type": f.type,
                                   "expected": {"d": f.diameter, "x": f.center[0], "y": f.center[1]},
                                   "found": _found(cyl, f.diameter, *f.center)})

    edge_ops = [n for n in (build_notes or []) if n["item"] in
                {f.id for f in part.features if isinstance(f, (Fillet, Chamfer))}]
    checks = {
        "valid_solid": solid.isValid(),
        "single_solid": len(wp.solids().vals()) == 1,
        "size_ok": all(abs(a - b) < TOL for a, b in zip(size, expected)),
        "z_range_ok": abs(bb.zmin - zmin) < TOL and abs(bb.zmax - zmax) < TOL,
        "holes_depth_ok": all(h["depth_ok"] for h in holes),
        "holes_diameter_ok": all(h["diameter_ok"] and h.get("cylinder_face_found", True) for h in holes),
        "round_features_found": all(r["found"] for r in round_features),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "size_xyz": size,
        "expected_size_xyz": [round(v, 3) for v in expected],
        "z_range": [round(bb.zmin, 3), round(bb.zmax, 3)],
        "volume": round(solid.Volume(), 2),
        "holes": holes,
        "round_features": round_features,
        "edge_operations": edge_ops,
    }


def check_step(path: str) -> dict:
    try:
        re = cq.importers.importStep(path)
        return {"reimport_ok": re.val().isValid(), "solids": len(re.solids().vals())}
    except Exception as e:  # report, never crash
        return {"reimport_ok": False, "error": str(e)}
