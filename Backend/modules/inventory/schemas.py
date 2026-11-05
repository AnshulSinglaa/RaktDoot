from enum import Enum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict

from domain import BloodGroup  # the shared vocabulary (top-level domain.py), not another module


class Component(str, Enum):         # mirrors the CHECK in migration 0001
    red_cells = "red_cells"
    plasma = "plasma"
    platelets = "platelets"


class UnitStatus(str, Enum):        # mirrors the CHECK (0001, DISCARDED added in 0013). Moves only through hold rules.
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    ISSUED = "ISSUED"
    EXPIRED = "EXPIRED"
    DISCARDED = "DISCARDED"         # the bank rejected a hold as "not in stock": the record no longer matches a shelf


class BloodUnitCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")       # `status` cannot be set at creation: a new unit is always AVAILABLE

    blood_bank_id: UUID
    blood_group: BloodGroup
    component: Component = Component.red_cells
    collected_at: AwareDatetime
    expires_at: AwareDatetime


class BloodUnitRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    blood_bank_id: UUID
    blood_group: BloodGroup
    component: Component
    collected_at: AwareDatetime
    expires_at: AwareDatetime
    status: UnitStatus


class BloodUnitPage(BaseModel):
    items: list[BloodUnitRead]
    next_cursor: str | None


class StockLine(BaseModel):
    """Usable (AVAILABLE, unexpired) units of one group/component at one bank: what the stock-take form starts from."""
    model_config = ConfigDict(from_attributes=True)

    blood_group: BloodGroup
    component: Component
    units: int

