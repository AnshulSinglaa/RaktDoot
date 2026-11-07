from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from domain import BloodGroup, indian_ten_digits

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


def _indian_mobile(v: str) -> str:
    """Accepts '98123 45678', '09812345678', '+91 98123-45678'; stores E.164 '+919812345678'. Indian mobiles start 6-9."""
    digits = indian_ten_digits(v)
    if digits[0] not in "6789":
        raise ValueError("enter a 10-digit Indian mobile number")
    return "+91" + digits


Phone = Annotated[str, AfterValidator(_indian_mobile)]
LOCATION_DECIMALS = 2       # ~1.1 km: enough to find "donors near this hospital", never a home address


class _ApproxLocation(BaseModel):
    """Donor location is health-data-grade personal data (DPDP). The SERVER rounds it, whatever the client sends."""
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("latitude", "longitude")
    @classmethod
    def round_to_about_a_km(cls, v: float | None) -> float | None:
        return None if v is None else round(v, LOCATION_DECIMALS)

    @model_validator(mode="after")
    def both_or_neither(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("send latitude and longitude together")
        return self


class DonorCreate(_ApproxLocation):
    model_config = ConfigDict(extra="forbid")        # unknown fields -> 422

    name: Name
    email: EmailStr
    age: int = Field(ge=18, le=65)
    blood_group: BloodGroup
    sex: Literal["male", "female", "other"]          # drives the donation interval (domain.donation_interval_days)
    phone: Phone | None = None                       # verified in person: by staff at a camp, or at the first donation
    # Self-declared at signup: accepted risk. The camp's medical screening is the real check.
    last_donated_at: AwareDatetime | None = None
    city: Name | None = None
    # DPDP Act: processing health data needs explicit consent. Must be literally true; false or missing is a 422.
    consent: Literal[True]

    @field_validator("email")
    @classmethod
    def lowercase_email(cls, v: str) -> str:
        return v.lower()


class DonorUpdate(_ApproxLocation):
    """What a donor may change about themselves. NOT last_donated_at: eligibility is a medical fact the blood bank
    attests to (POST /donors/{id}/donations). A donor who could edit it could make himself 'eligible' (threat model #1)."""
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    is_available: bool | None = None
    phone: Phone | None = None                      # a new number is UNverified until someone sees the donor again

    @model_validator(mode="after")
    def check_patch(self):
        if not self.model_fields_set:
            raise ValueError("empty update: send at least one field")
        for field in ("name", "is_available"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        if ("latitude" in self.model_fields_set) != ("longitude" in self.model_fields_set):
            raise ValueError("send latitude and longitude together")
        return self


class DonationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    donated_at: AwareDatetime | None = None     # None = now. Back-dating is allowed (paper records), the future is not.
    phone: Phone | None = None                  # the number the donor shows at the bank: verifies it ONLY if it matches


class PhoneVerification(BaseModel):
    """Staff must PRESENT the number they checked: the API verifies it only if it is the one on file (ADR 020)."""
    model_config = ConfigDict(extra="forbid")

    phone: Phone


class DonorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)   # can be built from objects, not just dicts

    id: UUID
    name: str
    blood_group: BloodGroup
    is_available: bool
    eligible_now: bool
    phone_verified: bool = False        # only donors with a verified phone are matched and alerted


class DonorPage(BaseModel):
    """A list is an object, not a bare array, so fields can be added later without breaking clients."""

    items: list[DonorRead]
    next_cursor: str | None          # None = this was the last page