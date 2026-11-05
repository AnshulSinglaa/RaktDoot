from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, StringConstraints, field_validator, model_validator

# NIST SP 800-63B: length beats composition rules. 12+ chars, no "must contain a symbol" theatre.
# The upper bound stops a 1 MB "password" from tying up argon2.
Password = Annotated[str, StringConstraints(min_length=12, max_length=128)]


class _Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr

    @field_validator("email")
    @classmethod
    def lowercase_email(cls, v: str) -> str:
        return v.lower()


class RegisterRequest(_Credentials):
    """Public self-signup. Always creates a DONOR: the role is not a field, so nobody can sign up as admin."""
    password: Password


class LoginRequest(_Credentials):
    password: Annotated[str, StringConstraints(min_length=1, max_length=128)]   # no length rule leaks at login


class StaffCreate(_Credentials):
    """Admin-only: an account for hospital / blood-bank / NGO staff, or another admin."""
    password: Password
    role: Literal["hospital", "blood_bank", "ngo", "admin"]
    organization_id: UUID | None = None

    @model_validator(mode="after")
    def staff_need_an_organization(self):
        # same rule as the CHECK in migration 0007; checked here too so the client gets a clear 422
        if (self.role != "admin") != (self.organization_id is not None):
            raise ValueError("hospital, blood_bank and ngo staff need an organization_id; admins must not have one")
        return self


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int                     # seconds


class Me(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    role: str
    organization_id: UUID | None
