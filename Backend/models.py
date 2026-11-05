import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, SmallInteger, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class Donor(Base):
    __tablename__ = "donors"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text)
    age: Mapped[int] = mapped_column(SmallInteger)
    blood_group: Mapped[str] = mapped_column(Text)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    last_donated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    city: Mapped[str | None] = mapped_column(Text)          # added in migration 0002, nullable (expand step)
    user_id: Mapped[uuid.UUID | None]                       # 0007: the owning account; NULL = walk-in donor (camp)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))    # 0007: when consent was given
    # 0009: APPROXIMATE location (rounded to ~1 km by the schema). Postgres derives the indexed `location` column.
    latitude: Mapped[float | None]
    longitude: Mapped[float | None]
    sex: Mapped[str | None] = mapped_column(Text)                   # 0011: drives the donation interval
    phone: Mapped[str | None] = mapped_column(Text)                 # 0011: E.164, unique
    # 0011: set by staff who saw the donor in person
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
