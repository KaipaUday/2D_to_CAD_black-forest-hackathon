#!/usr/bin/env python3
"""Convert vector PDF page(s) to DXF, and extract clean drawing entities.

Single file: python3 pdf2dxf.py drawing.pdf drawing.dxf
Folder:      python3 pdf2dxf.py ./pdfs ./dxfs

PDF page coordinates are converted from points to millimetres at 1:1 sheet
scale (sheet mm, origin lower-left of the page, y up). Used by the MCP tool
load_drawing through extract() / write_dxf().

What the extraction cleans up compared with a raw path dump:
- A closed ring of cubic Beziers with a square bbox becomes a CIRCLE.
- Every single Bezier that fits a circle becomes an ARC; arcs on the same
  centre/radius that cover 360° are merged into a CIRCLE.
- Lines are merged per line width; collinear runs of short pieces with gaps
  become one line flagged dashed (hidden line) or chain (centre line). CAD
  exporters often write dash patterns as separate segments, not as a PDF dash.
- Real text is read per line (get_text lines), not per word.
- Small filled triangles (arrowheads) are dropped; filled glyph paths
  (text converted to outlines) are collected as text regions.
"""

import argparse
import logging
import math
import sys
from collections import defaultdict
from pathlib import Path

import ezdxf
import pymupdf as fitz

logging.getLogger("ezdxf").setLevel(logging.WARNING)

PT_TO_MM = 25.4 / 72.0


# ---------- small geometry helpers ----------
def _bez(p, t):
    u = 1 - t
    return (u ** 3 * p[0][0] + 3 * u * u * t * p[1][0] + 3 * u * t * t * p[2][0] + t ** 3 * p[3][0],
            u ** 3 * p[0][1] + 3 * u * u * t * p[1][1] + 3 * u * t * t * p[2][1] + t ** 3 * p[3][1])


def _circumcenter(a, b, c):
    d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    if abs(d) < 1e-9:
        return None
    ux = ((a[0] ** 2 + a[1] ** 2) * (b[1] - c[1]) + (b[0] ** 2 + b[1] ** 2) * (c[1] - a[1])
          + (c[0] ** 2 + c[1] ** 2) * (a[1] - b[1])) / d
    uy = ((a[0] ** 2 + a[1] ** 2) * (c[0] - b[0]) + (b[0] ** 2 + b[1] ** 2) * (a[0] - c[0])
          + (c[0] ** 2 + c[1] ** 2) * (b[0] - a[0])) / d
    return ux, uy


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _ang(c, p):
    return math.degrees(math.atan2(p[1] - c[1], p[0] - c[0])) % 360


def _ccw_span(a0, a1):
    return (a1 - a0) % 360


def bezier_as_arc(p):
    """p = 4 control points (mm). Returns {'c','r','a0','a1'} (CCW a0->a1) if
    the cubic is a circular arc, else None."""
    p0, p3 = p[0], p[3]
    pm = _bez(p, 0.5)
    if _dist(p0, p3) < 1e-6:
        return None
    c = _circumcenter(p0, pm, p3)
    if c is None:
        return None
    r = _dist(c, p0)
    if r > 5000:
        return None
    tol = max(0.006 * r, 0.02)
    for t in (0.2, 0.35, 0.65, 0.8):
        if abs(_dist(c, _bez(p, t)) - r) > tol:
            return None
    a0, a1, am = _ang(c, p0), _ang(c, p3), _ang(c, pm)
    if _ccw_span(a0, am) > _ccw_span(a0, a1):  # pm not on the CCW way a0->a1
        a0, a1 = a1, a0
    return {"c": c, "r": r, "a0": a0, "a1": a1}


def _collinear_bezier(p):
    """Cubic whose control points are on the chord -> straight line."""
    a, b = p[0], p[3]
    L = _dist(a, b)
    if L < 1e-9:
        return False
    for q in (p[1], p[2]):
        if abs((b[0] - a[0]) * (q[1] - a[1]) - (b[1] - a[1]) * (q[0] - a[0])) / L > 0.01:
            return False
    return True


# ---------- extraction ----------
def extract(page, scale: float = 1.0) -> dict:
    """Read one PDF page. Returns sheet-mm entities:
    lines [{p1,p2,w,style}], circles [{c,r,w,style}], arcs [{c,r,a0,a1,w,style}],
    splines [{pts,w}], texts [{text,bbox,center,height,angle}],
    glyph_boxes [bbox] (outlined text), page_mm (w, h), visible_width."""
    h_pt = page.rect.height
    f = PT_TO_MM * scale

    def xy(pt):
        return (float(pt[0]) * f, (h_pt - float(pt[1])) * f)

    raw_lines, raw_arcs, circles, splines, glyph_boxes, arrows = [], [], [], [], [], []
    curve_count = defaultdict(int)
    length_by_w = defaultdict(float)
    for d in page.get_drawings():
        items = d.get("items", [])
        rect = d.get("rect")
        w = round(float(d.get("width") or 0), 2)
        kind = d.get("type", "s")
        if "s" not in kind:  # filled only: arrowheads, dots and outlined text
            bw, bh = rect.width * f, rect.height * f
            pts = _path_points(items, xy)
            arrow = _arrowhead(pts) if max(bw, bh) < 6 else None
            if arrow:
                arrows.append(arrow)
                continue
            if max(bw, bh) < 1.0:
                continue  # dot
            if max(bw, bh) < 12:
                glyph_boxes.append((rect.x0 * f, (h_pt - rect.y1) * f, rect.x1 * f, (h_pt - rect.y0) * f))
            continue
        # closed ring of cubic curves with a square bbox -> circle
        if len(items) >= 2 and all(it[0] == "c" for it in items):
            first, last = xy(items[0][1]), xy(items[-1][4])
            bw, bh = rect.width * f, rect.height * f
            if _dist(first, last) < 0.05 + 0.002 * max(bw, bh) and bw > 0 and abs(bw - bh) <= max(0.05, 0.01 * bw):
                c = ((rect.x0 + rect.x1) / 2 * f, (h_pt - (rect.y0 + rect.y1) / 2) * f)
                r = (bw + bh) / 4
                if all(abs(_dist(c, xy(it[1])) - r) <= max(0.02 * r, 0.03) for it in items):
                    circles.append({"c": c, "r": r, "w": w, "style": None})
                    curve_count[w] += len(items)
                    continue
        for it in items:
            k = it[0]
            if k == "l":
                a, b = xy(it[1]), xy(it[2])
                if _dist(a, b) > 1e-4:
                    raw_lines.append((a, b, w))
                    length_by_w[w] += _dist(a, b)
            elif k == "c":
                p = [xy(q) for q in it[1:5]]
                if _collinear_bezier(p):
                    raw_lines.append((p[0], p[3], w))
                    continue
                arc = bezier_as_arc(p)
                curve_count[w] += 1
                if arc:
                    arc["w"] = w
                    raw_arcs.append(arc)
                else:
                    splines.append({"pts": p, "w": w})
            elif k == "re":
                r = it[1]
                cs = [xy((r.x0, r.y0)), xy((r.x1, r.y0)), xy((r.x1, r.y1)), xy((r.x0, r.y1))]
                for i in range(4):
                    raw_lines.append((cs[i], cs[(i + 1) % 4], w))
            elif k == "qu":
                q = it[1]
                cs = [xy(q.ul), xy(q.ur), xy(q.lr), xy(q.ll)]
                for i in range(4):
                    raw_lines.append((cs[i], cs[(i + 1) % 4], w))

    # arrowheads drawn as a stroked triangle (+ fill strokes)
    raw_lines, drawn = _find_drawn_arrows(raw_lines)
    arrows += drawn
    # flattened curves: chains of short segments that lie on a circle
    raw_lines, chain_arcs = _fit_polyline_arcs(raw_lines)
    for a in chain_arcs:
        curve_count[a["w"]] += 1
    raw_arcs += chain_arcs

    # visible line width = the thickest width that draws a good share of the
    # curves (outlines, holes); frames and dimension lines draw few curves
    if curve_count:
        top = max(curve_count.values())
        vis_w = max(w for w, n in curve_count.items() if n >= 0.25 * top)
    elif length_by_w:
        vis_w = max(length_by_w, key=lambda k: (length_by_w[k], k))
    else:
        vis_w = 0
    thick = lambda w: w >= 0.7 * vis_w  # noqa: E731

    tagged = [{"p1": a, "p2": b, "w": w, "style": None} for a, b, w in raw_lines]
    _classify_dimension_lines(tagged, arrows)
    lines = _merge_lines([(t["p1"], t["p2"], t["w"], t["style"]) for t in tagged], thick)
    lines = [ln for ln in lines if _dist(ln["p1"], ln["p2"]) >= 0.2]  # curve leftovers
    _classify_hatch(lines)
    arcs, more_circles = _merge_arcs(raw_arcs, thick)
    for c in circles:
        c["style"] = "visible" if thick(c["w"]) else "thin"
    circles = _dedupe_circles(circles + more_circles)

    texts = []
    for block in page.get_text("dict").get("blocks", []):
        for ln in block.get("lines", []):
            s = "".join(sp.get("text", "") for sp in ln.get("spans", [])).strip()
            if not s:
                continue
            x0, y0, x1, y1 = ln["bbox"]
            dx, dy = ln.get("dir", (1, 0))
            size = max((sp.get("size", 0) for sp in ln.get("spans", [])), default=0)
            bb = (x0 * f, (h_pt - y1) * f, x1 * f, (h_pt - y0) * f)
            texts.append({"text": s, "bbox": bb, "center": ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2),
                          "height": round(size * f, 2), "angle": round(math.degrees(math.atan2(-dy, dx)) % 360, 1)})

    return {"page_mm": (page.rect.width * f, h_pt * f), "visible_width": vis_w,
            "lines": lines, "circles": circles, "arcs": arcs, "splines": splines,
            "texts": texts, "glyph_boxes": glyph_boxes, "arrows": arrows}


def _path_points(items, xy):
    pts = []
    for it in items:
        if it[0] == "l":
            pts += [xy(it[1]), xy(it[2])]
        elif it[0] == "c":
            pts += [xy(q) for q in it[1:5]]
        elif it[0] == "re":
            r = it[1]
            pts += [xy((r.x0, r.y0)), xy((r.x1, r.y1)), xy((r.x0, r.y1)), xy((r.x1, r.y0))]
        elif it[0] == "qu":
            q = it[1]
            pts += [xy(q.ul), xy(q.ur), xy(q.lr), xy(q.ll)]
    return pts


def _hull(pts):
    pts = sorted(set((round(x, 4), round(y, 4)) for x, y in pts))
    if len(pts) < 3:
        return pts
    cross = lambda o, a, b: (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])  # noqa: E731
    lo, hi = [], []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 1e-9:
            lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], p) <= 1e-9:
            hi.pop()
        hi.append(p)
    return lo[:-1] + hi[:-1]


def _arrowhead(pts):
    """Slim filled triangle -> {'tip', 'dir'} (dir points from base to tip)."""
    h = _hull(pts)
    # drop hull vertices that are nearly on a straight edge
    simple = []
    for i, p in enumerate(h):
        a, b = h[i - 1], h[(i + 1) % len(h)]
        L = _dist(a, b)
        if L > 1e-9 and abs((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])) / L > 0.05:
            simple.append(p)
    if len(simple) != 3:
        return None
    angs = []
    for i, p in enumerate(simple):
        a, b = simple[i - 1], simple[(i + 1) % 3]
        v1, v2 = (a[0] - p[0], a[1] - p[1]), (b[0] - p[0], b[1] - p[1])
        cosv = (v1[0] * v2[0] + v1[1] * v2[1]) / (math.hypot(*v1) * math.hypot(*v2) + 1e-12)
        angs.append(math.degrees(math.acos(max(-1, min(1, cosv)))))
    i = min(range(3), key=lambda k: angs[k])
    if angs[i] > 35:
        return None
    tip = simple[i]
    base = ((simple[i - 1][0] + simple[(i + 1) % 3][0]) / 2, (simple[i - 1][1] + simple[(i + 1) % 3][1]) / 2)
    L = _dist(tip, base)
    if L < 0.8:
        return None
    return {"tip": tip, "dir": ((tip[0] - base[0]) / L, (tip[1] - base[1]) / L), "len": L}


def _in_tri(p, t, tol=0.06):
    def side(a, b):
        L = _dist(a, b) or 1e-12
        return ((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])) / L
    s = [side(t[0], t[1]), side(t[1], t[2]), side(t[2], t[0])]
    return all(v >= -tol for v in s) or all(v <= tol for v in s)


def _find_drawn_arrows(raw):
    """Closed slim triangles made of 3 short segments are arrowheads; strokes
    inside them (solid fill drawn as lines) are dropped with them."""
    key = lambda p: (round(p[0], 2), round(p[1], 2))  # noqa: E731
    adj = defaultdict(list)
    for i, (a, b, w) in enumerate(raw):
        if 0.05 < _dist(a, b) < 6:
            adj[key(a)].append((key(b), i))
            adj[key(b)].append((key(a), i))
    arrows, drop, tris = [], set(), set()
    for i, (a, b, w) in enumerate(raw):
        ka, kb = key(a), key(b)
        if ka not in adj or i in drop:
            continue
        for kc, j in adj[kb]:
            if j == i or kc == ka:
                continue
            k = next((k for kd, k in adj[kc] if kd == ka and k not in (i, j)), None)
            if k is None:
                continue
            tri = tuple(sorted((ka, kb, kc)))
            if tri in tris:
                continue
            ar = _arrowhead([ka, kb, kc])
            if not ar:
                continue
            tris.add(tri)
            arrows.append(ar)
            drop.update((i, j, k))
            break
    if tris:
        for i, (a, b, w) in enumerate(raw):
            if i in drop or _dist(a, b) > 6:
                continue
            if any(_in_tri(a, t) and _in_tri(b, t) for t in tris
                   if abs(a[0] - t[0][0]) < 7 and abs(a[1] - t[0][1]) < 7):
                drop.add(i)
    return [r for i, r in enumerate(raw) if i not in drop], arrows


def _kasa(pts):
    """Least-squares circle through points -> (cx, cy, r, max residual)."""
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    suu = svv = suv = suuu = svvv = suvv = svuu = 0.0
    for x, y in pts:
        u, v = x - mx, y - my
        suu += u * u; svv += v * v; suv += u * v  # noqa: E702
        suuu += u ** 3; svvv += v ** 3; suvv += u * v * v; svuu += v * u * u  # noqa: E702
    det = suu * svv - suv * suv
    if abs(det) < 1e-12:
        return None
    b1, b2 = 0.5 * (suuu + suvv), 0.5 * (svvv + svuu)
    uc = (b1 * svv - b2 * suv) / det
    vc = (suu * b2 - suv * b1) / det
    cx, cy = uc + mx, vc + my
    r = math.sqrt(uc * uc + vc * vc + (suu + svv) / n)
    res = max(abs(_dist((cx, cy), p) - r) for p in pts)
    return cx, cy, r, res


def _fit_polyline_arcs(raw):
    """Find chains of short segments (flattened curves) and turn the parts that
    lie on a circle into arcs. Returns (remaining segments, arcs)."""
    key = lambda p: (round(p[0], 2), round(p[1], 2))  # noqa: E731
    short = defaultdict(list)
    rest = []
    for i, (a, b, w) in enumerate(raw):
        if _dist(a, b) < 4.0:
            short[w].append(i)
        else:
            rest.append(i)
    used, arcs = set(), []
    for w, idx in short.items():
        adj = defaultdict(list)
        for i in idx:
            a, b, _ = raw[i]
            adj[key(a)].append(i)
            adj[key(b)].append(i)
        seen = set()
        for start in idx:
            if start in seen:
                continue
            # walk to one end of the chain, then collect it in order
            chain = _walk_chain(start, raw, adj, key)
            seen.update(chain)
            if len(chain) < 4:
                continue
            pts = _chain_points(chain, raw, key)
            i0 = 0
            while i0 < len(pts) - 4:
                j = i0 + 4
                best = None
                while j <= len(pts):
                    fit = _kasa(pts[i0:j])
                    if not fit or fit[3] > max(0.004 * fit[2], 0.02) or fit[2] > 2000:
                        break
                    best = (j, fit)
                    j += 1
                if best is None:
                    i0 += 1
                    continue
                j, (cx, cy, r, _) = best
                seg_pts = pts[i0:j]
                a0, a1, am = _ang((cx, cy), seg_pts[0]), _ang((cx, cy), seg_pts[-1]), \
                    _ang((cx, cy), seg_pts[len(seg_pts) // 2])
                if _dist(seg_pts[0], seg_pts[-1]) < 0.02:  # closed ring
                    arcs.append({"c": (cx, cy), "r": r, "a0": 0.0, "a1": 359.999, "w": w})
                else:
                    if _ccw_span(a0, am) > _ccw_span(a0, a1):
                        a0, a1 = a1, a0
                    arcs.append({"c": (cx, cy), "r": r, "a0": a0, "a1": a1, "w": w})
                used.update(chain[i0:j - 1])
                i0 = j - 1
    remaining = [raw[i] for i in range(len(raw)) if i not in used]
    return remaining, arcs


def _walk_chain(start, raw, adj, key):
    def other_end(i, k):
        a, b, _ = raw[i]
        return key(b) if key(a) == k else key(a)

    # go backwards to an end
    cur, k = start, key(raw[start][0])
    visited = {start}
    while True:
        nxt = [i for i in adj[k] if i not in visited]
        if len(adj[k]) != 2 or not nxt:
            break
        cur = nxt[0]
        visited.add(cur)
        k = other_end(cur, k)
    # now walk forward from that end
    chain, seen = [cur], {cur}
    k = other_end(cur, k)
    while True:
        nxt = [i for i in adj[k] if i not in seen]
        if len(adj[k]) != 2 or not nxt:
            break
        cur = nxt[0]
        chain.append(cur)
        seen.add(cur)
        k = other_end(cur, k)
    return chain


def _chain_points(chain, raw, key):
    a, b, _ = raw[chain[0]]
    if len(chain) > 1:
        a2, b2, _ = raw[chain[1]]
        if key(a) in (key(a2), key(b2)):
            a, b = b, a
    pts = [a, b]
    for i in chain[1:]:
        p, q, _ = raw[i]
        pts.append(q if key(p) == key(pts[-1]) else p)
    return pts


def _classify_dimension_lines(lines, arrows, tol=0.35):
    """Lines that end at an arrow tip along the arrow axis are dimension lines;
    lines crossing an arrow tip across the axis are extension lines."""
    for ln in lines:
        a, b = ln["p1"], ln["p2"]
        L = _dist(a, b)
        if L < 1e-6 or L < 1.2:  # dash pieces are short; dimension lines are not
            continue
        u = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
        for ar in arrows:
            t = ar["tip"]
            s = (t[0] - a[0]) * u[0] + (t[1] - a[1]) * u[1]
            if s < -tol or s > L + tol:
                continue
            if abs((t[0] - a[0]) * u[1] - (t[1] - a[1]) * u[0]) > tol:
                continue
            along = abs(u[0] * ar["dir"][0] + u[1] * ar["dir"][1])
            if along > 0.9:
                ln["style"] = "dimension"
                break
            end = min(s, L - s)
            if along < 0.2 and -tol <= end < 5:  # extension lines overshoot the arrow tip a little
                ln["style"] = "extension"


def _classify_hatch(lines):
    """Many parallel thin/visible short lines at the same slanted angle -> hatch."""
    by_angle = defaultdict(list)
    for ln in lines:
        if ln["style"] not in ("thin", "visible"):
            continue
        a, b = ln["p1"], ln["p2"]
        if _dist(a, b) < 2:
            continue
        th = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180
        if min(th % 90, 90 - th % 90) < 5:
            continue
        by_angle[(ln["w"], round(th))].append(ln)
    for group in by_angle.values():
        if len(group) >= 6:
            for ln in group:
                ln["style"] = "hatch"


def _merge_lines(raw, thick):
    """Merge collinear pieces per width; flag dashed (hidden) / chain (centre)."""
    groups = defaultdict(list)
    for a, b, w, tag in raw:
        th = math.atan2(b[1] - a[1], b[0] - a[0]) % math.pi
        if th > math.pi - 1e-4:
            th = 0.0
        n = (-math.sin(th), math.cos(th))
        rho = a[0] * n[0] + a[1] * n[1]
        groups[(w, tag, round(math.degrees(th) / 0.25), round(rho / 0.05))].append((a, b, th))
    out = []
    for (w, tag, _, _), segs in groups.items():
        th = segs[0][2]
        u = (math.cos(th), math.sin(th))
        iv = sorted((min(a[0] * u[0] + a[1] * u[1], b[0] * u[0] + b[1] * u[1]),
                     max(a[0] * u[0] + a[1] * u[1], b[0] * u[0] + b[1] * u[1]), a, b) for a, b, _ in segs)
        n = (-u[1], u[0])
        rho = sum(a[0] * n[0] + a[1] * n[1] for a, b, _ in segs) / len(segs)
        runs, cur = [], [list(iv[0][:2])]
        for t0, t1, _, _ in iv[1:]:
            last = cur[-1]
            if t0 <= last[1] + 0.02:           # touching / overlapping: same piece
                last[1] = max(last[1], t1)
            elif t0 - last[1] <= 3.0:          # small gap: part of a dash pattern
                cur.append([t0, t1])
            else:
                runs.append(cur)
                cur = [[t0, t1]]
        runs.append(cur)
        for run in runs:
            pieces = [p1 - p0 for p0, p1 in run]
            style = tag or ("visible" if thick(w) else "thin")
            if not tag and len(run) >= 3 and sorted(pieces)[len(pieces) // 2] < 12:
                inner = pieces[1:-1] or pieces
                style = "center" if max(inner) > 2.5 * min(inner) else "hidden"
            elif len(run) == 2:  # two pieces with a small gap: keep them apart
                for p0, p1 in run:
                    out.append(_seg(u, n, rho, p0, p1, w, style))
                continue
            out.append(_seg(u, n, rho, run[0][0], run[-1][1], w, style))
    return out


def _seg(u, n, rho, t0, t1, w, style):
    p = lambda t: (u[0] * t + n[0] * rho, u[1] * t + n[1] * rho)  # noqa: E731
    return {"p1": p(t0), "p2": p(t1), "w": w, "style": style}


def _merge_arcs(raw, thick):
    """Group arcs by centre/radius/width; full coverage -> circle; many short
    pieces with gaps -> dashed (hidden) circle/arc."""
    groups = []
    for a in raw:
        for g in groups:
            h = g[0]
            if h["w"] == a["w"] and abs(h["r"] - a["r"]) <= max(0.01 * a["r"], 0.03) \
                    and _dist(h["c"], a["c"]) <= max(0.01 * a["r"], 0.03):
                g.append(a)
                break
        else:
            groups.append([a])
    arcs, circles = [], []
    for g in groups:
        w = g[0]["w"]
        cx = sum(a["c"][0] for a in g) / len(g)
        cy = sum(a["c"][1] for a in g) / len(g)
        r = sum(a["r"] for a in g) / len(g)
        style = "visible" if thick(w) else "thin"
        # merge angular intervals
        iv = []
        for a in g:
            s, e = a["a0"], a["a0"] + _ccw_span(a["a0"], a["a1"])
            iv.append((s, e))
        iv.sort()
        merged = []
        for s, e in iv:
            if merged and s <= merged[-1][1] + 0.5:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        if len(merged) > 1 and merged[-1][1] - 360 >= merged[0][0] - 0.5:  # wraps around 0°
            merged[0][0] = merged[-1][0] - 360
            merged[0][1] = max(merged[0][1], merged[-1][1] - 360)
            merged.pop()
        cover = sum(e - s for s, e in merged)
        if len(merged) == 1 and cover >= 359:
            circles.append({"c": (cx, cy), "r": r, "w": w, "style": style})
            continue
        if len(merged) >= 4 and cover / 360 > 0.3 and max(e - s for s, e in merged) < 60:
            span = merged[-1][1] - merged[0][0]
            if span >= 330:
                circles.append({"c": (cx, cy), "r": r, "w": w, "style": "hidden"})
            else:
                arcs.append({"c": (cx, cy), "r": r, "a0": merged[0][0] % 360, "a1": merged[-1][1] % 360,
                             "w": w, "style": "hidden"})
            continue
        for s, e in merged:
            arcs.append({"c": (cx, cy), "r": r, "a0": s % 360, "a1": e % 360, "w": w, "style": style})
    return arcs, circles


def _dedupe_circles(circles):
    """Same circle drawn twice (outline + thin overlay): keep the thickest."""
    out = []
    for c in sorted(circles, key=lambda c: -c["w"]):
        if any(_dist(c["c"], o["c"]) < 0.05 and abs(c["r"] - o["r"]) < 0.05 for o in out):
            continue
        out.append(c)
    return out


# ---------- DXF ----------
LAYERS = {"visible": ("VISIBLE", 7, None), "hidden": ("HIDDEN", 8, "DASHED"),
          "center": ("CENTER", 1, "CENTER"), "thin": ("THIN", 3, None),
          "dimension": ("DIMENSION", 4, None), "extension": ("DIMENSION", 4, None), "hatch": ("HATCH", 8, None)}


def write_dxf(ent: dict, output_path: Path) -> tuple[int, int]:
    dxf = ezdxf.new("R2010", setup=True)
    msp = dxf.modelspace()
    for name, color, lt in dict((v[0], v) for v in LAYERS.values()).values():
        attrs = {"color": color}
        if lt:
            attrs["linetype"] = lt
        dxf.layers.add(name, **attrs)
    dxf.layers.add("SPLINES", color=5)
    dxf.layers.add("TEXT", color=2)
    dxf.layers.add("TEXT_REGIONS", color=9)
    lay = lambda s: LAYERS[s][0]  # noqa: E731
    n = 0
    for ln in ent["lines"]:
        msp.add_line(ln["p1"], ln["p2"], dxfattribs={"layer": lay(ln["style"])})
        n += 1
    for c in ent["circles"]:
        msp.add_circle(c["c"], c["r"], dxfattribs={"layer": lay(c["style"])})
        n += 1
    for a in ent["arcs"]:
        msp.add_arc(a["c"], a["r"], a["a0"], a["a1"], dxfattribs={"layer": lay(a["style"])})
        n += 1
    for s in ent["splines"]:
        msp.add_open_spline(s["pts"], degree=3, dxfattribs={"layer": "SPLINES"})
        n += 1
    for t in ent["texts"]:
        e = msp.add_text(t["text"], height=max(t["height"] * 0.8, 0.5),
                         dxfattribs={"layer": "TEXT", "rotation": t["angle"]})
        e.set_placement((t["bbox"][0], t["bbox"][1]))
    for b in ent.get("text_regions", []):
        x0, y0, x1, y1 = b["bbox"]
        msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True,
                           dxfattribs={"layer": "TEXT_REGIONS"})
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    dxf.saveas(output_path)
    return n, len(ent["texts"])


def convert_pdf(input_path: Path, output_path: Path, page_number: int, scale: float) -> tuple[int, int]:
    pdf = fitz.open(input_path)
    if page_number > len(pdf):
        raise ValueError(f"{input_path.name}: has {len(pdf)} page(s); requested page {page_number}")
    return write_dxf(extract(pdf[page_number - 1], scale), output_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="PDF file or folder containing PDFs")
    parser.add_argument("output", help="DXF file for one PDF, or destination folder for folder input")
    parser.add_argument("--page", type=int, default=1, help="PDF page number (default: 1)")
    parser.add_argument("--scale", type=float, default=1.0, help="Additional scale multiplier (default: 1)")
    args = parser.parse_args()
    if args.page < 1:
        parser.error("--page must be 1 or greater")
    if args.scale <= 0 or not math.isfinite(args.scale):
        parser.error("--scale must be a positive finite number")

    source = Path(args.input).expanduser()
    destination = Path(args.output).expanduser()
    if source.is_dir():
        pdfs = sorted(p for p in source.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
        if not pdfs:
            parser.error(f"No PDF files found in {source}")
        destination.mkdir(parents=True, exist_ok=True)
        errors = 0
        for pdf_path in pdfs:
            dxf_path = destination / f"{pdf_path.stem}.dxf"
            try:
                geometry_count, text_count = convert_pdf(pdf_path, dxf_path, args.page, args.scale)
                print(f"{pdf_path.name} -> {dxf_path}: {geometry_count} geometry, {text_count} text entities")
            except (OSError, RuntimeError, ValueError, ezdxf.DXFError) as exc:
                errors += 1
                print(f"Failed {pdf_path.name}: {exc}", file=sys.stderr)
        print(f"Finished: {len(pdfs) - errors}/{len(pdfs)} converted.")
        return 1 if errors else 0

    if not source.is_file():
        parser.error(f"Input file not found: {source}")
    try:
        geometry_count, text_count = convert_pdf(source, destination, args.page, args.scale)
    except (OSError, RuntimeError, ValueError, ezdxf.DXFError) as exc:
        print(f"pdf2dxf: {exc}", file=sys.stderr)
        return 1
    print(f"Saved {destination}: {geometry_count} geometry, {text_count} text entities (page {args.page}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
