from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from domain import BloodGroup

Band = Literal["under 2 km", "2-5 km", "5-10 km", "10-25 km", "25-50 km"]


class NearbyBank(BaseModel):
    blood_bank_id: UUID
    name: str
    city: str
    distance_km: float              # banks are public facilities: their distance is not sensitive
    units: int                      # usable units across every compatible group
    units_same_group: int           # of those, exactly the patient's group: "6 B+ units (18 compatible)"
    blood_groups: list[BloodGroup]
    phone: str | None                       # a public facility's number: call before travelling
    stock_confirmed_at: datetime | None     # when the bank last confirmed its stock list; None = never (render "X ago")
    latitude: float                         # the bank's own stored coordinates, unrounded (a public building)
    longitude: float
    ownership: Literal["government", "private", "charitable", "unknown"]
    ownership_inferred: bool                # True = "government" read from the name, not from an OSM tag
    stock_is_demo: bool                     # True = the units are seed-demo data next to a real (osm) or synthetic name


class DonorBand(BaseModel):
    band: Band
    donors: int | None              # None = fewer than MIN_SHOWN: too few to show without singling someone out


class DonorsNearby(BaseModel):
    total: int | None               # None when the total itself is below MIN_SHOWN
    bands: list[DonorBand]
    how_to_reach_them: str
