"""Drawing2CAD MCP server: PDF drawing -> drawing JSON + image -> part JSON
-> validate -> build -> check -> preview -> confirm -> STEP.

Requires: pip install -r requirements.txt  (mcp>=2 cadquery pydantic shapely pymupdf ezdxf)
Claude Desktop config: command = <venv>/bin/python, args = [<path>/server.py]
Env: OUTPUT_DIR (STEP, review JSON, DXF, mcp_log.jsonl), INPUT_DIR (where relative PDF paths are looked up),
     DRAWING2CAD_NOTIFY=0 to switch off desktop notifications,
     DRAWING2CAD_LOG=<path> to move the request log (default OUTPUT_DIR/mcp_log.jsonl, "0" = off).

Every tool response carries "dxf": the drawing DXF of the last load_drawing (sheet mm)
and, after a build, the model view DXFs (part mm, layers VISIBLE/HIDDEN).
"""
import functools
import json
import os
import time
from datetime import datetime
import platform
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cadquery as cq  # noqa: E402
from mcp.server.mcpserver import Image, MCPServer  # noqa: E402

import builder  # noqa: E402
import drawing  # noqa: E402
import guide  # noqa: E402
import preview  # noqa: E402
import schema  # noqa: E402
import validation as V  # noqa: E402
import views_dxf  # noqa: E402

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.expanduser("~/drawing2cad_output"))
INPUT_DIR = os.environ.get("INPUT_DIR", "")
os.makedirs(OUTPUT_DIR, exist_ok=True)

mcp = MCPServer("drawing2cad", instructions=guide.SERVER_INSTRUCTIONS)
state: dict = {"part": None, "model": None, "notes": [], "check": None, "confirm": [],
               "confirmed": {}, "drawing": None, "model_dxf": None}
LOG_PATH = os.environ.get("DRAWING2CAD_LOG", os.path.join(OUTPUT_DIR, "mcp_log.jsonl"))
_seq = [0]


def notify(msg: str):
    print(f"[drawing2cad] {msg}", file=sys.stderr)  # goes to Claude Desktop's MCP log
    if os.environ.get("DRAWING2CAD_NOTIFY", "1") == "0":
        return
    if platform.system() == "Darwin" and shutil.which("osascript"):
        subprocess.Popen(["osascript", "-e", f'display notification "{msg}" with title "drawing2cad"'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _text(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


def _dxf() -> dict:
    """DXF files the AI can open next to the PDF / STEP."""
    d = state["drawing"]
    return {"drawing": d.get("dxf") if d else None, "model": state["model_dxf"]}


def _files(obj, found: list) -> list:
    """Existing file paths anywhere in a request or response."""
    if isinstance(obj, dict):
        for v in obj.values():
            _files(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _files(v, found)
    elif isinstance(obj, str) and len(obj) < 1024 and (os.sep in obj or "." in obj):
        p = obj if os.path.isabs(obj) else _resolve(obj)
        if os.path.isfile(p) and p not in found:
            found.append(p)
    return found


def _summary(resp) -> tuple[dict, list]:
    """(response for the log without image bytes, list of the response's JSON parts)."""
    items = resp if isinstance(resp, list) else [resp]
    parts, logged = [], []
    for it in items:
        if isinstance(it, Image):
            logged.append({"image": "png"})
            continue
        try:
            obj = json.loads(it) if isinstance(it, str) else it
        except ValueError:
            obj = it
        parts.append(obj)
        logged.append(obj)
    head = parts[0] if parts and isinstance(parts[0], dict) else {}
    summ = {k: head[k] for k in ("ok", "status", "stage", "error", "error_count") if k in head}
    if isinstance(head.get("errors"), list):
        summ["errors"] = [e.get("problem", e) if isinstance(e, dict) else e for e in head["errors"][:5]]
    return {"summary": summ, "response": logged}, parts


def _log(entry: dict):
    if LOG_PATH == "0":
        return
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        print(f"[drawing2cad] log write failed: {e}", file=sys.stderr)


def _maybe_json(v):
    if isinstance(v, str) and v.lstrip()[:1] in "{[":
        try:
            return json.loads(v)
        except ValueError:
            pass
    return v


def logged(fn):
    """Log every call as one JSONL line: time, tool, request, files, response, duration."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        _seq[0] += 1
        t0, wall0 = time.perf_counter(), time.time()
        entry = {"ts": datetime.now().astimezone().isoformat(timespec="milliseconds"), "seq": _seq[0],
                 "pid": os.getpid(), "tool": fn.__name__, "request": {k: _maybe_json(v) for k, v in kwargs.items()}}
        try:
            resp = fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            entry.update(duration_ms=round((time.perf_counter() - t0) * 1000, 1),
                         summary={"ok": False, "exception": repr(e)})
            _log(entry)
            raise
        entry["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out, parts = _summary(resp)
        entry["summary"] = out["summary"]
        entry["files_in"] = _files(kwargs, [])
        entry["files_out"] = [p for p in _files(parts, []) if os.path.getmtime(p) >= wall0 - 0.5]
        text = json.dumps(out["response"], ensure_ascii=False, default=str)
        entry["response"] = out["response"] if len(text) < 50_000 else f"<{len(text)} chars, not logged>"
        _log(entry)
        return resp
    return wrapper


def _resolve(path: str) -> str:
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        for base in [INPUT_DIR, os.getcwd(), OUTPUT_DIR]:
            if base and os.path.exists(os.path.join(base, p)):
                return os.path.join(base, p)
    elif not os.path.exists(p) and INPUT_DIR:
        # in Docker the AI may still pass the host path: look up the file name in INPUT_DIR
        alt = os.path.join(INPUT_DIR, os.path.basename(p))
        if os.path.exists(alt):
            return alt
    return p


def _slug(name) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (name or "part").strip()).lower() or "part"


def _write_model_dxf(model, stem: str, res: dict):
    """Model view DXFs into OUTPUT_DIR; a failure is reported, never fatal."""
    try:
        state["model_dxf"] = views_dxf.export(model, OUTPUT_DIR, stem)
    except Exception as e:  # noqa: BLE001
        state["model_dxf"] = None
        res["dxf_error"] = str(e)


@mcp.tool()
@logged
def load_drawing(pdf_path: str, page: int = 1, scale: float | None = None) -> list:
    """Read a technical drawing (vector PDF). Call this FIRST when you get a drawing file.

    Saves a DXF of the sheet in the output folder and returns
    1. a drawing JSON: sheet size, frame, title block area, scale (stated text and
       measured from dimension text vs. geometry), the VIEWS (clustered outlines with
       a kind hint: orthographic / section (hatched) / isometric), and per view its
       circles (centre + diameter), arcs (radius), lines (hidden flag);
       DIMENSION LINES (length on the sheet, linked circle Ø / arc R, linked text);
       texts, or for PDFs with outlined lettering the text regions;
    2. the page as an image with the views boxed and labelled V1, V2, ...

    Geometry in a view is local to the view's lower-left corner. With scale=None it
    is in sheet mm; once you know the factor (dimension value / dimension sheet_length,
    e.g. 2.0 for a 1:2 sheet), call again with scale=<factor> to get part mm.
    Dimension values written on the drawing always win over measured geometry.
    pdf_path: absolute path, or relative to INPUT_DIR. page: 1-based."""
    path = _resolve(pdf_path)
    if not os.path.isfile(path):
        return [_text({"ok": False, "error": f"File not found: {path}"})]
    try:
        data, png = drawing.load(path, OUTPUT_DIR, page=page, scale=scale)
    except Exception as e:  # noqa: BLE001
        return [_text({"ok": False, "error": f"Could not read the PDF: {e}"})]
    state["drawing"] = {"path": path, "page": page, "views": data["views"], "dxf": data.get("dxf")}
    notify(f"load_drawing: {len(data['views'])} views")
    data["ok"] = True
    data["dxf"] = _dxf()
    data["next"] = ("Read the image and the JSON, zoom_drawing on views or dimensions you cannot read, "
                    "then get_part_schema and write the part JSON.")
    return [_text(data), Image(data=png, format="png")]


@mcp.tool()
@logged
def zoom_drawing(view: str | None = None, region: list[float] | None = None, max_px: int = 1400) -> list:
    """Zoom into the last loaded drawing to read small dimension text, symbols
    (Ø, R, M, ⌴ counterbore, ⌵ countersink, surface/tolerance marks) or details.
    view: a view id from load_drawing ('V2'); or region: [x0, y0, x1, y1] in sheet mm
    (origin lower-left of the page, as in bbox_sheet / mid_sheet). Returns an image."""
    d = state["drawing"]
    if d is None:
        return [_text({"ok": False, "error": "No drawing loaded. Call load_drawing first."})]
    if view:
        v = next((v for v in d["views"] if v["id"] == view), None)
        if v is None:
            return [_text({"ok": False, "error": f"Unknown view {view}; views: {[v['id'] for v in d['views']]}"})]
        x0, y0, x1, y1 = v["bbox_sheet"]
        m = 25  # keep the dimensions around the view
        region = [x0 - m, y0 - m, x1 + m, y1 + m]
    if not region or len(region) != 4:
        return [_text({"ok": False, "error": "Give view or region [x0, y0, x1, y1] (sheet mm)."})]
    png = drawing.render(d["path"], d["page"], max_px=max(400, min(max_px, 2400)), clip_sheet=region)
    return [_text({"ok": True, "region_sheet": [round(v, 1) for v in region], "dxf": _dxf()}),
            Image(data=png, format="png")]


@mcp.tool()
@logged
def get_part_schema() -> dict:
    """Return how to write the part JSON: the drawing-reading guide (views, sections,
    symbols, general notes), the conventions (origin, units, depths), the JSON schema
    with every field described in drawing terms, and one worked example from a real
    drawing. Call this before writing the part JSON."""
    return {"workflow": guide.WORKFLOW,
            "reading_guide": guide.READING_GUIDE,
            "conventions": schema.__doc__,
            "worked_example": guide.worked_example(),
            "json_schema": schema.Part.model_json_schema(),
            "dxf": _dxf()}


def _check(part) -> tuple:
    """parse + plausibility. Returns (parsed or None, response dict)."""
    fixes: list = []
    parsed, errors = V.parse(part, fixes)
    stage = "schema"
    if parsed is not None:
        errors = V.plausibility(parsed)
        stage = "plausibility"
    res = {"ok": not errors}
    if errors:
        res.update(stage=stage, error_count=len(errors), errors=errors[:25],
                   how_to_fix="Fix ALL listed errors in one go, then call again. Errors name the feature id and field.")
        hint = V.scale_hint(parsed, errors)
        if hint:
            res["hint"] = hint
    if fixes:
        res["format_fixes"] = fixes  # accepted, but write it this way next time
    return (parsed if not errors else None), res


@mcp.tool()
@logged
def validate_part_json(part: dict | str) -> dict:
    """Check a part JSON BEFORE building: schema (unknown keys, missing values),
    plausibility (features inside the outline, depths, counterbore/countersink sizes,
    threads, fillets...) and the review list. Small format slips ('THRU', {"x","y"}
    points, confidence words, note fields) are fixed and listed in format_fixes.
    Fix every listed error in one go and call again until ok."""
    parsed, res = _check(part)
    if parsed is not None:
        res.update(review_list=V.review_list(parsed), confirm=V.confirm_list(parsed))
    res["dxf"] = _dxf()
    return res


@mcp.tool()
@logged
def build_from_json(part: dict | str) -> list:
    """Validate the part JSON, build the 3D model and check it against the spec
    (valid solid, overall size, every hole probed for diameter and depth, fillets).

    Build first, confirm after: the model is built even when some values are
    uncertain. The result has
    - ok / model check results and the review list,
    - confirm: a SHORT list of medium/low-confidence assumptions and flagged features.
      Ask the user about exactly these (one line each, with your proposed value),
      then either set confirmed: true / fix the value in the JSON and rebuild, or call
      confirm_items when the user simply agrees,
    - status: 'draft' while confirm items are open, else 'final',
    - a PNG preview (isometric, top, front): compare it with the drawing views.
    Call export_step afterwards if ok is true."""
    notify("build_from_json called")
    parsed, res = _check(part)
    if parsed is None:
        res["dxf"] = _dxf()
        return [_text(res)]
    notes: list = []
    try:
        model = builder.build(parsed, notes)
    except Exception as e:  # noqa: BLE001
        state.update(part=None, model=None, notes=[], check=None, confirm=[], model_dxf=None)
        return [_text({"ok": False, "stage": "build", "errors": [{"problem": str(e)}], "dxf": _dxf()})]
    result = V.check_model(model, parsed, notes)
    confirm = V.confirm_list(parsed)
    state.update(part=parsed, model=model, notes=notes, check=result, confirm=confirm, confirmed={})
    result["stage"] = "model_check"
    if res.get("format_fixes"):
        result["format_fixes"] = res["format_fixes"]
    if not result["ok"]:
        result["how_to_fix"] = ("The model was built but a check failed: see checks/holes. Usually a depth, "
                                "position or z-height in the JSON does not match the geometry; fix and rebuild.")
    result["confirm"] = confirm
    result["status"] = "draft" if confirm else "final"
    result["review_list"] = V.review_list(parsed, notes)
    _write_model_dxf(model, _slug(parsed.name), result)
    result["dxf"] = _dxf()
    out = [_text(result)]
    try:
        title = f"{parsed.name} - {'DRAFT, ' + str(len(confirm)) + ' item(s) to confirm' if confirm else 'final'}"
        out.append(Image(data=preview.render(model, title), format="png"))
    except Exception as e:  # noqa: BLE001
        result["preview_error"] = str(e)
        out[0] = _text(result)
    return out


@mcp.tool()
@logged
def confirm_items(ids: list[str], answer: str = "confirmed by user") -> dict:
    """Record that the user confirmed items of the last build's confirm list
    (ids like 'C1', or 'all') without changing values. If the user gives a different
    value, change the part JSON instead and call build_from_json again."""
    if state["model"] is None:
        return {"ok": False, "error": "Nothing built yet.", "dxf": _dxf()}
    open_ids = {c["id"] for c in state["confirm"] if c["id"] != "more"}
    chosen = open_ids if ids == ["all"] or "all" in ids else set(ids)
    unknown = sorted(chosen - open_ids)
    for i in chosen & open_ids:
        state["confirmed"][i] = answer
    remaining = [c for c in state["confirm"] if c["id"] not in state["confirmed"] and c["id"] != "more"]
    return {"ok": not unknown, "unknown_ids": unknown, "remaining": remaining,
            "status": "draft" if remaining else "final", "dxf": _dxf()}


@mcp.tool()
@logged
def export_step(filename: str) -> list:
    """Export the last built model as STEP (filename without path, e.g.
    'grundplatte.step'), re-import it, and save <name>_review.json next to it
    (status, confirm list with answers, review list, model check) plus a preview PNG.
    While confirm items are open the file is written as <name>_draft.step and the
    review JSON says status 'draft'. Refuses if the last model check failed."""
    if state["model"] is None:
        return [_text({"ok": False, "error": "Nothing built yet. Call build_from_json first.", "dxf": _dxf()})]
    if not state["check"]["ok"]:
        failed = [k for k, v in state["check"]["checks"].items() if not v]
        return [_text({"ok": False, "error": f"Last build failed the model check {failed}; fix the JSON and rebuild.",
                       "dxf": _dxf()})]
    open_items = [c for c in state["confirm"] if c["id"] not in state["confirmed"] and c["id"] != "more"]
    status = "draft" if open_items else "final"
    stem = os.path.splitext(os.path.basename(filename))[0]
    if status == "draft" and not stem.endswith("_draft"):
        stem += "_draft"
    if status == "final" and stem.endswith("_draft"):
        stem = stem[:-6]
    path = os.path.join(OUTPUT_DIR, stem + ".step")
    cq.exporters.export(state["model"], path)
    step = V.check_step(path)
    review_path = os.path.join(OUTPUT_DIR, stem + "_review.json")
    png = preview.render(state["model"], f"{state['part'].name} - {status}")
    png_path = os.path.join(OUTPUT_DIR, stem + "_preview.png")
    with open(png_path, "wb") as fh:
        fh.write(png)
    extra: dict = {}
    _write_model_dxf(state["model"], stem, extra)
    confirm = [dict(c, answer=state["confirmed"].get(c["id"])) for c in state["confirm"]]
    with open(review_path, "w", encoding="utf-8") as f:
        json.dump({"part": state["part"].name, "step_file": os.path.basename(path), "status": status,
                   "validated": state["check"]["ok"] and step["reimport_ok"],
                   "confirm": confirm, "open_confirmations": open_items,
                   "model_check": state["check"], "step_check": step,
                   "review_list": V.review_list(state["part"], state["notes"]),
                   "drawing": state["drawing"]["path"] if state["drawing"] else None,
                   "dxf": _dxf()},
                  f, indent=2, ensure_ascii=False)
    notify(f"export_step: {os.path.basename(path)} ({status})")
    return [_text({"ok": step["reimport_ok"], "status": status, "path": path, "review_file": review_path,
                   "preview_file": png_path, "open_confirmations": open_items, **step, **extra,
                   "dxf": _dxf()}),
            Image(data=png, format="png")]


if __name__ == "__main__":
    mcp.run()
