"""Part description (intermediate JSON) for Drawing2CAD.
Every value comes from the drawing; anything else goes into `assumptions`.

Conventions
- Units: mm. Angles: degrees.
- Origin: lower-left corner of the outline bounding box in the TOP view,
  on the UNDERSIDE of the part (z = 0). X = right, Y = up (top view), Z = up.
  The plate top surface is at z = body.thickness.
- Outlines are vertex lists. A vertex is [x, y], [x, y, r] or
  {"x":..,"y":..,"r":..} / {"x":..,"y":..,"c":..}: r = corner radius
  (convex or concave), c = 45° corner chamfer, both in the top view.
  r/c must fit the two adjacent edges (checked).
- circles_hull: outline wrapped around circles (exact tangent lines + arcs),
  e.g. lever/bearing outlines made of bosses.
- revolve (turned parts): `points` is the closed half-profile as
  [axial position, radius] vertices (vertex r/c = profile fillet/chamfer).
  axis "z": the axis is vertical through (R, R) with R = max radius, axial = z.
  axis "x": the axis is horizontal along X at y = R, z = R, axial = x.
  Axial positions start at 0.
- Cutting features (hole, slot, pocket, cavity) go from the TOP surface down.
  Depths are measured from the plate top (z = thickness) unless the hole gives
  `from_z` (e.g. a cavity floor). "through" cuts through the whole part
  (pads and bottom discs included).
- side_hole: drilled into a side face of the bounding box: front (y=0, coords
  [x, z]), back (y=max, [x, z]), left (x=0, [y, z]), right (x=max, [y, z]),
  bottom (z=min, [x, y]). axis_hole: any direction: entry point + direction
  vector pointing INTO the material (inclined drillings).
- thread: "M8" or {"designation": "M10x1", "depth": 12}. Internal threads are
  built as the tap-drill hole (ISO table, diameter may be omitted); external
  threads (pad) as a plain cylinder at nominal Ø. Threads are cosmetic: they
  are written to the review JSON, never modelled as helix.
- cavity: the outline offset inward by wall_thickness, cut `depth` deep.
  boss / rib / tab are ISLANDS inside the cavity: material that stays
  (from the cavity floor up to top_z). They require a cavity.
- pad: material ADDED on top: circle | rect | polygon footprint, from z0
  (default plate top) up by height, optional taper and bore. Use it for
  standing bosses, rails, steps, tubes, studs.
- bottom_disc: material added BELOW the plate (z = -height .. 0).
- fillet / chamfer: explicit edge rounding. edges = top_outline |
  bottom_outline | vertical_corners | holes (targets = hole ids, side =
  entry|exit|both) | pad_top | pad_root (targets = pad ids). Hole chamfers are
  exact cones; other edges use the CadQuery fillet/chamfer and fall back to
  the review list if the kernel fails. Plan-view corner radii belong in the
  outline vertices (r), not here.
- Build order (fixed, independent of list order):
  body -> cavity with islands -> pads -> bottom discs -> holes/slots/pockets
  -> hole chamfers -> edge fillets/chamfers.
- Never guess silently: every value not written on the drawing goes into
  `assumptions` (or the feature's `review`) with a reason. Fields you leave
  out that have a default (e.g. depth "through", csk_angle 90, z0) are listed
  as "defaulted" on the review list.
- Parameters marked "(not modelled)" are recorded and put on the review list
  but do not change the geometry yet.
"""
from __future__ import annotations

from typing import Annotated, Any, ClassVar, Literal, Optional, Union

from pydantic import (BaseModel, BeforeValidator, ConfigDict, Field,
                      model_validator)

import threads as T

Point = tuple[float, float]
Point3 = tuple[float, float, float]
Depth = Union[Annotated[float, Field(gt=0)], Literal["through"]]


class Source(BaseModel):
    model_config = ConfigDict(extra="allow")
    view: Optional[str] = Field(None, description="Where on the drawing: 'top V3', 'section A-A', 'detail X'")
    text: Optional[str] = Field(None, description="Callout as written on the drawing, e.g. '4x Ø6.6 ⌴Ø11 ↧6.8'")
    dims: Optional[list[str]] = Field(None, description="Dimension values read from the drawing for this feature")


class Base(BaseModel):
    # forbid unknown keys so typos like "diamter" are reported
    model_config = ConfigDict(extra="forbid")
    # fields with a None default that still mean "a value was assumed"
    FLAG_IF_UNSET: ClassVar[tuple[str, ...]] = ()
    source: Optional[Source] = None
    confidence: float = Field(1.0, ge=0, le=1, description="How sure the reading of this feature is (below 0.8 = must be confirmed)")
    review: Optional[str] = Field(None, description="Why a human should check this (puts it on the confirm list)")
    confirmed: bool = Field(False, description="Set true after the user confirmed this feature")
    derivation: Optional[str] = Field(None, description="How a value was calculated from drawing dimensions, e.g. 'height = 76.2 - 19'")
    description: Optional[str] = Field(None, description="Free text (what the feature is on the part)")


# ---------- outline vertices ----------
class Vertex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float
    r: Optional[float] = Field(None, gt=0, description="Corner radius at this vertex")
    c: Optional[float] = Field(None, gt=0, description="45° corner chamfer length at this vertex")


def _vertex(v):
    if isinstance(v, (list, tuple)):
        if len(v) == 2:
            return {"x": v[0], "y": v[1]}
        if len(v) == 3:
            return {"x": v[0], "y": v[1], "r": v[2] or None}
        raise ValueError("vertex must be [x, y] or [x, y, r]")
    return v


VertexIn = Annotated[Vertex, BeforeValidator(_vertex)]


def vdicts(points, default_r: float = 0) -> list[dict]:
    """Vertices as plain dicts; default_r applies where no r/c is given."""
    out = []
    for p in points:
        d = p.model_dump()
        if default_r and not d["r"] and not d["c"]:
            d["r"] = default_r
        out.append(d)
    return out


# ---------- threads ----------
class ThreadSpec(Base):
    FLAG_IF_UNSET = ("depth",)
    designation: str = Field(description="ISO metric, e.g. 'M8' or 'M10x1'")
    pitch: Optional[float] = Field(None, gt=0, description="Only if not in the designation; default = ISO coarse")
    depth: Optional[float] = Field(None, gt=0, description="Thread length; default = full hole/pad length")
    tolerance: Optional[str] = Field(None, description="e.g. '6H', '6g' (recorded)")

    @model_validator(mode="after")
    def _known(self):
        T.parse(self.designation, self.pitch)  # raises on unknown size
        return self

    @property
    def info(self) -> dict:
        return T.parse(self.designation, self.pitch)


ThreadIn = Annotated[ThreadSpec, BeforeValidator(lambda v: {"designation": v} if isinstance(v, str) else v)]


# ---------- base body ----------
class BoxBody(Base):
    type: Literal["box"]
    id: str = "B0"
    length: float = Field(gt=0, description="Overall X size in the top view")
    width: float = Field(gt=0, description="Overall Y size in the top view")
    thickness: float = Field(gt=0, description="Plate thickness (Z), from the front view or section")
    corner_radius: float = Field(0, ge=0, description="Corner radius of the outline in the top view (R on the corners)")

    def top_z(self) -> float:
        return self.thickness


class PolygonBody(Base):
    type: Literal["polygon"]
    id: str = "B0"
    points: list[VertexIn] = Field(min_length=3, description="Outline in the top view; vertex r/c = corner radius/chamfer")
    thickness: float = Field(gt=0)
    corner_radius: float = Field(0, ge=0, description="Radius on every vertex that has no own r/c")

    def top_z(self) -> float:
        return self.thickness


class HullCircle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    center: Point = Field(description="Circle centre in the top view (part mm, origin lower-left)")
    diameter: float = Field(gt=0, description="Ø of the boss/eye that forms the outline")


class CirclesHullBody(Base):
    type: Literal["circles_hull"]
    id: str = "B0"
    circles: list[HullCircle] = Field(min_length=2, description="Outline = convex hull around these circles (tangent lines + arcs)")
    thickness: float = Field(gt=0)

    def top_z(self) -> float:
        return self.thickness


class RevolveBody(Base):
    type: Literal["revolve"]
    id: str = "B0"
    axis: Literal["z", "x"] = Field(description="z: vertical axis; x: horizontal shaft along X")
    points: list[VertexIn] = Field(min_length=3, description="Closed half-profile [axial, radius]; vertex r/c = fillet/chamfer")

    @model_validator(mode="after")
    def _profile(self):
        if any(p.y < 0 for p in self.points):
            raise ValueError("radius must be >= 0 in every profile point")
        if abs(min(p.x for p in self.points)) > 1e-6:
            raise ValueError("axial positions must start at 0 (origin convention)")
        if max(p.y for p in self.points) <= 0:
            raise ValueError("profile has no radius > 0")
        return self

    @property
    def radius(self) -> float:
        return max(p.y for p in self.points)

    @property
    def length(self) -> float:
        return max(p.x for p in self.points)

    def top_z(self) -> float:
        return self.length if self.axis == "z" else 2 * self.radius


Body = Annotated[Union[BoxBody, PolygonBody, CirclesHullBody, RevolveBody], Field(discriminator="type")]
PRISMATIC = (BoxBody, PolygonBody, CirclesHullBody)


# ---------- holes ----------
class _HoleCore(Base):
    """Fields shared by every drilled hole."""
    id: str
    diameter: Optional[float] = Field(None, gt=0, description="Drill Ø; may be omitted when thread is given (tap drill from ISO table)")
    depth: Depth = Field("through", description="'through' or depth from the entry face")
    thread: Optional[ThreadIn] = Field(None, description="Internal thread, e.g. 'M8' (cosmetic, tap drill modelled)")

    @model_validator(mode="after")
    def _thread(self):
        if self.thread is not None:
            info = self.thread.info
            if self.diameter is None:
                self.diameter = info["tap_drill"]
                self.derivation = (self.derivation or "") + \
                    f" Ø{info['tap_drill']} = tap drill for {info['designation']} (ISO 261/2306 table)."
            elif self.diameter >= info["nominal"]:
                raise ValueError(f"Ø{self.diameter} is not a core hole for {info['designation']} "
                                 f"(tap drill {info['tap_drill']}); omit diameter or fix it")
            if self.thread.depth is not None and self.depth != "through" and self.thread.depth > self.depth + 1e-6:
                raise ValueError(f"thread depth {self.thread.depth} > hole depth {self.depth}")
        elif self.diameter is None:
            raise ValueError("give diameter (or thread)")
        return self


class Hole(_HoleCore):
    type: Literal["hole"]
    positions: list[Point] = Field(min_length=1, description="Hole centres in the top view; '4x' = 4 positions")
    from_z: Optional[float] = Field(None, description="Entry height; default = plate top (e.g. give the cavity floor)")


class CounterboreHole(_HoleCore):
    type: Literal["counterbore_hole"]
    positions: list[Point] = Field(min_length=1, description="Hole centres in the top view")
    cbore_diameter: float = Field(gt=0, description="Counterbore Ø (⌴ symbol, 'Senkung' for socket head screws)")
    cbore_depth: float = Field(gt=0, description="Counterbore depth from the top (↧)")
    from_z: Optional[float] = Field(None, description="Entry height; default = plate top")


class CountersinkHole(_HoleCore):
    type: Literal["countersink_hole"]
    positions: list[Point] = Field(min_length=1, description="Hole centres in the top view")
    csk_diameter: float = Field(gt=0, description="Countersink outer Ø at the surface (⌵ symbol)")
    csk_angle: float = Field(90, gt=0, lt=180, description="Included cone angle, usually 90°")
    from_z: Optional[float] = Field(None, description="Entry height; default = plate top")


class _HeadOptions(BaseModel):
    """Optional counterbore / countersink on side and axis holes."""
    cbore_diameter: Optional[float] = Field(None, gt=0)
    cbore_depth: Optional[float] = Field(None, gt=0)
    csk_diameter: Optional[float] = Field(None, gt=0)
    csk_angle: Optional[float] = Field(None, gt=0, lt=180)

    @model_validator(mode="after")
    def _head(self):
        if (self.cbore_diameter is None) != (self.cbore_depth is None):
            raise ValueError("counterbore needs cbore_diameter and cbore_depth")
        if self.csk_diameter is not None and self.csk_angle is None:
            raise ValueError("countersink needs csk_angle (90 is common; write it if the drawing says so)")
        if self.cbore_diameter is not None and self.csk_diameter is not None:
            raise ValueError("give a counterbore or a countersink, not both")
        return self


class SideHole(_HoleCore, _HeadOptions):
    type: Literal["side_hole"]
    face: Literal["front", "back", "left", "right", "bottom"]
    positions: list[Point] = Field(min_length=1, description="Face coords: front/back [x, z], left/right [y, z], bottom [x, y]")


class AxisHole(_HoleCore, _HeadOptions):
    type: Literal["axis_hole"]
    entry: Point3 = Field(description="Point where the drill enters the surface")
    direction: Point3 = Field(description="Drill direction, pointing into the material")

    @model_validator(mode="after")
    def _dir(self):
        if sum(c * c for c in self.direction) < 1e-12:
            raise ValueError("direction must not be zero")
        return self


# ---------- other cutting features ----------
class Slot(Base):
    type: Literal["slot"]
    id: str
    start: Point = Field(description="Centre of one round end")
    end: Point = Field(description="Centre of the other round end")
    width: float = Field(gt=0, description="Slot width (= Ø of the round ends)")
    depth: Depth = Field("through", description="Depth from the top or 'through'")


class Pocket(Base):
    type: Literal["pocket"]
    id: str
    center: Point = Field(description="Pocket centre in the top view")
    size: Point = Field(description="(x size, y size) of the rectangular pocket / recess")
    depth: float = Field(gt=0, description="Depth from the top (from the section)")
    corner_radius: float = Field(0, ge=0, description="Inner corner radius in the top view")


class PolygonPocket(Base):
    type: Literal["pocket_polygon"]
    id: str
    points: list[VertexIn] = Field(min_length=3, description="Pocket outline in the top view; may extend past the part edge to make a step")
    depth: float = Field(gt=0)
    corner_radius: float = Field(0, ge=0, description="Radius on every vertex that has no own r/c")


# ---------- cavity and islands ----------
class Cavity(Base):
    type: Literal["cavity"]
    id: str
    wall_thickness: float = Field(gt=0, description="Wall thickness of the shelled tray ('Wandstärke')")
    depth: float = Field(gt=0, description="Cavity depth from the top (section: top to floor)")
    floor_thickness: Optional[float] = Field(None, description="Check value: thickness - depth")
    corner_radius: Optional[float] = Field(None, description="Inner corner radius (from offset; recorded)")
    floor_fillet: float = Field(0, ge=0, description="(not modelled)")
    draft_deg: float = Field(0, ge=0, description="(not modelled)")
    islands: list[str] = Field([], description="ids of boss/rib/tab features (optional cross-reference)")


class Boss(Base):
    FLAG_IF_UNSET = ("top_z",)
    type: Literal["boss"]
    id: str
    center: Point = Field(description="Centre in the top view")
    diameter: float = Field(gt=0, description="Outer Ø of the round island standing in the cavity")
    top_z: Optional[float] = Field(None, description="Top height; default = plate top")


class Rib(Base):
    FLAG_IF_UNSET = ("top_z",)
    type: Literal["rib"]
    id: str
    center: Point
    direction: Literal["x", "y"] = Field(description="Rib runs along X or Y in the top view")
    length: float = Field(gt=0, description="Rib length")
    width: float = Field(gt=0, description="Rib thickness")
    top_z: Optional[float] = Field(None, description="Top height; default = plate top")
    end_slope_deg: float = Field(0, ge=0, lt=90, description="(not modelled)")


class Tab(Base):
    FLAG_IF_UNSET = ("top_z",)
    type: Literal["tab"]
    id: str
    center_x: Optional[float] = Field(None, description="For wall 'top' or 'bottom'")
    center_y: Optional[float] = Field(None, description="For wall 'left' or 'right'")
    wall: Literal["top", "bottom", "left", "right"] = Field(description="Cavity wall (top view) the tab grows from")
    width: float = Field(gt=0, description="Tab width along the wall")
    length_from_inner_wall: float = Field(gt=0, description="How far the tab reaches into the cavity")
    tip_corner_radius: float = Field(0, ge=0, description="Radius on the tab tip corners")
    top_z: Optional[float] = Field(None, description="Top height; default = plate top")

    @model_validator(mode="after")
    def _position(self):
        need = "center_x" if self.wall in ("top", "bottom") else "center_y"
        if getattr(self, need) is None:
            raise ValueError(f"tab on wall '{self.wall}' needs {need}")
        return self


# ---------- added material ----------
class Pad(Base):
    FLAG_IF_UNSET = ("z0",)
    type: Literal["pad"]
    id: str
    shape: Literal["circle", "rect", "polygon"]
    center: Optional[Point] = Field(None, description="circle / rect")
    diameter: Optional[float] = Field(None, gt=0, description="circle")
    size: Optional[tuple[Annotated[float, Field(gt=0)], Annotated[float, Field(gt=0)]]] = Field(None, description="rect (x size, y size)")
    corner_radius: float = Field(0, ge=0, description="rect / polygon: vertical edge radius")
    points: Optional[list[VertexIn]] = Field(None, min_length=3, description="polygon outline")
    z0: Optional[float] = Field(None, description="Base height; default = plate top")
    height: float = Field(gt=0, description="Height above z0 (standing boss height = overall height - plate thickness)")
    taper_deg: float = Field(0, ge=0, lt=45, description="Draft of the side walls (real geometry)")
    bore_diameter: Optional[float] = Field(None, gt=0, description="Centre bore (circle / rect)")
    bore_depth: Optional[Depth] = Field(None, description="From the pad top; 'through' = whole part")
    thread: Optional[ThreadIn] = Field(None, description="External thread on a circle pad (cosmetic)")

    @model_validator(mode="after")
    def _shape(self):
        need = {"circle": ("center", "diameter"), "rect": ("center", "size"), "polygon": ("points",)}[self.shape]
        missing = [k for k in need if getattr(self, k) is None]
        if missing:
            raise ValueError(f"pad shape '{self.shape}' needs {', '.join(missing)}")
        if self.bore_diameter is not None:
            if self.shape == "polygon":
                raise ValueError("bore only for circle/rect pads; use a hole for polygon pads")
            if self.bore_depth is None:
                raise ValueError("bore_diameter needs bore_depth (number or 'through')")
            lim = self.diameter if self.shape == "circle" else min(self.size)
            if self.bore_diameter >= lim:
                raise ValueError(f"bore Ø{self.bore_diameter} must be smaller than the pad ({lim})")
        if self.shape == "rect" and self.corner_radius * 2 >= min(self.size):
            raise ValueError("corner_radius too large for the rect size")
        if self.thread is not None:
            if self.shape != "circle":
                raise ValueError("external thread needs a circle pad")
            nom = self.thread.info["nominal"]
            if abs(nom - self.diameter) > 0.05:
                raise ValueError(f"pad Ø{self.diameter} must equal thread nominal Ø{nom:g}")
        return self


class BottomDisc(Base):
    type: Literal["bottom_disc"]
    id: str
    center: Point = Field(description="Centre in the top view")
    diameter: float = Field(gt=0, description="Ø of the disc / collar under the plate (section)")
    height: float = Field(gt=0, description="Below the plate underside")


# ---------- edge treatment ----------
EdgeSet = Literal["top_outline", "bottom_outline", "vertical_corners", "holes", "pad_top", "pad_root"]


class _EdgeOp(Base):
    id: str
    edges: EdgeSet
    targets: list[str] = Field([], description="Feature ids for edges = holes / pad_top / pad_root")
    side: Optional[Literal["entry", "exit", "both"]] = Field(None, description="Hole edges only")

    @model_validator(mode="after")
    def _targets(self):
        if self.edges in ("holes", "pad_top", "pad_root") and not self.targets:
            raise ValueError(f"edges '{self.edges}' needs targets (feature ids)")
        if self.edges not in ("holes", "pad_top", "pad_root") and self.targets:
            raise ValueError(f"targets only apply to holes / pad_top / pad_root")
        if self.edges == "holes" and self.side is None:
            raise ValueError("hole edges need side: entry | exit | both")
        if self.edges != "holes" and self.side is not None:
            raise ValueError("side only applies to edges = holes")
        return self


class Fillet(_EdgeOp):
    type: Literal["fillet"]
    radius: float = Field(gt=0, description="R from the drawing (edge rounding, root fillet)")


class Chamfer(_EdgeOp):
    type: Literal["chamfer"]
    size: float = Field(gt=0, description="45° chamfer leg length")


Feature = Annotated[
    Union[Hole, CounterboreHole, CountersinkHole, SideHole, AxisHole, Slot, Pocket,
          PolygonPocket, Cavity, Boss, Rib, Tab, Pad, BottomDisc, Fillet, Chamfer],
    Field(discriminator="type"),
]

ROUND_HOLES = (Hole, CounterboreHole, CountersinkHole)
ALL_HOLES = ROUND_HOLES + (SideHole, AxisHole)


Level = Literal["high", "medium", "low"]


class Assumption(BaseModel):
    """A value that is NOT written on the drawing (measured from the image,
    taken from a general note, symmetry, a standard, or a guess)."""
    model_config = ConfigDict(extra="allow")
    item: str = Field(description="What was assumed, using feature ids, e.g. 'H1 depth' or 'P1 height'")
    value: Any = Field(None, description="The value used in the model")
    reason: str = Field(description="Why: 'scaled from top view', 'general note R3', 'symmetric to H1', 'not shown'")
    confidence: Level = Field("medium", description=(
        "high = follows directly from the drawing (symmetry, general note, standard); "
        "medium = plausible reading, should be confirmed; low = guess. "
        "medium/low go on the confirm list returned by build_from_json"))
    confirmed: bool = Field(False, description="Set true after the user confirmed this value")


class Part(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, description="Part name from the title block")
    description: Optional[str] = None
    units: Literal["mm"] = "mm"
    body: Body = Field(description="Base solid: box | polygon | circles_hull | revolve")
    features: list[Feature] = Field([], description="Everything else read from the drawing, in any order")
    assumptions: list[Assumption] = Field([], description="Every value NOT written on the drawing, with reason and confidence")
    # free-form documentation sections (not used for geometry)
    title_block: Optional[dict[str, Any]] = None
    coordinate_system: Optional[dict[str, Any]] = None
    modeling_plan: Optional[list[str]] = None
    verification: Optional[Any] = None

    @model_validator(mode="after")
    def _name(self):
        if not self.name:
            self.name = (self.title_block or {}).get("part_name")
        if not self.name:
            raise ValueError("Give 'name' or 'title_block.part_name'")
        return self

    @property
    def top(self) -> float:
        return self.body.top_z()
