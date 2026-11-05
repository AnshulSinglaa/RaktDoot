from datetime import datetime
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from domain import BloodGroup, indian_ten_digits
from modules.inventory.service import Component, StockLine


class OrganizationKind(str, Enum):          # mirrors the CHECK in migration 0001
    hospital = "hospital"
    blood_bank = "blood_bank"
    ngo = "ngo"


Ownership = Literal["government", "private", "charitable", "unknown"]     # mirror the CHECKs of migration 0016
DataSource = Literal["osm", "synthetic", "manual"]

OrgName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
City = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]


def _facility_phone(v: str) -> str:
    """Landlines allowed (a blood bank's front desk), so any 10 national digits, but never a leading 0 after +91.
    Deliberately NOT the donor rule (mobiles only, 6-9), which stays strict."""
    digits = indian_ten_digits(v)
    if digits[0] == "0":
        raise ValueError("enter the number without a leading 0 after +91")
    return "+91" + digits


FacilityPhone = Annotated[str, AfterValidator(_facility_phone)]


def _both_or_neither(model):
    if (model.latitude is None) != (model.longitude is None):
        raise ValueError("send latitude and longitude together")
    return model


class OrganizationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: OrganizationKind
    name: OrgName
    city: City
    # optional, but a hospital or blood bank without a location cannot take part in emergency allocation
    latitude: Latitude | None = None
    longitude: Longitude | None = None
    phone: FacilityPhone | None = None
    has_blood_centre: bool = False                  # a hospital with its own blood centre (never a blood bank)

    _together = model_validator(mode="after")(_both_or_neither)

    @model_validator(mode="after")
    def blood_centre_only_for_hospitals(self):
        if self.has_blood_centre and self.kind != OrganizationKind.hospital:
            raise ValueError("has_blood_centre is only for hospitals")
        return self


class OrganizationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")        # kind is immutable: sending it is a 422

    name: OrgName | None = None                     # admin only (the router checks): what families see
    city: City | None = None
    latitude: Latitude | None = None                # admin only: decides where donors are searched and alerted
    longitude: Longitude | None = None              # admin only
    phone: FacilityPhone | None = None              # admin only: a wrong number misroutes emergencies
    has_blood_centre: bool | None = None            # admin only

    @model_validator(mode="after")
    def check_patch(self):
        if not self.model_fields_set:
            raise ValueError("empty update: send at least one field")
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        if ("latitude" in self.model_fields_set) != ("longitude" in self.model_fields_set):
            raise ValueError("send latitude and longitude together")
        return self


class OrganizationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: OrganizationKind
    name: str
    city: str
    latitude: float | None
    longitude: float | None
    phone: str | None
    stock_confirmed_at: datetime | None     # when the bank last said "our stock list is right"; None = never
    auto_confirm_holds: bool
    has_blood_centre: bool
    ownership: Ownership                    # who runs it: from OSM's operator:type, or (inferred) from the name
    ownership_inferred: bool                # True = read from the name ("Govt ..."), not tagged
    data_source: DataSource                 # osm (real facility), synthetic (seed-demo), manual (created via the API)


class BankSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_confirm_holds: bool


class OrganizationPage(BaseModel):
    items: list[OrganizationRead]
    next_cursor: str | None


class StockCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blood_group: BloodGroup
    component: Component
    units: int = Field(ge=0, le=10000)          # bags on the shelf, not the ones set aside for Rakt Doot holds


class StockTake(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counts: list[StockCount] = Field(max_length=24)     # 8 groups x 3 components, each at most once

    @field_validator("counts")
    @classmethod
    def unique(cls, v: list[StockCount]) -> list[StockCount]:
        pairs = [(c.blood_group, c.component) for c in v]
        if len(set(pairs)) != len(pairs):
            raise ValueError("count each blood group and component once")
        return v


class StockTakeResult(BaseModel):
    summary: list[StockLine]                    # the list after the count
    stock_confirmed_at: datetime                # = now: what families see as "stock counted X ago"
