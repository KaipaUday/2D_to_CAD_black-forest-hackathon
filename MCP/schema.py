"""Part description (intermediate JSON) for Drawing2CAD.

Conventions
- Units: mm. Angles: degrees.
- Origin: lower-left corner of the outline bounding box in the TOP view,
  on the UNDERSIDE of the plate (z = 0). X = right, Y = up (top view), Z = up.
  The plate top surface is at z = body.thickness.
- Cutting features (hole, slot, pocket, cavity) go from the TOP surface down.
- cavity: the outline offset inward by wall_thickness, cut `depth` deep.
  boss / rib / tab are ISLANDS inside the cavity: material that stays
  (from the cavity floor up to top_z). They require a cavity.
- bottom_disc: material added BELOW the plate (z = -height .. 0).
- Build order (fixed, independent of list order):
  body -> cavity with islands -> holes/slots/pockets -> bottom discs.
- Never guess silently: every value not written on the drawing goes into
  `assumptions` (or the feature's `review`) with a reason.
- Parameters marked "(not modelled)" are recorded and put on the review list
  but do not change the geometry yet.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

Point = tuple[float, float]


class Source(BaseModel):
    model_config = ConfigDict(extra="allow")
    view: Optional[str] = Field(None, description="e.g. 'top', 'section A-A'")
    text: Optional[str] = Field(None, description="Text as written on the drawing")
    dims: Optional[list[str]] = Field(None, description="Dimension values used")


class Base(BaseModel):
    # forbid unknown keys so typos like "diamter" are reported
    model_config = ConfigDict(extra="forbid")
    source: Optional[Source] = None
    confidence: float = Field(1.0, ge=0, le=1)
    review: Optional[str] = Field(None, description="Why a human should check this")
    derivation: Optional[str] = Field(None, description="How a value was calculated")
    description: Optional[str] = None


# ---------- base body ----------
class BoxBody(Base):
    type: Literal["box"]
    id: str = "B0"
    length: float = Field(gt=0, description="X size")
    width: float = Field(gt=0, description="Y size")
    thickness: float = Field(gt=0, description="Z size")
    corner_radius: float = Field(0, ge=0, description="Radius on the vertical outer edges")


class PolygonBody(Base):
    type: Literal["polygon"]
    id: str = "B0"
    points: list[Point] = Field(min_length=3, description="Outline in the top view")
    thickness: float = Field(gt=0)
    corner_radius: float = Field(0, ge=0, description="Radius on all vertical outer edges")


Body = Annotated[Union[BoxBody, PolygonBody], Field(discriminator="type")]


# ---------- cutting features ----------
class Hole(Base):
    type: Literal["hole"]
    id: str
    positions: list[Point] = Field(min_length=1)
    diameter: float = Field(gt=0)
    depth: Union[float, Literal["through"]] = "through"
    thread: Optional[str] = Field(None, description="e.g. 'M5' (recorded, not modelled)")


class CounterboreHole(Base):
    type: Literal["counterbore_hole"]
    id: str
    positions: list[Point] = Field(min_length=1)
    diameter: float = Field(gt=0)
    cbore_diameter: float = Field(gt=0)
    cbore_depth: float = Field(gt=0)
    depth: Union[float, Literal["through"]] = "through"


class CountersinkHole(Base):
    type: Literal["countersink_hole"]
    id: str
    positions: list[Point] = Field(min_length=1)
    diameter: float = Field(gt=0)
    csk_diameter: float = Field(gt=0)
    csk_angle: float = Field(90, gt=0, lt=180)
    depth: Union[float, Literal["through"]] = "through"


class Slot(Base):
    type: Literal["slot"]
    id: str
    start: Point = Field(description="Centre of one round end")
    end: Point = Field(description="Centre of the other round end")
    width: float = Field(gt=0)
    depth: Union[float, Literal["through"]] = "through"


class Pocket(Base):
    type: Literal["pocket"]
    id: str
    center: Point
    size: Point = Field(description="(x size, y size)")
    depth: float = Field(gt=0)
    corner_radius: float = Field(0, ge=0)


class PolygonPocket(Base):
    type: Literal["pocket_polygon"]
    id: str
    points: list[Point] = Field(min_length=3, description="Pocket outline in the top view; may extend past the part edge to make a step")
    depth: float = Field(gt=0)
    corner_radius: float = Field(0, ge=0)


# ---------- cavity and islands ----------
class Cavity(Base):
    type: Literal["cavity"]
    id: str
    wall_thickness: float = Field(gt=0)
    depth: float = Field(gt=0)
    floor_thickness: Optional[float] = Field(None, description="Check value: thickness - depth")
    corner_radius: Optional[float] = Field(None, description="Inner corner radius (from offset; recorded)")
    floor_fillet: float = Field(0, ge=0, description="(not modelled)")
    draft_deg: float = Field(0, ge=0, description="(not modelled)")
    islands: list[str] = Field([], description="ids of boss/rib/tab features (optional cross-reference)")


class Boss(Base):
    type: Literal["boss"]
    id: str
    center: Point
    diameter: float = Field(gt=0)
    top_z: Optional[float] = Field(None, description="Top height; default = plate top")


class Rib(Base):
    type: Literal["rib"]
    id: str
    center: Point
    direction: Literal["x", "y"]
    length: float = Field(gt=0)
    width: float = Field(gt=0)
    top_z: Optional[float] = Field(None, description="Top height; default = plate top")
    end_slope_deg: float = Field(0, ge=0, lt=90, description="(not modelled)")


class Tab(Base):
    type: Literal["tab"]
    id: str
    center_x: Optional[float] = Field(None, description="For wall 'top' or 'bottom'")
    center_y: Optional[float] = Field(None, description="For wall 'left' or 'right'")
    wall: Literal["top", "bottom", "left", "right"]
    width: float = Field(gt=0)
    length_from_inner_wall: float = Field(gt=0)
    tip_corner_radius: float = Field(0, ge=0)
    top_z: Optional[float] = None

    @model_validator(mode="after")
    def _position(self):
        need = "center_x" if self.wall in ("top", "bottom") else "center_y"
        if getattr(self, need) is None:
            raise ValueError(f"tab on wall '{self.wall}' needs {need}")
        return self


class BottomDisc(Base):
    type: Literal["bottom_disc"]
    id: str
    center: Point
    diameter: float = Field(gt=0)
    height: float = Field(gt=0, description="Below the plate underside")


Feature = Annotated[
    Union[Hole, CounterboreHole, CountersinkHole, Slot, Pocket, PolygonPocket,
          Cavity, Boss, Rib, Tab, BottomDisc],
    Field(discriminator="type"),
]


class Assumption(BaseModel):
    model_config = ConfigDict(extra="allow")
    item: str
    value: Any = None
    reason: str


class Part(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    units: Literal["mm"] = "mm"
    body: Body
    features: list[Feature] = []
    assumptions: list[Assumption] = []
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