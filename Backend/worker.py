"""The outbox worker:  python worker.py   (its own container in docker-compose.yml)

Loop: lock a batch of due outbox rows (FOR UPDATE SKIP LOCKED, so N workers never take the same row), run each one's
handler inside a SAVEPOINT, mark it processed, or schedule a retry with exponential backoff, or dead-letter it.
Delivery is AT-LEAST-ONCE: if the process dies after sending but before COMMIT, the row is retried. Handlers are
therefore idempotent (docs/adr/017-outbox.md)."""
import asyncio
import logging
import signal
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from notifier import Notifier

log = logging.getLogger("raktdoot.worker")
Handler = Callable[[AsyncSession, dict, Notifier, datetime], Awaitable[None]]
# 12 attempts = 11 retries: 2+4+8+...+256 s, then 300 s three times (the cap): about 24 minutes before a message is
# dead-lettered, so an SMS provider outage of a few minutes delays donor alerts instead of losing them (B7).
MAX_ATTEMPTS = 12


def backoff(attempts: int) -> timedelta:
    """Wait after the Nth failed attempt: 2, 4, 8 ... 256 s, then capped at 5 minutes (from attempt 9).
    ponytail: no jitter; add it when many workers retry the same outage."""
    return timedelta(seconds=min(2 ** attempts, 300))


async def process_batch(
    sessionmaker: async_sessionmaker, handlers: dict[str, Handler], notifier: Notifier, now: datetime,
    batch_size: int = 20,
) -> int:
    """One pass. Returns how many rows were taken (0 = nothing due)."""
    async with sessionmaker() as session, session.begin():
        rows = (await session.execute(text("""
            SELECT id, topic, payload, attempts FROM outbox
            WHERE processed_at IS NULL AND dead_at IS NULL AND available_at <= :now
            ORDER BY available_at, id
            LIMIT :n
            FOR UPDATE SKIP LOCKED"""), {"now": now, "n": batch_size})).all()
        for row in rows:
            try:
                async with session.begin_nested():          # SAVEPOINT: one bad message cannot undo the others
                    await handlers[row.topic](session, row.payload, notifier, now)
                await session.execute(text("UPDATE outbox SET processed_at = :now, attempts = attempts + 1 WHERE id = :id"),
                                      {"now": now, "id": row.id})
            except Exception as exc:                        # any failure means "retry later", never a crash
                attempts = row.attempts + 1
                dead = attempts >= MAX_ATTEMPTS
                await session.execute(text("""
                    UPDATE outbox SET attempts = :a, last_error = :err,
                        available_at = :retry_at, dead_at = CASE WHEN :dead THEN CAST(:now AS timestamptz) END
                    WHERE id = :id"""),
                    {"a": attempts, "err": f"{type(exc).__name__}: {exc}"[:300], "retry_at": now + backoff(attempts),
                     "dead": dead, "now": now, "id": row.id})
                log.warning("outbox id=%s topic=%s attempt=%s %s", row.id, row.topic, attempts,
                            "DEAD-LETTERED" if dead else "will retry")
        return len(rows)


async def run_loop(sessionmaker: async_sessionmaker, handlers: dict[str, Handler], notifier: Notifier,
                   stop: asyncio.Event, expire, expiry_every: float, poll_seconds: float = 1.0) -> None:
    """The worker's loop. Nothing a pass raises ends it: a DB restart, a dropped connection or a bug in one pass is
    logged and retried with backoff, because a dead worker silently stops donor alerts AND hold expiry."""
    failures, next_expiry = 0, 0.0
    while not stop.is_set():
        # Hold expiry (ADR 021) rides the same loop, NOT the outbox: it is a periodic sweep, not a message.
        # If the worker is down, holds simply do not expire: fail safe (blood stays held, never double-promised).
        loop_time = asyncio.get_running_loop().time()
        if loop_time >= next_expiry:
            next_expiry = loop_time + expiry_every
            try:
                if released := await expire(sessionmaker, datetime.now(timezone.utc)):
                    log.info("released %s expired hold(s)", released)
            except Exception:                           # a failing sweep must not stop notifications
                log.exception("hold expiry pass failed; retrying next interval")
        # ponytail: polls every second. Upgrade: LISTEN/NOTIFY to wake instantly, poll only as the safety net.
        try:
            taken = await process_batch(sessionmaker, handlers, notifier, datetime.now(timezone.utc))
            failures = 0
        except Exception:                               # e.g. Postgres restarting: back off, never die
            failures += 1
            taken = 0
            log.exception("outbox pass failed (%s in a row); backing off", failures)
        if taken == 0:
            try:
                await asyncio.wait_for(stop.wait(), min(poll_seconds * 2 ** failures, 60))
            except TimeoutError:
                pass


async def run(poll_seconds: float = 1.0) -> None:
    from config import get_settings
    from db import make_engine, make_sessionmaker
    from modules.emergencies.service import HANDLERS, expire_holds
    from notifier import LogNotifier

    settings = get_settings()
    engine = make_engine(settings.database_url, 2, 0)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)    # finish the batch, then exit cleanly
        except (NotImplementedError, RuntimeError):                         # Windows: Ctrl+C still raises
            pass
    log.info("worker started")
    try:
        await run_loop(make_sessionmaker(engine), HANDLERS, LogNotifier(), stop, expire_holds,
                       settings.hold_expiry_interval_seconds, poll_seconds)
    finally:
        await engine.dispose()
        log.info("worker stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run())
