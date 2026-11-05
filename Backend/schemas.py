from typing import Annotated
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from domain import BloodGroup

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class DonorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")        # unknown fields -> 422

    name: Name
    email: EmailStr
    age: int = Field(ge=18, le=65)
    blood_group: BloodGroup
    last_donated_at: AwareDatetime | None = None
    city: Name | None = None

    @field_validator("email")
    @classmethod
    def lowercase_email(cls, v: str) -> str:
        return v.lower()


class DonorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    is_available: bool | None = None
    last_donated_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def check_patch(self):
        if not self.model_fields_set:
            raise ValueError("empty update: send at least one field")
        for field in ("name", "is_available"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class DonorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)   # can be built from objects, not just dicts

    id: UUID
    name: str
    blood_group: BloodGroup
    is_available: bool
    eligible_now: bool


class DonorPage(BaseModel):
    """A list is an object, not a bare array, so fields can be added later without breaking clients."""

    items: list[DonorRead]
    next_cursor: str | None          # None = this was the last page