"""Part description (intermediate JSON) for Drawing2CAD.

Conventions
- Units: mm. Angles: degrees.
- Origin: lower-left corner of the TOP view, on the BOTTOM face of the part.
  X = right, Y = up (in the top view), Z = thickness direction.
- All cutting features are machined from the TOP face downward.
- Every value that was not written on the drawing must be listed in
  `assumptions` with a reason (it goes on the review list).
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field

Point = tuple[float, float]


class Source(BaseModel):
    view: Optional[str] = Field(None, description="e.g. 'top', 'section A-A'")
    text: Optional[str] = Field(None, description="Text as written, e.g. '2x Ø8 THRU'")


class Base(BaseModel):
    source: Optional[Source] = None
    confidence: float = Field(1.0, ge=0, le=1)
    review: Optional[str] = Field(None, description="Why a human should check this")


# ---------- base body ----------
class BoxBody(Base):
    type: Literal["box"]
    length: float = Field(gt=0, description="X size")
    width: float = Field(gt=0, description="Y size")
    thickness: float = Field(gt=0, description="Z size")
    corner_radius: float = Field(0, ge=0, description="Radius on the 4 vertical edges")


class PolygonBody(Base):
    type: Literal["polygon"]
    points: list[Point] = Field(min_length=3, description="Outline in the top view, counter-clockwise")
    thickness: float = Field(gt=0)
    corner_radius: float = Field(0, ge=0, description="Radius on all vertical outer edges")


Body = Annotated[Union[BoxBody, PolygonBody], Field(discriminator="type")]


# ---------- features ----------
class Hole(Base):
    type: Literal["hole"]
    id: str
    positions: list[Point] = Field(min_length=1)
    diameter: float = Field(gt=0)
    depth: Union[float, Literal["through"]] = "through"


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


Feature = Annotated[
    Union[Hole, CounterboreHole, CountersinkHole, Slot, Pocket],
    Field(discriminator="type"),
]


class Assumption(BaseModel):
    item: str
    value: Union[float, str, None] = None
    reason: str


class Part(BaseModel):
    name: str
    units: Literal["mm"] = "mm"
    body: Body
    features: list[Feature] = []
    assumptions: list[Assumption] = []
