"""How a donor is actually reached. One adapter per channel; the worker never knows which one it has."""
import logging
from typing import Protocol
from uuid import UUID

log = logging.getLogger("raktdoot.notifier")


class Notifier(Protocol):
    # Targets DONORS only (a donor_id). Nothing in this app sends anything to an organization: real facilities'
    # phones are shown to people, never dialled or messaged by the system (ADR 022); organizations have no email column.
    async def send(self, donor_id: UUID, request_id: UUID) -> None: ...


class LogNotifier:
    """Development adapter: writes a log line instead of an SMS. Ids only: never a name, a phone number or a group.

    Trade-off: one adapter. A real channel (SMS via an Indian DLT-registered provider, FCM push, email) is a class with
    the same send(); pick it from settings when the first one is integrated."""

    async def send(self, donor_id: UUID, request_id: UUID) -> None:
        log.info("notify donor=%s request=%s channel=log", donor_id, request_id)
