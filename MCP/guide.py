"""Texts for the AI: server instructions, workflow, drawing-reading guide and
one worked example (Fuss, BCT sample sheet). Returned by get_part_schema."""

SERVER_INSTRUCTIONS = """drawing2cad turns a 2D technical drawing into a validated 3D STEP model.
You never write geometry or CAD code: you read the drawing and write a part JSON; the server builds it.
Flow: load_drawing(pdf) -> look at the image + drawing JSON (zoom_drawing for small text) -> get_part_schema
-> write the part JSON -> validate_part_json -> build_from_json (look at the preview) -> ask the user the
short confirm list -> confirm_items or fix + rebuild -> export_step.
Never invent a dimension silently: every value that is not written on the drawing goes into `assumptions`
with a reason and a confidence (high / medium / low)."""

WORKFLOW = [
    "1. load_drawing(pdf_path): image of the sheet + drawing JSON (views, circles, arcs, lines, dimension lines, texts).",
    "2. Find the scale: dimension value / dimension sheet_length. Sheets often say 1:1 but are drawn at 1:2. "
    "Call load_drawing again with scale=<factor> so view geometry is in part mm.",
    "3. Identify the views: top view (plan, outline + holes), front/side view or section A-A (heights, depths). "
    "Use zoom_drawing on each view to read every dimension and symbol.",
    "4. Choose the base body: box | polygon outline (vertex r/c for corner radii/chamfers) | circles_hull "
    "(outline of bosses joined by tangents) | revolve (turned parts, half-profile).",
    "5. Add features from the drawing: holes / counterbores / countersinks / threads, slots, pockets, cavity "
    "(shelled tray) with islands, pads (standing bosses, rails, steps), bottom discs, side/axis holes, fillets/chamfers.",
    "6. Put the origin at the lower-left corner of the outline bbox in the top view, z = 0 on the underside. "
    "Convert positions: dimension chains from the drawing first, view geometry (local coords, scaled) second.",
    "7. Every value not written on the drawing -> assumptions[] {item, value, reason, confidence}. "
    "high = follows from the drawing (symmetry, general note, standard), medium = plausible reading, low = guess.",
    "8. validate_part_json until ok, then build_from_json. Compare the preview with the drawing views.",
    "9. Ask the user ONLY the confirm list (one line each with your value). Agreed -> confirm_items(ids); "
    "different value -> edit the JSON (confirmed: true) and rebuild.",
    "10. export_step('<part>.step'). With open confirm items it is saved as <part>_draft.step.",
]

READING_GUIDE = {
    "views": "First-angle (ISO E, Europe): the view from above is drawn BELOW the front view, the left view to "
             "the RIGHT. Third-angle (US): above / left. Check the projection symbol in the title block. "
             "Views that share X (aligned in load_drawing) show the same X positions.",
    "sections": "Hatched areas are cut material. 'A-A' names a section; the cutting plane is the chain line "
                "with arrows and letters A in another view. Sections give depths, floor and wall thickness, "
                "heights of bosses and blind-hole depths.",
    "hidden_lines": "Dashed lines are hidden edges (holes, pockets behind the surface). Chain lines "
                    "(long-short) are centre lines and symmetry axes, not edges.",
    "symbols": {
        "Ø": "diameter (hole, boss, pad, turned diameter)",
        "R": "radius (corner radius in plan -> vertex r; edge rounding -> fillet)",
        "M8 / M10x1": "metric thread (internal on holes, external on pads/shafts); '6H' / '6g' tolerance",
        "⌴ or 'Senkung' + Ø x depth": "counterbore (cbore_diameter, cbore_depth)",
        "⌵ or 'Senkung 90°'": "countersink (csk_diameter, csk_angle)",
        "3x / 4x": "number of equal features -> several positions in one feature",
        "1x45° / 0,5x45°, 'Fase'": "chamfer",
        "THRU / 'durch' / 'Durchgang'": "through hole (depth 'through'); 'tief' / '↧' = depth",
        "□ 20": "square size",
        "H7 / h6 / ±0,1": "fit or tolerance: model the nominal size, mention it in source.text",
        "( )": "reference dimension (check value, do not chain from it)",
        "Draft Angle / 'Formschräge'": "mould draft (recorded, not modelled except pad taper_deg)",
    },
    "general_notes": "Notes like 'Unbemaßte Radien R3' (undimensioned radii R3), 'Wandstärke 3 mm' "
                     "(wall thickness 3) or 'Formschrägen 3°' apply to every feature they describe: use them and "
                     "list them as assumptions with confidence high.",
    "scale": "Use written dimension values. Only measure from geometry when nothing is written, then write "
             "an assumption 'scaled from view Vn' (confidence medium or low).",
    "title_block": "Part name, material, scale, projection method, sheet. Put them into title_block.",
}

WORKED_EXAMPLE = {
    "drawing": "Fuss (BCT sample, A3, stated 1:1). load_drawing: V1 front view 115 x 76.2, V3 top view 115 x 115, "
               "V2 isometric. Measured: D9 sheet_length 115 reads '115' -> scale 1.0.",
    "reading": [
        "Top view V3: square 115 x 115 with R22 corners (4 arcs r=22); 4 circles Ø19 at (22,22), (93,22), (22,93), "
        "(93,93) -> 4x Ø19 through, concentric with the corner radii; spacing dimension 71 = 93 - 22.",
        "Top view V3: circles Ø57 and Ø33 at the centre -> standing boss Ø57 with bore Ø33.",
        "Front view V1: plate 19 thick (dimension 19), overall height 76.2 -> boss height 76.2 - 19 = 57.2; "
        "arcs r=9.5 at the boss root -> root fillet R9.5.",
        "Bore depth: hidden lines of the Ø33 bore run down through the plate -> through.",
    ],
    "part_json": {
        "name": "Fuss",
        "title_block": {"part_name": "Fuss", "company": "BCT", "scale_stated": "1:1", "sheet": "A3"},
        "body": {"type": "box", "length": 115, "width": 115, "thickness": 19, "corner_radius": 22,
                 "source": {"view": "top V3 / front V1", "dims": ["115", "115", "R22", "19"]}},
        "features": [
            {"type": "pad", "id": "P1", "shape": "circle", "center": [57.5, 57.5], "diameter": 57,
             "z0": 19, "height": 57.2, "bore_diameter": 33, "bore_depth": "through",
             "source": {"view": "front V1", "dims": ["Ø57", "Ø33", "76.2"]}, "derivation": "height = 76.2 - 19"},
            {"type": "hole", "id": "H1", "positions": [[22, 22], [93, 22], [22, 93], [93, 93]], "diameter": 19,
             "depth": "through", "source": {"view": "top V3", "text": "4x Ø19", "dims": ["71"]}},
            {"type": "fillet", "id": "R1", "edges": "pad_root", "targets": ["P1"], "radius": 9.5,
             "source": {"view": "front V1", "text": "R9.5"}},
        ],
        "assumptions": [
            {"item": "H1 positions", "value": "22 from each edge", "confidence": "high",
             "reason": "Only the spacing 71 is dimensioned; holes are concentric with the R22 corners (115 - 71) / 2 = 22"},
            {"item": "P1 bore depth", "value": "through", "confidence": "medium",
             "reason": "Hidden lines of the bore reach the underside in the front view; no THRU note"},
        ],
    },
    "confirm_list_you_would_ask": ["C1: P1 bore Ø33 goes through the plate (hidden lines reach the bottom). OK?"],
}


def worked_example() -> dict:
    return WORKED_EXAMPLE
