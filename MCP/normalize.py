"""Forgiving input layer: fix the format slips an AI makes in a first part JSON
before the strict schema sees it, and say what was changed.

Only FORMAT is fixed (strings vs numbers, {"x","y"} points, 'THRU', 'high' as
confidence, note fields, type aliases). Values and geometry are never invented:
anything that changes the part stays a schema/plausibility error.
"""
from __future__ import annotations

import copy
import difflib
import json
import re
from typing import Any, get_args

import schema as S

FEATURE_MODELS = {m.model_fields["type"].annotation.__args__[0]: m
                  for m in get_args(get_args(S.Feature)[0])}
BODY_MODELS = {m.model_fields["type"].annotation.__args__[0]: m
               for m in get_args(get_args(S.Body)[0])}

TYPE_ALIASES = {
    "through_hole": "hole", "blind_hole": "hole", "drill": "hole", "drilled_hole": "hole", "tapped_hole": "hole",
    "threaded_hole": "hole", "bore": "hole", "counterbore": "counterbore_hole", "cbore": "counterbore_hole",
    "counterbored_hole": "counterbore_hole", "countersink": "countersink_hole", "csk": "countersink_hole",
    "countersunk_hole": "countersink_hole", "rect_pocket": "pocket", "rectangular_pocket": "pocket",
    "polygon_pocket": "pocket_polygon", "step": "pocket_polygon", "shell": "cavity", "disc": "bottom_disc",
    "edge_fillet": "fillet", "round": "fillet", "edge_chamfer": "chamfer", "bevel": "chamfer",
    "standing_boss": "pad", "extrude": "pad", "extrusion": "pad", "plate": "box", "rectangle": "box",
    "rect": "box", "block": "box", "outline": "polygon", "profile": "polygon", "turned": "revolve",
    "lathe": "revolve", "hull": "circles_hull",
}
THROUGH = {"through", "thru", "through all", "through_all", "throughall", "durch", "durchgang", "through hole",
           "complete", "all", "full", "t", "through-all"}
LEVELS = {"high": 1.0, "certain": 1.0, "confirmed": 1.0, "sure": 1.0, "medium": 0.7, "med": 0.7,
          "moderate": 0.7, "low": 0.4, "guess": 0.3, "unsure": 0.4, "uncertain": 0.4}
NOTE_KEYS = {"note", "notes", "comment", "comments", "remark", "remarks", "label", "title", "name", "callout",
             "text", "info", "explanation", "reasoning", "purpose", "location", "view", "dims", "dimensions",
             "quantity", "qty", "count", "number", "n"}
POSITION_ALIASES = ("center", "centre", "position", "pos", "location", "xy", "point", "centers", "centres",
                    "points", "locations", "coords", "coordinates")
DEPTH_KEYS = ("depth", "bore_depth")
NUM = re.compile(r"^\s*(?:Ø|ø|⌀|R|M|r)?\s*([-+]?\d+(?:[.,]\d+)?)\s*(?:mm)?\s*$")


class Fixer:
    def __init__(self):
        self.fixes: list[str] = []
        self.ids: set = set()

    def note(self, where, what):
        self.fixes.append(f"{where}: {what}")

    # ---------- values ----------
    def point(self, v, where):
        if isinstance(v, dict) and "x" in v and "y" in v:
            out = [v["x"], v["y"]] + ([v["z"]] if "z" in v else [])
            self.note(where, "point {x, y} written as [x, y]")
            return out
        if isinstance(v, str):
            nums = re.findall(r"[-+]?\d+(?:[.,]\d+)?", v)
            if len(nums) in (2, 3):
                self.note(where, f"point '{v}' written as a list")
                return [float(n.replace(",", ".")) for n in nums]
        return v

    def number(self, v, where):
        if isinstance(v, str):
            m = NUM.match(v)
            if m:
                self.note(where, f"'{v}' read as {m.group(1)}")
                return float(m.group(1).replace(",", "."))
        return v

    def depth(self, v, where):
        if isinstance(v, str):
            if v.strip().lower() in THROUGH:
                if v != "through":
                    self.note(where, f"'{v}' read as 'through'")
                return "through"
            return self.number(v, where)
        return v

    # ---------- objects ----------
    def common(self, d: dict, where: str, model) -> dict:
        fields = model.model_fields
        # confidence given as a word
        if isinstance(d.get("confidence"), str):
            c = d["confidence"].strip().lower()
            if c in LEVELS:
                self.note(where, f"confidence '{d['confidence']}' read as {LEVELS[c]}")
                d["confidence"] = LEVELS[c]
            else:
                d["confidence"] = self.number(d["confidence"], f"{where}.confidence")
        if isinstance(d.get("confidence"), (int, float)) and d["confidence"] > 1:
            self.note(where, f"confidence {d['confidence']} read as percent")
            d["confidence"] = d["confidence"] / 100
        # source as plain text
        if isinstance(d.get("source"), str):
            d["source"] = {"text": d["source"]}
            self.note(where, "source text moved to source.text")
        elif isinstance(d.get("source"), list):
            d["source"] = {"dims": [str(x) for x in d["source"]]}
            self.note(where, "source list moved to source.dims")
        # note-like extra keys -> description / source
        for k in list(d):
            if k in fields:
                continue
            if k.lower() in NOTE_KEYS:
                val = d.pop(k)
                if k.lower() in ("view",) and "source" in fields:
                    d.setdefault("source", {})
                    if isinstance(d["source"], dict):
                        d["source"].setdefault("view", str(val))
                elif k.lower() in ("dims", "dimensions") and "source" in fields:
                    d.setdefault("source", {})
                    if isinstance(d["source"], dict):
                        d["source"].setdefault("dims", [str(x) for x in (val if isinstance(val, list) else [val])])
                elif k.lower() in ("quantity", "qty", "count", "number", "n"):
                    pos = d.get("positions")
                    if isinstance(pos, list) and isinstance(val, (int, float)) and len(pos) != int(val):
                        # keep it so the mismatch is reported, but as a clear error
                        d["_count_mismatch"] = (int(val), len(pos))
                    continue
                elif "description" in fields:
                    d["description"] = "; ".join(x for x in [d.get("description"), f"{k}: {val}"] if x)
                self.note(where, f"'{k}' kept as a note (not a geometry field)")
        return d

    def feature(self, f: Any, i: int) -> Any:
        if not isinstance(f, dict):
            return f
        f = dict(f)
        where = f"features[{i}]" + (f" ({f.get('id')})" if f.get("id") else "")
        t = f.get("type")
        if isinstance(t, str):
            tl = t.strip().lower().replace("-", "_").replace(" ", "_")
            tl = TYPE_ALIASES.get(tl, tl)
            if tl != t:
                self.note(where, f"type '{t}' read as '{tl}'")
            f["type"] = tl
            t = tl
        if "id" not in f and t:
            k = i + 1
            while f"{t[:1].upper()}{k}" in self.ids:
                k += 1
            f["id"] = f"{t[:1].upper()}{k}"
            self.ids.add(f["id"])
            self.note(where, f"missing id set to '{f['id']}'")
        model = FEATURE_MODELS.get(t)
        if model is None:
            return f
        fields = model.model_fields
        # single position given as center/position -> positions
        if "positions" in fields and "positions" not in f:
            for k in POSITION_ALIASES:
                if k in f:
                    v = f.pop(k)
                    single = isinstance(v, dict) or (isinstance(v, (list, tuple)) and len(v) in (2, 3)
                                                     and all(isinstance(x, (int, float)) for x in v))
                    if single:
                        v = [v]
                    f["positions"] = v
                    self.note(where, f"'{k}' read as positions")
                    break
        if "positions" in f:
            v = f["positions"]
            if isinstance(v, (list, tuple)) and len(v) in (2, 3) and all(isinstance(x, (int, float)) for x in v):
                f["positions"] = [list(v)]
                self.note(where, "single [x, y] wrapped into positions [[x, y]]")
            if isinstance(f["positions"], list):
                f["positions"] = [self.point(p, f"{where}.positions") for p in f["positions"]]
        for k in ("center", "start", "end", "entry", "direction", "size"):
            if k in f and k in fields:
                f[k] = self.point(f[k], f"{where}.{k}")
        if "points" in f and isinstance(f["points"], list):
            f["points"] = [self.point(p, f"{where}.points") if isinstance(p, dict) and "r" not in p and "c" not in p
                           else p for p in f["points"]]
        for k in DEPTH_KEYS:
            if k in f:
                f[k] = self.depth(f[k], f"{where}.{k}")
        for k in fields:
            if k in f and isinstance(f[k], str) and k not in ("id", "type", "review", "derivation", "description",
                                                             "edges", "side", "shape", "wall", "direction",
                                                             "face", "thread", "bore_depth", "depth"):
                f[k] = self.number(f[k], f"{where}.{k}")
        if isinstance(f.get("targets"), str):
            f["targets"] = [f["targets"]]
            self.note(where, "targets written as a list")
        return self.common(f, where, model)

    def body(self, b: Any) -> Any:
        if not isinstance(b, dict):
            return b
        b = dict(b)
        t = b.get("type")
        if isinstance(t, str):
            tl = TYPE_ALIASES.get(t.strip().lower(), t.strip().lower())
            if tl != t:
                self.note("body", f"type '{t}' read as '{tl}'")
            b["type"] = tl
            t = tl
        model = BODY_MODELS.get(t)
        if model is None:
            return b
        if t == "box":
            for a, k in (("height", "thickness"), ("depth", "width"), ("x", "length"), ("y", "width"),
                         ("z", "thickness"), ("size_x", "length"), ("size_y", "width")):
                if a in b and k not in b:
                    b[k] = b.pop(a)
                    self.note("body", f"'{a}' read as '{k}'")
        if t in ("polygon", "circles_hull") and "thickness" not in b and "height" in b:
            b["thickness"] = b.pop("height")
            self.note("body", "'height' read as 'thickness'")
        for k in ("length", "width", "thickness", "corner_radius"):
            if k in b:
                b[k] = self.number(b[k], f"body.{k}")
        if isinstance(b.get("points"), list):
            b["points"] = [self.point(p, "body.points") if isinstance(p, dict) and "r" not in p and "c" not in p
                           else p for p in b["points"]]
        if isinstance(b.get("circles"), list):
            for c in b["circles"]:
                if isinstance(c, dict) and "center" in c:
                    c["center"] = self.point(c["center"], "body.circles")
                    if "diameter" in c:
                        c["diameter"] = self.number(c["diameter"], "body.circles.diameter")
        return self.common(b, "body", model)

    def assumption(self, a: Any, i: int) -> Any:
        where = f"assumptions[{i}]"
        if isinstance(a, str):
            self.note(where, "plain text assumption written as {item, reason}")
            return {"item": a[:60], "reason": a}
        if not isinstance(a, dict):
            return a
        a = dict(a)
        c = a.get("confidence")
        if isinstance(c, (int, float)):
            lvl = "high" if c >= 0.8 else "medium" if c >= 0.5 else "low"
            self.note(where, f"confidence {c} read as '{lvl}'")
            a["confidence"] = lvl
        elif isinstance(c, str) and c.lower() not in ("high", "medium", "low"):
            v = LEVELS.get(c.lower())
            if v is not None:
                a["confidence"] = "high" if v >= 0.8 else "medium" if v >= 0.5 else "low"
                self.note(where, f"confidence '{c}' read as '{a['confidence']}'")
        elif isinstance(c, str):
            a["confidence"] = c.lower()
        if "item" not in a:
            for k in ("name", "what", "parameter", "feature", "field"):
                if k in a:
                    a["item"] = str(a.pop(k))
                    self.note(where, f"'{k}' read as item")
                    break
        if "reason" not in a:
            for k in ("why", "rationale", "note", "notes", "source", "basis", "explanation", "comment"):
                if k in a:
                    a["reason"] = str(a.pop(k))
                    self.note(where, f"'{k}' read as reason")
                    break
        return a


def normalize(raw: Any) -> tuple[Any, list[str], list[dict]]:
    """Returns (part dict, list of format fixes, errors that stop before the schema)."""
    fx = Fixer()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
            fx.note("part", "JSON text parsed")
        except json.JSONDecodeError as e:
            return None, fx.fixes, [{"check": "json", "where": f"line {e.lineno} col {e.colno}",
                                     "problem": f"part is not valid JSON: {e.msg}"}]
    if not isinstance(raw, dict):
        return None, fx.fixes, [{"check": "json", "where": "part", "problem": "part must be a JSON object"}]
    if set(raw) == {"part"} and isinstance(raw["part"], (dict, str)):
        return normalize(raw["part"])
    d = copy.deepcopy(raw)
    allowed = set(S.Part.model_fields)
    extra = {k: d.pop(k) for k in list(d) if k not in allowed}
    if extra:
        d["notes"] = extra
        fx.note("part", f"top-level keys {sorted(extra)} kept under notes")
    if isinstance(d.get("units"), str) and d["units"].strip().lower() in ("mm", "millimeter", "millimetre", "millimeters"):
        d["units"] = "mm"
    d["body"] = fx.body(d.get("body"))
    if isinstance(d.get("features"), list):
        fx.ids = {f.get("id") for f in d["features"] if isinstance(f, dict) and f.get("id")}
        d["features"] = [fx.feature(f, i) for i, f in enumerate(d["features"])]
    if isinstance(d.get("assumptions"), list):
        d["assumptions"] = [fx.assumption(a, i) for i, a in enumerate(d["assumptions"])]
    elif isinstance(d.get("assumptions"), dict):
        d["assumptions"] = [{"item": k, "reason": str(v)} for k, v in d["assumptions"].items()]
        fx.note("assumptions", "dict written as a list")
    errors = []
    for i, f in enumerate(d.get("features") or []):
        if isinstance(f, dict) and "_count_mismatch" in f:
            n, got = f.pop("_count_mismatch")
            errors.append({"check": "count", "where": f"features[{i}] ({f.get('id')})",
                           "problem": f"callout says {n}x but positions has {got} entries; give all {n} positions"})
    return d, fx.fixes, errors


# ---------- readable schema errors ----------
def friendly(errors: list[dict], part: dict) -> list[dict]:
    """Turn pydantic errors into one clear line per problem (merge union noise,
    name the feature by id, list allowed fields for unknown keys)."""
    out, seen = [], set()
    feats = part.get("features") if isinstance(part, dict) else None
    for e in errors:
        loc = list(e["loc"])
        msg = e["msg"]
        # drop union branch tags ('hole', 'constrained-float', "literal['through']")
        tags = [x for x in loc if isinstance(x, str) and (x in FEATURE_MODELS or x in BODY_MODELS
                                                         or x.startswith(("constrained", "literal[", "function-")))]
        clean = [x for x in loc if x not in tags]
        where = ""
        model = None
        for n, x in enumerate(clean):
            if x == "features" and n + 1 < len(clean) and isinstance(clean[n + 1], int):
                i = clean[n + 1]
                fid = feats[i].get("id") if feats and i < len(feats) and isinstance(feats[i], dict) else None
                ftype = feats[i].get("type") if feats and i < len(feats) and isinstance(feats[i], dict) else None
                model = FEATURE_MODELS.get(ftype)
                where = f"features[{i}]" + (f" {fid}" if fid else "") + (f" ({ftype})" if ftype else "")
                rest = clean[n + 2:]
                break
        else:
            rest = clean
            if clean and clean[0] == "body":
                b = part.get("body") if isinstance(part, dict) else None
                model = BODY_MODELS.get(b.get("type")) if isinstance(b, dict) else None
        field = ".".join(map(str, rest))
        where = ".".join(x for x in [where, field] if x) or "part"
        if any(str(t).startswith(("constrained", "literal[")) for t in tags) and "depth" in field:
            msg = "use a number (mm from the entry face) or 'through'"
        elif e["type"] == "extra_forbidden" and model is not None:
            key = str(rest[-1]) if rest else ""
            ok = [k for k in model.model_fields if k not in ("type",)]
            near = difflib.get_close_matches(key, ok, n=1)
            msg = f"unknown field '{key}'" + (f", did you mean '{near[0]}'?" if near else "") + \
                  f" Allowed: {', '.join(ok)}"
        elif e["type"] == "missing" and model is not None and rest:
            desc = model.model_fields.get(str(rest[-1]))
            msg = f"missing '{rest[-1]}'" + (f" ({desc.description})" if desc and desc.description else "")
        elif e["type"] == "union_tag_invalid":
            kinds = sorted(FEATURE_MODELS if "features" in clean else BODY_MODELS)
            msg = f"unknown type '{e.get('input', {}).get('type') if isinstance(e.get('input'), dict) else ''}'. " \
                  f"Use one of: {', '.join(kinds)}"
        elif e["type"] == "union_tag_not_found":
            msg = "missing 'type'"
        msg = msg.replace("Value error, ", "")
        key = (where, msg)
        if key in seen:
            continue
        seen.add(key)
        out.append({"check": "schema", "where": where, "problem": msg})
    return out
