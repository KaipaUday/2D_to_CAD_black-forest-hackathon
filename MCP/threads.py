"""ISO metric threads: designation -> nominal Ø, pitch, tap-drill Ø.

Threads are cosmetic (industry STEP practice): an internal thread is modelled
as its tap-drill hole, an external thread as a plain cylinder at nominal Ø.
The thread itself is written to the review JSON.
"""
from __future__ import annotations

import re

# ISO 261 coarse pitch; tap drill per ISO 2306 (= d - p, rounded as in shop tables)
COARSE = {
    1.6: (0.35, 1.25), 2: (0.4, 1.6), 2.5: (0.45, 2.05), 3: (0.5, 2.5),
    4: (0.7, 3.3), 5: (0.8, 4.2), 6: (1.0, 5.0), 8: (1.25, 6.8),
    10: (1.5, 8.5), 12: (1.75, 10.2), 14: (2.0, 12.0), 16: (2.0, 14.0),
    18: (2.5, 15.5), 20: (2.5, 17.5), 22: (2.5, 19.5), 24: (3.0, 21.0),
    27: (3.0, 24.0), 30: (3.5, 26.5), 36: (4.0, 32.0), 42: (4.5, 37.5),
    48: (5.0, 43.0),
}

_RE = re.compile(r"^\s*M\s*(\d+(?:[.,]\d+)?)\s*(?:[x×X]\s*(\d+(?:[.,]\d+)?))?\s*$")


def parse(designation: str, pitch: float | None = None) -> dict:
    """'M8' -> {'nominal': 8, 'pitch': 1.25, 'tap_drill': 6.8, 'fine': False}.
    Raises ValueError for unknown sizes or malformed designations."""
    m = _RE.match(designation)
    if not m:
        raise ValueError(f"Thread '{designation}' is not an ISO metric designation like 'M8' or 'M10x1'")
    d = float(m.group(1).replace(",", "."))
    p_txt = float(m.group(2).replace(",", ".")) if m.group(2) else None
    if p_txt is not None and pitch is not None and abs(p_txt - pitch) > 1e-6:
        raise ValueError(f"Pitch {pitch} contradicts designation '{designation}'")
    p = p_txt if p_txt is not None else pitch
    coarse = COARSE.get(d if d != int(d) else int(d))
    if p is None:
        if coarse is None:
            raise ValueError(f"M{d:g} is not in the ISO coarse table; give the pitch (e.g. 'M{d:g}x1')")
        p, drill = coarse
        fine = False
    else:
        fine = coarse is None or abs(p - coarse[0]) > 1e-6
        drill = coarse[1] if not fine else round(d - p, 2)
    if p <= 0 or p >= d / 2:
        raise ValueError(f"Pitch {p} is not plausible for M{d:g}")
    return {"designation": f"M{d:g}" + (f"x{p:g}" if fine else ""), "nominal": d,
            "pitch": p, "tap_drill": drill, "fine": fine}
