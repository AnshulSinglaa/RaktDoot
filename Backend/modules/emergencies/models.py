import uuid
from datetime import datetime

from sqlalchemy import DateTime, SmallInteger, Text
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class EmergencyRequest(Base):
    __tablename__ = "emergency_requests"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    hospital_id: Mapped[uuid.UUID]
    created_by: Mapped[uuid.UUID]
    blood_group: Mapped[str] = mapped_column(Text)
    component: Mapped[str] = mapped_column(Text)
    units_needed: Mapped[int] = mapped_column(SmallInteger)
    urgency: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    code: Mapped[str] = mapped_column(Text)                         # 0017: written on the requisition, UNIQUE
    hospital_ref: Mapped[str | None] = mapped_column(Text)          # 0017: IP/bed number, never a name, never audited


class Allocation(Base):
    __tablename__ = "allocations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID]
    blood_unit_id: Mapped[uuid.UUID]
    blood_bank_id: Mapped[uuid.UUID]
    blood_group: Mapped[str] = mapped_column(Text)
    allocated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 0013, a HOLD (ADR 021): PENDING until the bank confirms, then CONFIRMED until pickup; expires_at is the deadline
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(Text)        # rejected | expired | cancelled | released (0014)
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))   # 0015: units left the bank, ISSUED
