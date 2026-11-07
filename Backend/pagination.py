"""Keyset pagination cursor, shared by every list endpoint. The cursor is an opaque bookmark: (sort timestamp, row id)."""
import base64
from datetime import datetime
from uuid import UUID

from errors import Problem


def encode_cursor(at: datetime, row_id: UUID) -> str:
    """`at` is whatever timestamp the list is sorted by (created_at for donors, expires_at for stock)."""
    return base64.urlsafe_b64encode(f"{at.isoformat()}|{row_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        ts, row_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        when = datetime.fromisoformat(ts)
        if when.tzinfo is None:
            raise ValueError("naive timestamp")
        return when, UUID(row_id)
    except Exception:       # Trade-off: any malformed cursor is the client's fault, never a 500
        raise Problem(
            422, "invalid-cursor", "Invalid cursor",
            "The cursor is malformed. Start again from the first page.",
        ) from None
