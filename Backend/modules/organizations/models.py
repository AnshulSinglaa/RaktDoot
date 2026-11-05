import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    city: Mapped[str] = mapped_column(Text)
    # plain numbers in, Postgres derives the indexed geography column `location` (migration 0009); never mapped here
    latitude: Mapped[float | None]
    longitude: Mapped[float | None]
    # 0013 (ADR 021)
    phone: Mapped[str | None] = mapped_column(Text)                         # +91 + 10 digits; public for blood banks
    stock_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))   # only the bank sets it
    auto_confirm_holds: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    has_blood_centre: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))   # hospitals
    # 0016 (ADR 022): provenance and ownership. Set by import-osm and seed-demo; no endpoint changes them yet
    osm_id: Mapped[str | None] = mapped_column(Text)                        # 'node/123': the import's idempotency key
    data_source: Mapped[str] = mapped_column(Text, default="manual", server_default=text("'manual'"))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ownership: Mapped[str] = mapped_column(Text, default="unknown", server_default=text("'unknown'"))
    ownership_inferred: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
