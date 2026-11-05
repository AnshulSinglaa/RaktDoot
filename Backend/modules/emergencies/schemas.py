from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from domain import BloodGroup


class EmergencyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")       # hospital_id comes from the token, never the body (no spoofing)

    blood_group: BloodGroup
    component: Literal["red_cells", "plasma"] = "red_cells"      # platelets: no compatibility rule yet (domain.py)
    units_needed: int = Field(ge=1, le=20)
    urgency: Literal["CRITICAL", "HIGH", "NORMAL"] = "HIGH"
    # the ward's own reference. The pattern is the guard: no dots, commas or apostrophes, so "Mr. R. Sharma" fails;
    # a bare name can still pass, hence: never audited, never public, nulled when the request is fulfilled or cancelled
    hospital_ref: str | None = Field(None, pattern=r"^[A-Za-z0-9 /#-]{1,30}$",
                                     description="IP or bed number. Never the patient's name.")


class AllocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    blood_unit_id: UUID
    blood_bank_id: UUID
    blood_group: BloodGroup
    allocated_at: datetime


HoldStatus = Literal["PENDING", "CONFIRMED", "OVERDUE", "ISSUED", "REJECTED", "EXPIRED", "CANCELLED", "RELEASED"]


class HoldRead(BaseModel):
    """One blood bank's part of a request, per state (ADR 021). Released holds stay visible: the hospital sees WHY."""
    blood_bank_id: UUID
    bank_name: str
    bank_phone: str | None                  # so the hospital can call the bank about a PENDING hold
    bank_is_demo: bool                      # osm/synthetic facility: a REAL public number, a synthetic request (R2)
    units: int
    # OVERDUE: confirmed, pickup deadline passed, still held until the hospital or the bank resolves it.
    # ISSUED: collected, the units left the bank for good. RELEASED: the bank took back an OVERDUE hold.
    status: HoldStatus
    expires_at: datetime                    # live: the deadline. Released: the deadline it had.


class HoldReject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # not_in_stock: the units are not on the shelf (the record was wrong) -> DISCARDED.
    # cannot_supply: the units exist but the bank cannot release them now -> back to AVAILABLE.
    reason: Literal["not_in_stock", "cannot_supply"]


class CloseRequest(BaseModel):
    """fulfil / cancel must say what physically happened: which banks' units were collected (ADR 021 revision 2).
    Required, even if empty: a close that does not answer is how collected blood used to come back as stock (F1)."""
    model_config = ConfigDict(extra="forbid")

    collected_from: list[UUID] = Field(max_length=20)

    @field_validator("collected_from")
    @classmethod
    def unique(cls, v: list[UUID]) -> list[UUID]:
        if len(set(v)) != len(v):
            raise ValueError("list each blood bank once")
        return v


class BankHold(BaseModel):
    """What a blood bank sees: the request's needs and its own hold. No patient data exists to show, and no other
    bank's hold. GET /holds lists live ones; confirm / reject / release return the one they acted on."""
    request_id: UUID
    code: str                               # what the ward wrote on the requisition
    hospital_ref: str | None                # IP/bed number, if the ward gave one
    hospital_name: str
    hospital_phone: str | None
    hospital_is_demo: bool                  # osm/synthetic facility: do not offer a tel: link about a demo request (R2)
    blood_group: BloodGroup
    component: str
    units: int
    urgency: str
    status: HoldStatus
    expires_at: datetime


class EmergencyRead(BaseModel):
    id: UUID
    code: str
    hospital_ref: str | None
    hospital_id: UUID
    blood_group: BloodGroup
    component: str
    units_needed: int
    units_allocated: int                        # live holds, collected ones included
    units_collected: int = 0                    # of those, units that left the bank (ISSUED)
    urgency: str
    status: str
    created_at: datetime
    allocations: list[AllocationRead]           # live holds, unit by unit (kept for existing clients)
    holds: list[HoldRead] = []
    cap_reached: bool = False                   # only on create / allocate: fewer units held because of the hospital cap


class DonorMatch(BaseModel):
    """Minimal by design (DPDP data minimisation): no email, no phone, no coordinates, no exact distance.
    Contact happens through the platform's notification, so the hospital never needs the donor's details."""
    donor_id: UUID
    first_name: str
    blood_group: BloodGroup
    distance_band: Literal["under 2 km", "2-5 km", "5-10 km", "10-25 km", "25+ km"]


class NotifyResult(BaseModel):
    queued: int                     # donors queued for a notification (delivered by the worker, at least once)


class DonorReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accept: bool


class ResponseRead(BaseModel):
    donor_id: UUID
    first_name: str
    blood_group: BloodGroup
    status: Literal["NOTIFIED", "ACCEPTED", "DECLINED"]
    responded_at: datetime | None


class InboxItem(BaseModel):
    request_id: UUID
    hospital_name: str
    hospital_city: str
    blood_group: BloodGroup
    component: str
    urgency: str
    request_status: str
    my_status: Literal["NOTIFIED", "ACCEPTED", "DECLINED"]
    notified_at: datetime
