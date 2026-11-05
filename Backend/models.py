import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, SmallInteger, Text, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())