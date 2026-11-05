import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class BloodUnit(Base):
    __tablename__ = "blood_units"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    blood_bank_id: Mapped[uuid.UUID]            # FK to organizations: by id only, never a cross-module import
    blood_group: Mapped[str] = mapped_column(Text)
    component: Mapped[str] = mapped_column(Text, default="red_cells", server_default=text("'red_cells'"))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, default="AVAILABLE", server_default=text("'AVAILABLE'"))
