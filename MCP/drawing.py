"""Read a vector PDF drawing into a compact drawing JSON + page image.

load(pdf, out_dir) -> (drawing_dict, png_bytes)
- runs pdf2dxf.extract (clean circles, arcs, dashed lines, dimension lines)
- saves <name>.dxf in out_dir
- finds the sheet frame and title block, clusters the views, links texts and
  dimension lines to geometry, estimates the drawing scale
Coordinates: sheet mm (origin lower-left of the page, y up) for bboxes;
geometry inside a view is LOCAL to the view bbox lower-left and multiplied
by `scale` (part mm per sheet mm, default 1 = raw sheet mm).
"""
from __future__ import annotations

import math
import os
import re
import statistics

import pymupdf as fitz

import pdf2dxf as P

STANDARD_SCALES = [(1, 1), (1, 2), (1, 2.5), (1, 5), (1, 10), (1, 20), (2, 1), (5, 1), (10, 1), (1, 1.5), (1, 4)]


def _r(v, n=2):
    return round(float(v), n)


def _bbox_of(ent) -> tuple:
    if "p1" in ent:
        (x0, y0), (x1, y1) = ent["p1"], ent["p2"]
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
    cx, cy = ent["c"]
    r = ent["r"]
    if "a0" not in ent:
        return cx - r, cy - r, cx + r, cy + r
    a0, span = ent["a0"], (ent["a1"] - ent["a0"]) % 360
    angs = [a0, a0 + span] + [k * 90 for k in range(9) if a0 < k * 90 < a0 + span]
    xs = [cx + r * math.cos(math.radians(a)) for a in angs]
    ys = [cy + r * math.sin(math.radians(a)) for a in angs]
    return min(xs), min(ys), max(xs), max(ys)


def _inside(b, box, tol=0.5):
    return b[0] >= box[0] - tol and b[1] >= box[1] - tol and b[2] <= box[2] + tol and b[3] <= box[3] + tol


def _gap(a, b):
    dx = max(0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _seg_dist(p, a, b):
    L2 = (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2
    if L2 < 1e-12:
        return P._dist(p, a)
    t = max(0, min(1, ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / L2))
    return P._dist(p, (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))


def _cluster_boxes(boxes, gap):
    """Union-find on boxes closer than gap. Returns list of index lists."""
    parent = list(range(len(boxes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    order = sorted(range(len(boxes)), key=lambda i: boxes[i][0])
    for n, i in enumerate(order):
        for j in order[n + 1:]:
            if boxes[j][0] > boxes[i][2] + gap:
                break
            if _gap(boxes[i], boxes[j]) <= gap:
                parent[find(i)] = find(j)
    groups = {}
    for i in range(len(boxes)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _union(bs):
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


# ---------- frame and title block ----------
def _frame(lines, W, H):
    """Inner drawing frame = innermost long horizontal/vertical lines."""
    hs = [l for l in lines if abs(l["p1"][1] - l["p2"][1]) < 0.1 and abs(l["p1"][0] - l["p2"][0]) > 0.6 * W]
    vs = [l for l in lines if abs(l["p1"][0] - l["p2"][0]) < 0.1 and abs(l["p1"][1] - l["p2"][1]) > 0.6 * H]
    bottom = [l["p1"][1] for l in hs if l["p1"][1] < 0.12 * H]
    top = [l["p1"][1] for l in hs if l["p1"][1] > 0.88 * H]
    left = [l["p1"][0] for l in vs if l["p1"][0] < 0.12 * W]
    right = [l["p1"][0] for l in vs if l["p1"][0] > 0.88 * W]
    return (max(left) if left else 0, max(bottom) if bottom else 0,
            min(right) if right else W, min(top) if top else H)


def _title_block(lines, frame):
    """Bottom-right block: highest horizontal line that reaches the right
    frame edge in the lower part of the sheet, and the vertical line that
    closes it on the left."""
    x0f, y0f, x1f, y1f = frame
    fh = y1f - y0f
    best_y = None
    for l in lines:
        (ax, ay), (bx, by) = l["p1"], l["p2"]
        if abs(ay - by) > 0.1:
            continue
        if max(ax, bx) >= x1f - 1 and min(ax, bx) > x0f + (x1f - x0f) * 0.3 and y0f + 5 < ay < y0f + 0.35 * fh:
            if abs(ax - bx) > 40 and (best_y is None or ay > best_y):
                best_y = ay
    if best_y is None:
        return None
    left = x1f
    for l in lines:
        (ax, ay), (bx, by) = l["p1"], l["p2"]
        if abs(ax - bx) < 0.1 and min(ay, by) <= y0f + 1 and max(ay, by) >= best_y - 1 and ax > x0f + 10:
            left = min(left, ax)
    if left >= x1f - 20:
        hz = [min(l["p1"][0], l["p2"][0]) for l in lines
              if abs(l["p1"][1] - best_y) < 0.1 and abs(l["p2"][1] - best_y) < 0.1]
        left = min(hz) if hz else x1f - 180
    return (left, y0f, x1f, best_y)


# ---------- text regions (outlined text) ----------
def _text_regions(glyphs):
    if not glyphs:
        return []
    out = []
    for grp in _cluster_boxes(glyphs, 0.9):
        b = _union([glyphs[i] for i in grp])
        out.append({"bbox": b, "center": ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2), "glyphs": len(grp)})
    out.sort(key=lambda t: (-t["center"][1], t["center"][0]))
    for i, t in enumerate(out, 1):
        t["id"] = f"T{i}"
    return out


# ---------- numbers in text ----------
NUM = re.compile(r"^\s*(?P<pre>[ØøⲪ⌀∅]|R|M|SR|S?Ø|O)?\s*(?P<val>\d+(?:[.,]\d+)?)(?![\d.,])(?!\s*(?:x|°|:))")


def _parse_dim(text):
    m = NUM.match(text)
    if not m:
        return None
    pre = (m.group("pre") or "").upper().replace("⌀", "Ø").replace("∅", "Ø").replace("Ⲫ", "Ø")
    if pre == "O":  # some CAD fonts map the diameter sign to 'O'
        pre = "Ø"
    kind = "diameter" if "Ø" in pre else "radius" if pre in ("R", "SR") else "thread" if pre == "M" else "length"
    return kind, float(m.group("val").replace(",", "."))


def _nearest_scale(f):
    """f = part mm per sheet mm. Return 'a:b' text of the nearest standard scale."""
    best = min(STANDARD_SCALES, key=lambda s: abs(math.log((s[1] / s[0]) / f)))
    a, b = best
    return f"{a:g}:{b:g}", b / a


# ---------- main ----------
def load(pdf_path: str, out_dir: str, page: int = 1, scale: float | None = None,
         max_lines_per_view: int = 250, image_px: int = 1600) -> tuple[dict, bytes]:
    doc = fitz.open(pdf_path)
    if page < 1 or page > len(doc):
        raise ValueError(f"{os.path.basename(pdf_path)} has {len(doc)} page(s); asked for page {page}")
    pg = doc[page - 1]
    ent = P.extract(pg)
    W, H = ent["page_mm"]
    regions = _text_regions(ent["glyph_boxes"]) if len(ent["texts"]) < 5 else []
    ent["text_regions"] = regions

    name = os.path.splitext(os.path.basename(pdf_path))[0]
    os.makedirs(out_dir, exist_ok=True)
    dxf_path = os.path.join(out_dir, f"{name}.dxf" if len(doc) == 1 else f"{name}_p{page}.dxf")
    P.write_dxf(ent, dxf_path)

    frame = _frame(ent["lines"], W, H)
    tb = _title_block(ent["lines"], frame)
    area = frame

    def usable(b):
        if not _inside(b, area, 0.3):
            return False
        if tb and _inside(b, tb, 0.5):
            return False
        # lines lying on the frame itself
        on = lambda v, ref: abs(v - ref) < 1.5  # noqa: E731
        return not ((on(b[1], area[1]) and on(b[3], area[1])) or (on(b[1], area[3]) and on(b[3], area[3]))
                    or (on(b[0], area[0]) and on(b[2], area[0])) or (on(b[0], area[2]) and on(b[2], area[2])))

    # views are clustered from visible geometry only: dashed centre lines are
    # often misread as hidden lines and stick out of the outline
    geo = [e for e in ent["lines"] + ent["circles"] + ent["arcs"] if e["style"] == "visible"]
    geo = [e for e in geo if usable(_bbox_of(e))]
    hidden = [e for e in ent["lines"] + ent["circles"] + ent["arcs"] if e["style"] == "hidden"]
    boxes = [_bbox_of(e) for e in geo]
    views = []
    for grp in _cluster_boxes(boxes, 4.0):
        b = _union([boxes[i] for i in grp])
        members = [geo[i] for i in grp]
        size = max(b[2] - b[0], b[3] - b[1])
        if size < 8 and len(grp) < 6:
            continue  # section arrows, symbols
        seg = [P._dist(m["p1"], m["p2"]) for m in members if "p1" in m]
        if size < 20 and len(seg) > 3 * size:
            continue  # lettering drawn as strokes (section letters)
        views.append({"bbox": b, "members": members})
    # a cluster inside another view's bbox belongs to that view
    merged = True
    while merged:
        merged = False
        for v in views:
            for o in views:
                if o is not v and _inside(o["bbox"], v["bbox"], 1.0):
                    v["members"] += o["members"]
                    views.remove(o)
                    merged = True
                    break
            if merged:
                break
    for e in hidden:  # hidden edges lie inside their view
        b = _bbox_of(e)
        for v in views:
            if _inside(b, v["bbox"], 0.5):
                v["members"].append(e)
                break
    views.sort(key=lambda v: (-(v["bbox"][3]), v["bbox"][0]))

    s = scale or 1.0
    texts = [t for t in ent["texts"] if usable(t["bbox"])]
    dims = [l for l in ent["lines"] if l["style"] == "dimension" and usable(_bbox_of(l))]
    centers = [l for l in ent["lines"] if l["style"] == "center"]
    hatch = [l for l in ent["lines"] if l["style"] == "hatch"]

    # ---- views ----
    out_views = []
    for i, v in enumerate(views, 1):
        b = v["bbox"]
        vid = f"V{i}"
        v["id"] = vid
        loc = lambda p: [_r((p[0] - b[0]) * s), _r((p[1] - b[1]) * s)]  # noqa: E731,B023
        lines = [m for m in v["members"] if "p1" in m]
        slanted = sum(P._dist(m["p1"], m["p2"]) for m in lines
                      if 10 < (math.degrees(math.atan2(m["p2"][1] - m["p1"][1], m["p2"][0] - m["p1"][0])) % 90) < 80)
        total = sum(P._dist(m["p1"], m["p2"]) for m in lines) or 1
        n_hatch = sum(1 for h in hatch if _inside(_bbox_of(h), b, 0.5))
        circ = sorted((m for m in v["members"] if "c" in m and "a0" not in m), key=lambda c: -c["r"])
        arcs = [m for m in v["members"] if "a0" in m]
        lines.sort(key=lambda m: -P._dist(m["p1"], m["p2"]))
        cl = [c for c in centers if _gap(_bbox_of(c), b) < 0.5]
        view = {
            "id": vid,
            "bbox_sheet": [_r(x) for x in b],
            "size": [_r((b[2] - b[0]) * s), _r((b[3] - b[1]) * s)],
            "kind_hint": ("isometric/pictorial (do not measure)" if slanted / total > 0.35 and not circ
                          else "section (hatched)" if n_hatch >= max(12, len(lines)) else "orthographic view"),
            "circles": [{"center": loc(c["c"]), "diameter": _r(2 * c["r"] * s), "hidden": c["style"] == "hidden"}
                        for c in circ],
            "arcs": [{"center": loc(a["c"]), "radius": _r(a["r"] * s), "start_deg": _r(a["a0"], 1) % 360,
                      "end_deg": _r(a["a1"], 1) % 360, "hidden": a["style"] == "hidden"} for a in arcs],
            "lines": [{"p1": loc(m["p1"]), "p2": loc(m["p2"]), **({"hidden": True} if m["style"] == "hidden" else {})}
                      for m in lines[:max_lines_per_view]],
            "center_lines": len(cl),
        }
        if len(lines) > max_lines_per_view:
            view["lines_truncated"] = f"{len(lines) - max_lines_per_view} shorter lines left out (see the DXF)"
        if "isometric" in view["kind_hint"]:
            view.pop("lines")
            view.pop("arcs")
        out_views.append(view)

    # alignment between views (projection relations)
    for a in out_views:
        ba = a["bbox_sheet"]
        a["aligned"] = []
        for o in out_views:
            if o is a:
                continue
            bo = o["bbox_sheet"]
            ox = min(ba[2], bo[2]) - max(ba[0], bo[0])
            oy = min(ba[3], bo[3]) - max(ba[1], bo[1])
            if ox > 0.5 * min(ba[2] - ba[0], bo[2] - bo[0]):
                a["aligned"].append(f"{o['id']} {'above' if bo[1] > ba[3] else 'below'} (same X)")
            elif oy > 0.5 * min(ba[3] - ba[1], bo[3] - bo[1]):
                a["aligned"].append(f"{o['id']} {'right' if bo[0] > ba[2] else 'left'} (same Y)")

    def view_of(p):
        best, bd = None, 30.0
        for v in views:
            d = _gap((p[0], p[1], p[0], p[1]), v["bbox"])
            if d < bd:
                best, bd = v, d
        return best

    # ---- dimension lines ----
    all_circles = [c for c in ent["circles"] if usable(_bbox_of(c))]
    all_arcs = [a for a in ent["arcs"] if usable(_bbox_of(a))]
    out_dims = []
    for k, d in enumerate(sorted(dims, key=lambda d: (-d["p1"][1], d["p1"][0])), 1):
        a, bpt = d["p1"], d["p2"]
        mid = ((a[0] + bpt[0]) / 2, (a[1] + bpt[1]) / 2)
        L = P._dist(a, bpt)
        v = view_of(mid)
        rec = {"id": f"D{k}", "view": v["id"] if v else None, "sheet_length": _r(L),
               "length": _r(L * s), "angle_deg": _r(math.degrees(math.atan2(bpt[1] - a[1], bpt[0] - a[0])) % 180, 1),
               "mid_sheet": [_r(mid[0]), _r(mid[1])]}
        for c in all_circles:  # diameter dimension through a circle centre
            if _seg_dist(c["c"], a, bpt) < 0.3 and abs(L - 2 * c["r"]) < 0.05 * L + 0.3:
                rec["for_circle_diameter"] = _r(2 * c["r"] * s)
                break
        else:
            for arc in all_arcs:  # radius leader from an arc centre
                if min(P._dist(arc["c"], a), P._dist(arc["c"], bpt)) < 0.5 and abs(L - arc["r"]) < 0.05 * L + 0.3:
                    rec["for_arc_radius"] = _r(arc["r"] * s)
                    break
        out_dims.append(rec)
        d["rec"] = rec

    # ---- texts: link to nearest dimension line / circle ----
    def nearest_dim(p, maxd=8.0):
        best, bd = None, maxd
        for d in dims:
            dd = _seg_dist(p, d["p1"], d["p2"])
            if dd < bd:
                best, bd = d, dd
        return best

    out_texts = []
    for t in texts:
        rec = {"text": t["text"], "center_sheet": [_r(t["center"][0]), _r(t["center"][1])],
               "angle_deg": t["angle"]}
        d = nearest_dim(t["center"])
        if d is not None:
            rec["dimension"] = d["rec"]["id"]
            d["rec"]["text"] = t["text"]
        v = view_of(t["center"])
        if v:
            rec["view"] = v["id"]
        out_texts.append(rec)
    out_regions = []
    for t in regions:
        if not usable(t["bbox"]):
            continue
        rec = {"id": t["id"], "bbox_sheet": [_r(x) for x in t["bbox"]]}
        d = nearest_dim(t["center"], 6.0)
        if d is not None:
            rec["dimension"] = d["rec"]["id"]
            d["rec"].setdefault("text_region", t["id"])
        v = view_of(t["center"])
        if v:
            rec["view"] = v["id"]
        out_regions.append(rec)

    # ---- scale ----
    sheet_scale = None
    for t in ent["texts"]:
        m = re.search(r"(?:^|[^\d.,])(\d+(?:[.,]\d+)?)\s*:\s*(\d+(?:[.,]\d+)?)(?![\d.,])", t["text"])
        if m and (re.search(r"ma(ß|ss)stab|scale|^m\s", t["text"], re.I) or (tb and _inside(t["bbox"], tb, 2))
                  or re.fullmatch(r"\s*(M\s*)?\d+(?:[.,]\d+)?\s*:\s*\d+(?:[.,]\d+)?\s*", t["text"])):
            sheet_scale = f"{m.group(1)}:{m.group(2)}"
            break
    ratios = []
    for d in dims:
        txt = d["rec"].get("text")
        if not txt or txt.lstrip()[:1] in "+-±(":
            continue
        pd = _parse_dim(txt)
        if not pd:
            continue
        kind, val = pd
        L = d["rec"]["sheet_length"]
        if kind == "diameter" and "for_circle_diameter" in d["rec"]:
            L = d["rec"]["for_circle_diameter"] / s
        if kind in ("length", "diameter") and L > 2 and val > 0:   # ordinate "0" baselines give ratio 0
            ratios.append(val / L)
    measured = None
    ratios = [r for r in ratios if r > 0 and math.isfinite(r)]
    if ratios:
        # the ratio most other samples agree with (tolerances and notes give outliers)
        try:
            med = max(ratios, key=lambda r: sum(1 for q in ratios if abs(q / r - 1) < 0.03))
            close = [q for q in ratios if abs(q / med - 1) < 0.03]
            agree = len(close)
            med = statistics.median(close)
            txt, f = _nearest_scale(med)
            measured = {"part_mm_per_sheet_mm": _r(med, 4), "nearest_standard": txt, "samples": len(ratios),
                        "agreeing": agree}
        except (ArithmeticError, ValueError) as e:  # the scale is only a hint: never fail the page
            measured = {"error": f"scale not measured: {e}"}

    text_mode = "text" if len(ent["texts"]) >= 5 else ("outlined" if len(ent["glyph_boxes"]) > 20 else "none")
    result = {
        "file": os.path.basename(pdf_path), "page": page, "pages": len(doc),
        "dxf": dxf_path,
        "sheet_mm": [_r(W), _r(H)],
        "frame_sheet": [_r(x) for x in frame],
        "title_block_sheet": [_r(x) for x in tb] if tb else None,
        "coordinates": ("bbox_sheet / *_sheet: sheet mm, origin lower-left of the page, y up. "
                        "View geometry (circles, arcs, lines) is LOCAL to the view bbox lower-left corner, "
                        f"multiplied by scale = {s:g} part mm per sheet mm."),
        "scale": {
            "applied": s,
            "sheet_scale_text": sheet_scale,
            "measured": measured,
            "how_to": ("Drawing dimensions beat scaled geometry. Read a long dimension value, divide it by "
                       "that dimension line's sheet_length -> part mm per sheet mm. Call load_drawing again "
                       "with scale=<that factor> to get view geometry in part mm."),
        },
        "text_mode": text_mode,
        "views": out_views,
        "dimensions": out_dims,
        "texts": out_texts,
        "text_regions": out_regions,
        "counts": {"lines": len(ent["lines"]), "circles": len(ent["circles"]), "arcs": len(ent["arcs"]),
                   "dimension_lines": len(dims), "arrowheads": len(ent["arrows"]), "hatch_lines": len(hatch)},
    }
    if text_mode == "outlined":
        result["text_note"] = ("This PDF has no extractable text: all lettering is drawn as outlines. "
                               "Read dimension values, labels and the title block from the image "
                               "(zoom_drawing for detail). text_regions mark where the lettering is; "
                               "each region is linked to the nearest dimension line where possible.")
    elif text_mode == "none":
        result["text_note"] = "No text found in the PDF. Read everything from the image."
    png = render(pdf_path, page, views=out_views, max_px=image_px)
    return result, png


def render(pdf_path: str, page: int = 1, views=None, max_px: int = 1600, clip_sheet=None) -> bytes:
    """Page (or a clip in sheet mm) as PNG; view boxes labelled V1.. in red."""
    doc = fitz.open(pdf_path)
    pg = doc[page - 1]
    Hpt = pg.rect.height
    k = 72 / 25.4
    if views:
        for v in views:
            x0, y0, x1, y1 = v["bbox_sheet"]
            r = fitz.Rect(x0 * k - 3, Hpt - y1 * k - 3, x1 * k + 3, Hpt - y0 * k + 3)
            pg.draw_rect(r, color=(0.9, 0.1, 0.1), width=0.6, dashes="[3 2] 0")
            pg.insert_text((r.x0 + 1, r.y0 - 2), v["id"], fontsize=9, color=(0.9, 0.1, 0.1))
    if clip_sheet:
        x0, y0, x1, y1 = clip_sheet
        clip = fitz.Rect(x0 * k, Hpt - y1 * k, x1 * k, Hpt - y0 * k) & pg.rect
    else:
        clip = pg.rect
    zoom = max_px / max(clip.width, clip.height)
    pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
    return pix.tobytes("png")
