"""Live events without Redis: Postgres LISTEN/NOTIFY, fanned out in-process to SSE streams (docs/adr/018-live-status.md).

    write path:  any transaction  ->  SELECT pg_notify('emergency_events', '{"request_id": ...}')
                 Postgres delivers it ONLY IF that transaction COMMITS (a rolled-back change is never broadcast).
    read path:   ONE listening connection per API process  ->  EventHub  ->  one asyncio.Queue per open SSE stream.
Every API instance listens, so a change made on instance A reaches a viewer connected to instance B.
"""
import asyncio
import json
import logging
from collections import defaultdict
from contextlib import contextmanager, suppress
from typing import Iterator

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

CHANNEL = "emergency_events"
log = logging.getLogger("raktdoot.events")


async def publish(session: AsyncSession, request_id, kind: str) -> None:
    """Call INSIDE the transaction that made the change. The payload is ids only (NOTIFY payloads are logged by
    nothing, but they are visible to every listener on the server)."""
    await session.execute(text("SELECT pg_notify(:channel, :payload)"),
                          {"channel": CHANNEL, "payload": json.dumps({"request_id": str(request_id), "kind": kind})})


class EventHub:
    def __init__(self, retry_seconds: float = 1.0) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._conn: asyncpg.Connection | None = None
        self._lost = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._retry = retry_seconds

    async def start(self, database_url: str) -> None:
        self._url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        await self._connect()                       # fail fast at boot: an API without live updates should not start
        self._task = asyncio.create_task(self._reconnect_forever())

    async def _connect(self) -> None:
        conn = await asyncpg.connect(self._url)
        await conn.add_listener(CHANNEL, self._on_notify)
        conn.add_termination_listener(lambda _c: self._lost.set())     # DB restart, pg_terminate_backend, network
        self._conn = conn
        self._lost.clear()

    async def _reconnect_forever(self) -> None:
        """A dropped LISTEN connection is re-opened with backoff (1, 2, 4 ... 30 s). Events committed while it was
        down are gone (NOTIFY is not stored), so every viewer is nudged once: each stream re-sends a full snapshot."""
        while True:
            await self._lost.wait()
            failures = 0
            while self._lost.is_set():
                try:
                    await self._connect()
                except Exception as exc:                    # refused, starting up, timeout: keep trying
                    failures += 1
                    log.warning("LISTEN reconnect failed (%s in a row): %s", failures, exc)
                    await asyncio.sleep(min(self._retry * 2 ** (failures - 1), 30))
            log.info("LISTEN connection restored; nudging %s viewer(s) to re-snapshot", self.viewers())
            for request_id in list(self._subscribers):
                self._on_notify(None, 0, CHANNEL, json.dumps({"request_id": request_id, "kind": "reconnected"}))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._conn is not None:
            await self._conn.close()

    def _on_notify(self, _conn, _pid, _channel, payload: str) -> None:
        event = json.loads(payload)
        for queue in list(self._subscribers.get(event["request_id"], ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass        # a slow viewer misses a nudge, not data: the next event re-sends the full state

    @contextmanager
    def subscribe(self, request_id: str) -> Iterator[asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        self._subscribers[request_id].add(queue)
        try:
            yield queue
        finally:
            self._subscribers[request_id].discard(queue)
            if not self._subscribers[request_id]:
                del self._subscribers[request_id]

    def viewers(self) -> int:
        return sum(len(s) for s in self._subscribers.values())
