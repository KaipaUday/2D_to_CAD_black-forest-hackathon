"""Quick regression run: python test_all.py
1. every examples/*.json must build and pass the model check (run_json.run)
2. every bad JSON below must be REJECTED with the expected message."""
import glob
import sys

import run_json
import validation as V

BOX = {"type": "box", "length": 100, "width": 60, "thickness": 10}


def part(features, body=BOX):
    return {"name": "bad", "body": body, "features": features}


BAD = [
    ("vertex radius too big", part([], {"type": "polygon", "thickness": 5,
                                        "points": [[0, 0, 30], [40, 0, 30], [40, 40], [0, 40]]}), "too short"),
    ("r and c on one vertex", part([], {"type": "polygon", "thickness": 5,
                                        "points": [{"x": 0, "y": 0, "r": 2, "c": 1}, [40, 0], [40, 40]]}), "not both"),
    ("unknown field", part([{"type": "hole", "id": "H", "positions": [[5, 5]], "diamter": 3}]), "Extra inputs"),
    ("negative diameter", part([{"type": "hole", "id": "H", "positions": [[5, 5]], "diameter": -3}]), "greater than 0"),
    ("hole without diameter", part([{"type": "hole", "id": "H", "positions": [[5, 5]]}]), "diameter (or thread)"),
    ("unknown thread", part([{"type": "hole", "id": "H", "positions": [[5, 5]], "thread": "M7"}]), "not in the ISO"),
    ("thread with nominal Ø", part([{"type": "hole", "id": "H", "positions": [[5, 5]], "thread": "M8", "diameter": 8}]), "not a core hole"),
    ("thread deeper than hole", part([{"type": "hole", "id": "H", "positions": [[5, 5]], "depth": 5,
                                       "thread": {"designation": "M5", "depth": 8}}]), "thread depth"),
    ("blind hole too deep", part([{"type": "hole", "id": "H", "positions": [[50, 30]], "diameter": 5, "depth": 30}]), "below the part"),
    ("hole outside", part([{"type": "hole", "id": "H", "positions": [[99, 30]], "diameter": 5, "depth": "through"}]), "outside the part"),
    ("pad bore too big", part([{"type": "pad", "id": "P", "shape": "circle", "center": [50, 30], "diameter": 20,
                                "bore_diameter": 25, "bore_depth": "through", "z0": 10, "height": 5}]), "smaller than the pad"),
    ("floating pad", part([{"type": "pad", "id": "P", "shape": "circle", "center": [50, 30], "diameter": 20,
                            "z0": 30, "height": 5}]), "float"),
    ("pad taper closes top", part([{"type": "pad", "id": "P", "shape": "circle", "center": [50, 30], "diameter": 10,
                                    "z0": 10, "height": 40, "taper_deg": 20}]), "closes the pad top"),
    ("ext thread wrong Ø", part([{"type": "pad", "id": "P", "shape": "circle", "center": [50, 30], "diameter": 9,
                                  "z0": 10, "height": 10, "thread": "M10"}]), "must equal thread nominal"),
    ("fillet >= thickness", part([{"type": "fillet", "id": "F", "edges": "top_outline", "radius": 10}]), "smaller than the plate thickness"),
    ("fillet on missing hole", part([{"type": "chamfer", "id": "C", "edges": "holes", "targets": ["X"], "side": "entry",
                                      "size": 1}]), "does not exist"),
    ("hole edge without side", part([{"type": "hole", "id": "H", "positions": [[50, 30]], "diameter": 5, "depth": "through"},
                                     {"type": "chamfer", "id": "C", "edges": "holes", "targets": ["H"], "size": 1}]), "need side"),
    ("exit chamfer on blind hole", part([{"type": "hole", "id": "H", "positions": [[50, 30]], "diameter": 5, "depth": 4},
                                         {"type": "chamfer", "id": "C", "edges": "holes", "targets": ["H"], "side": "exit",
                                          "size": 1}]), "no exit edge"),
    ("chamfer breaks edge", part([{"type": "hole", "id": "H", "positions": [[4, 30]], "diameter": 6, "depth": "through"},
                                  {"type": "chamfer", "id": "C", "edges": "holes", "targets": ["H"], "side": "entry",
                                   "size": 1.5}]), "breaks into"),
    ("side hole off face", part([{"type": "side_hole", "id": "S", "face": "front", "positions": [[50, 9]],
                                  "diameter": 5, "depth": 10}]), "not inside the front face"),
    ("csk without angle", part([{"type": "side_hole", "id": "S", "face": "front", "positions": [[50, 5]],
                                 "diameter": 3, "depth": 10, "csk_diameter": 6}]), "csk_angle"),
    ("revolve not at 0", part([], {"type": "revolve", "axis": "z", "points": [[2, 0], [2, 5], [10, 5], [10, 0]]}), "start at 0"),
    ("cavity on revolve", part([{"type": "cavity", "id": "C", "wall_thickness": 1, "depth": 2}],
                               {"type": "revolve", "axis": "z", "points": [[0, 0], [0, 5], [10, 5], [10, 0]]}), "cavity needs"),
]


def drawing_checks() -> bool:
    """PDF reading: Bezier arcs, flattened circles, and the sample PDFs in
    $DRAWINGS_DIR (fuss.pdf, grundplatte.pdf, lagerschaft.pdf) when present."""
    import math
    import os
    import pdf2dxf as P
    ok = True
    k = 0.5523 * 10  # quarter circle r=10 as one cubic
    arc = P.bezier_as_arc([(10, 0), (10, k), (k, 10), (0, 10)])
    hit = arc is not None and abs(arc["r"] - 10) < 0.01 and min(arc["a0"], 360 - arc["a0"]) < 0.1 and abs(arc["a1"] - 90) < 0.1
    pts = [(5 + 3 * math.cos(t / 24 * 2 * math.pi), 7 + 3 * math.sin(t / 24 * 2 * math.pi)) for t in range(25)]
    rest, arcs = P._fit_polyline_arcs([(pts[i], pts[i + 1], 0.5) for i in range(24)])
    _, circles = P._merge_arcs(arcs, lambda w: True)
    hit &= not rest and len(circles) == 1 and abs(circles[0]["r"] - 3) < 0.05
    ok &= hit
    print(f"{'OK  ' if hit else 'FAIL'} bezier arc + flattened circle detection")
    base = os.environ.get("DRAWINGS_DIR", "")
    expect = {"fuss.pdf": ([115.0, 115.0], 19.0), "grundplatte.pdf": ([179.0, 85.0], 29.0),
              "lagerschaft.pdf": ([180.0, 90.0], 30.0)}
    if base and os.path.isdir(base):
        import tempfile
        import drawing
        for f, (size, dia) in expect.items():
            path = os.path.join(base, f)
            if not os.path.exists(path):
                continue
            d, png = drawing.load(path, tempfile.mkdtemp())
            sizes = [v["size"] for v in d["views"]]
            dias = {c["diameter"] for v in d["views"] for c in v["circles"]}
            hit = size in sizes and dia in dias and png[:4] == b"\x89PNG" and os.path.exists(d["dxf"])
            ok &= hit
            print(f"{'OK  ' if hit else 'FAIL'} load_drawing {f}: views {sizes}, dims {len(d['dimensions'])}")
    else:
        print("skip load_drawing PDFs (set DRAWINGS_DIR to a folder with fuss/grundplatte/lagerschaft.pdf)")
    return ok


def main() -> int:
    ok = True
    for f in sorted(glob.glob("examples/*.json")):
        ok &= run_json.run(f, quiet=True)
    for name, js, expect in BAD:
        p, errs = V.parse(js)
        if p is not None:
            errs = V.plausibility(p)
        msg = " | ".join(e["problem"] for e in errs)
        hit = expect in msg
        ok &= hit
        print(f"{'OK  ' if hit else 'FAIL'} rejects {name}: {msg[:110] if msg else 'NOT REJECTED'}")
    # defaults are never silent
    p, _ = V.parse(part([{"type": "countersink_hole", "id": "K", "positions": [[50, 30]], "diameter": 5, "csk_diameter": 10}]))
    flagged = {r["item"] for r in V.review_list(p) if r["kind"] == "defaulted"}
    hit = {"K.depth", "K.csk_angle"} <= flagged
    ok &= hit
    print(f"{'OK  ' if hit else 'FAIL'} defaulted fields on review list: {sorted(flagged)}")
    # confirm list: medium/low assumptions and flagged features, confirmed ones left out
    p, _ = V.parse(part([{"type": "hole", "id": "H", "positions": [[50, 30]], "diameter": 5, "review": "depth?"},
                         {"type": "hole", "id": "J", "positions": [[20, 30]], "diameter": 5, "review": "x", "confirmed": True}])
                   | {"assumptions": [{"item": "a", "reason": "r", "confidence": "high"},
                                      {"item": "b", "reason": "r", "confidence": "low"},
                                      {"item": "c", "reason": "r"}]})
    got = [c["item"] for c in V.confirm_list(p)]
    hit = got == ["b", "c", "H"]
    ok &= hit
    print(f"{'OK  ' if hit else 'FAIL'} confirm list: {got}")
    ok &= drawing_checks()
    print("ALL OK" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
