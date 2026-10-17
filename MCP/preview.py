"""PNG preview of a built model: isometric + top + front, so the AI can
compare the result with the drawing. SVG from CadQuery, rasterised with PyMuPDF."""
from __future__ import annotations

import cadquery as cq
import pymupdf as fitz
from cadquery.occ_impl.exporters.svg import getSVG

VIEWS = [("isometric", (1, -1, 0.8), False), ("top view", (0, 0, 1), False), ("front view", (0, 0, 1), True)]


def _svg(shape, direction, size) -> str:
    return getSVG(shape, {"width": size, "height": size, "marginLeft": 10, "marginTop": 10,
                          "showAxes": False, "projectionDir": direction, "strokeWidth": 0.4,
                          "strokeColor": (20, 20, 20), "hiddenColor": (150, 150, 220), "showHidden": True})


def render(model: cq.Workplane, title: str = "", size: int = 520) -> bytes:
    """Three views side by side as one PNG."""
    shape = model.val() if isinstance(model, cq.Workplane) else model
    pad, head = 12, 40
    out = fitz.open()
    page = out.new_page(width=len(VIEWS) * (size + pad) + pad, height=size + head + pad)
    page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
    # getSVG keeps Z up, so the front view is drawn as a top view of the
    # part turned -90° about X (front face up, part Z along sheet Y)
    front = shape.rotate(cq.Vector(0, 0, 0), cq.Vector(1, 0, 0), -90)
    for i, (label, direction, turned) in enumerate(VIEWS):
        svg = _svg(front if turned else shape, direction, size).encode()
        src = fitz.open(stream=svg, filetype="svg")
        pdf = fitz.open("pdf", src.convert_to_pdf())
        x0 = pad + i * (size + pad)
        rect = fitz.Rect(x0, head, x0 + size, head + size)
        page.show_pdf_page(rect, pdf, 0, keep_proportion=True)
        page.insert_text((x0 + 4, head - 8), label, fontsize=11, color=(0.2, 0.2, 0.2))
    if title:
        page.insert_text((pad, 14), title, fontsize=11, color=(0.6, 0.1, 0.1))
    return page.get_pixmap(dpi=96, alpha=False).tobytes("png")
