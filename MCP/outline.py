"""2D outlines with per-vertex corner radius / chamfer.

One routine turns a vertex list into exact segments (lines + arcs). The same
segments give the CadQuery wire (exact) and a dense shapely polygon (checks).
Used for polygon bodies, pads, pockets, circle hulls and revolve profiles.
"""
from __future__ import annotations

import math

import cadquery as cq
from shapely.geometry import Polygon

EPS_LEN = 1e-6


def _unit(v):
    n = math.hypot(*v)
    return (v[0] / n, v[1] / n)


def corner_cut(verts, i) -> float:
    """Distance from vertex i to where its rounding starts on each edge."""
    v = verts[i]
    r, c = v.get("r") or 0, v.get("c") or 0
    if not r and not c:
        return 0.0
    if c:
        return c
    p, n = verts[i - 1], verts[(i + 1) % len(verts)]
    u1 = _unit((p["x"] - v["x"], p["y"] - v["y"]))
    u2 = _unit((n["x"] - v["x"], n["y"] - v["y"]))
    cos_t = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
    theta = math.acos(cos_t)
    if theta < 1e-3 or theta > math.pi - 1e-3:
        raise ValueError(f"vertex {i}: corner is straight or a spike, cannot round it")
    return r / math.tan(theta / 2)


def check(verts) -> list[str]:
    """Problems with the rounding (radius too large for the adjacent edges...)."""
    out = []
    n = len(verts)
    cuts = []
    for i in range(n):
        if verts[i].get("r") and verts[i].get("c"):
            out.append(f"vertex {i}: give either r (radius) or c (chamfer), not both")
        try:
            cuts.append(corner_cut(verts, i))
        except ValueError as e:
            out.append(str(e))
            cuts.append(0.0)
    for i in range(n):
        a, b = verts[i], verts[(i + 1) % n]
        L = math.hypot(b["x"] - a["x"], b["y"] - a["y"])
        if L < EPS_LEN:
            out.append(f"vertices {i} and {(i + 1) % n} are identical")
        elif cuts[i] + cuts[(i + 1) % n] > L + 1e-6:
            out.append(f"edge {i}->{(i + 1) % n} (length {L:.3f}) is too short for the corner "
                       f"rounding at its ends ({cuts[i]:.3f} + {cuts[(i + 1) % n]:.3f}); "
                       f"reduce r/c")
    return out


def segments(verts):
    """Exact closed path: list of ('line', a, b) and ('arc', a, mid, b)."""
    n = len(verts)
    corners = []  # per vertex: (entry point, mid or None, exit point)
    for i, v in enumerate(verts):
        V = (v["x"], v["y"])
        d = corner_cut(verts, i)
        if d == 0:
            corners.append((V, None, V))
            continue
        p, nx = verts[i - 1], verts[(i + 1) % n]
        u1 = _unit((p["x"] - V[0], p["y"] - V[1]))
        u2 = _unit((nx["x"] - V[0], nx["y"] - V[1]))
        t1 = (V[0] + u1[0] * d, V[1] + u1[1] * d)
        t2 = (V[0] + u2[0] * d, V[1] + u2[1] * d)
        if v.get("c"):
            corners.append((t1, None, t2))
            continue
        r = v["r"]
        theta = math.acos(max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1])))
        b = _unit((u1[0] + u2[0], u1[1] + u2[1]))
        dc = r / math.sin(theta / 2)
        mid = (V[0] + b[0] * (dc - r), V[1] + b[1] * (dc - r))
        corners.append((t1, mid, t2))

    segs = []
    for i in range(n):
        t1, mid, t2 = corners[i]
        prev_exit = corners[i - 1][2]
        if math.dist(prev_exit, t1) > EPS_LEN:
            segs.append(("line", prev_exit, t1))
        if mid is not None:
            segs.append(("arc", t1, mid, t2))
        elif math.dist(t1, t2) > EPS_LEN:
            segs.append(("line", t1, t2))
    return segs


def arc_segments_from_circles(circles):
    """Convex hull of circles (dicts with x, y, r) as exact segments, CCW."""
    from shapely.geometry import Point
    from shapely.ops import unary_union
    hull = unary_union([Point(c["x"], c["y"]).buffer(c["r"], 256) for c in circles]).convex_hull
    ring = list(hull.exterior.coords)[:-1]
    if Polygon(ring).exterior.is_ccw is False:
        ring.reverse()
    # which circle owns each hull point -> order of circles along the hull
    order = []
    for p in ring:
        k = min(range(len(circles)), key=lambda j: abs(math.dist(p, (circles[j]["x"], circles[j]["y"])) - circles[j]["r"]))
        if not order or order[-1] != k:
            order.append(k)
    while len(order) > 1 and order[0] == order[-1]:
        order.pop()
    if len(order) < 2:
        raise ValueError("One circle encloses all others; use that circle alone")

    def tangent(a, b):
        """Outer tangent from circle a to b, material on the left (CCW)."""
        dx, dy = b["x"] - a["x"], b["y"] - a["y"]
        D = math.hypot(dx, dy)
        k = (a["r"] - b["r"]) / D
        if abs(k) >= 1:
            raise ValueError("Circles are nested")
        ang = math.atan2(dy, dx)
        phi = ang - math.acos(k)   # normal pointing outward (right side of travel)
        nx, ny = math.cos(phi), math.sin(phi)
        return ((a["x"] + a["r"] * nx, a["y"] + a["r"] * ny),
                (b["x"] + b["r"] * nx, b["y"] + b["r"] * ny))

    tans = [tangent(circles[order[i]], circles[order[(i + 1) % len(order)]]) for i in range(len(order))]
    segs = []
    for i in range(len(order)):
        c = circles[order[(i + 1) % len(order)]]
        start_t, end_t = tans[i]
        segs.append(("line", start_t, end_t))
        nxt = tans[(i + 1) % len(order)][0]
        a0 = math.atan2(end_t[1] - c["y"], end_t[0] - c["x"])
        a1 = math.atan2(nxt[1] - c["y"], nxt[0] - c["x"])
        while a1 < a0 - 1e-9:
            a1 += 2 * math.pi
        if a1 - a0 < 1e-6:   # tangent lines meet on the circle: no arc
            continue
        am = (a0 + a1) / 2
        segs.append(("arc", end_t, (c["x"] + c["r"] * math.cos(am), c["y"] + c["r"] * math.sin(am)), nxt))
    return segs, order


def draw(wp: cq.Workplane, segs) -> cq.Workplane:
    """Draw the closed path on a workplane (local coordinates)."""
    wp = wp.moveTo(*segs[0][1])
    for s in segs:
        wp = wp.lineTo(*s[2]) if s[0] == "line" else wp.threePointArc(s[2], s[3])
    return wp.close()


def _arc_points(a, m, b, n=24):
    # circle through three points
    ax, ay = a
    bx, by = m
    cx, cy = b
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [a, b]
    ux = ((ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay) + (cx**2 + cy**2) * (ay - by)) / d
    uy = ((ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx) + (cx**2 + cy**2) * (bx - ax)) / d
    r = math.dist((ux, uy), a)
    t0, tm, t1 = (math.atan2(p[1] - uy, p[0] - ux) for p in (a, m, b))
    # go from t0 to t1 passing tm
    def norm(t):
        return t % (2 * math.pi)
    ccw_span = norm(t1 - t0)
    ccw = norm(tm - t0) < ccw_span
    span = ccw_span if ccw else -(2 * math.pi - ccw_span)
    return [(ux + r * math.cos(t0 + span * k / n), uy + r * math.sin(t0 + span * k / n)) for k in range(n + 1)]


def polygon(segs) -> Polygon:
    """Dense shapely polygon of the path (for inside/overlap checks)."""
    pts = []
    for s in segs:
        pts.extend([s[1]] if s[0] == "line" else _arc_points(s[1], s[2], s[3])[:-1])
    return Polygon(pts)
