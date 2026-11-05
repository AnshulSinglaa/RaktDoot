# ADR 018: Live request status with SSE and Postgres LISTEN/NOTIFY

Status: accepted (2026-10-15). Drafted by Claude on Ansh's behalf: read it, own it.

## Context
A hospital watching an emergency must see units reserved, donors notified and donors answering as it happens, not on refresh.
Updates flow one way (server → screen). Scale today: tens of open dashboards, not thousands.

## Options (transport)
1. Polling every N s. Simple; wasteful, and up to N s late.
2. WebSockets. Two-way, but we only need one way; more protocol, more proxy configuration.
3. Server-Sent Events (SSE). One-way over plain HTTP, proxy- and HTTP/2-friendly, trivial to reconnect.

## Options (fan-out: how does a change on one API process reach a stream on another?)
a. Redis pub/sub. The standard answer at scale; one more system to run.
b. Postgres `LISTEN/NOTIFY`. Already there; `pg_notify` inside a transaction is delivered ONLY on COMMIT.

## Decision
SSE (3) + Postgres LISTEN/NOTIFY (b).
- Every change (`created`, `allocated`, `fulfilled`, `cancelled`, `notified` by the worker, `response` from a donor) calls
  `events.publish()` inside its transaction: `SELECT pg_notify('emergency_events', '{"request_id": ...}')`. Ids only.
- One `LISTEN` connection per API process (`events.EventHub`, outside the pool) fans out to one bounded `asyncio.Queue` per
  open stream. Every instance listens, so a change made on instance A reaches a viewer on instance B.
- Events carry no state: each one makes the stream send a FULL snapshot (request + responses). So a slow viewer can drop
  events (full queue → drop) and reconnects need no replay: the first frame is always the truth.
- `GET /v1/emergencies/{id}/events` uses no Session dependency: the auth check borrows a pooled connection and returns it;
  each snapshot borrows one for milliseconds. A yield-dependency session would be held for the whole stream: 10 dashboards
  would exhaust a pool of 10. Proven: `app.state.engine.pool.checkedout() == 0` while a stream is open.
- Heartbeat comment every 15 s (proxies close idle connections). The stream ENDS itself at FULFILLED / CANCELLED.
- Browser: `fetch()` + a stream reader, not `EventSource`, because EventSource cannot send an Authorization header and a
  token in the URL leaks into logs. Reconnect with backoff 1 → 15 s.

## Evidence (2026-10-15)
- `test_live.py`: real uvicorn + real HTTP client (Starlette's TestClient buffers streams and cannot test SSE); a rolled-back
  `pg_notify` never arrives; a slow viewer drops nudges instead of blocking the listener.
- Browser click-through on the compose stack: hospital posted A- × 3 (1 reserved) → "Ask nearby donors" → 9 donors appeared
  as notified live → a donor accepted in another tab → the hospital tab showed "accepted: 1" live → a blood bank added 2 A-
  units → "check again" → 3/3 reserved → "Units received" → FULFILLED and the stream closed.

## Consequences
- NOTIFY payloads are capped at 8000 bytes and share one Postgres-wide queue. Fine for tiny id-only payloads at this scale.
  Upgrade at Stage 5 (thousands of viewers, live GPS): Redis pub/sub or a dedicated gateway tier (CLAUDE.md 6.15).
- ~~If the LISTEN connection drops, live updates stop until the API restarts.~~ Fixed 2026-10-19 (the frontend agent saw
  exactly this after a Postgres restart): `EventHub` re-opens the LISTEN connection with backoff (1 s doubling to 30 s),
  woken by asyncpg's termination listener, then nudges every subscriber once. NOTIFY is not stored, so events committed
  while it was down are lost; the nudge makes every open stream re-send a full snapshot, so no viewer stays stale.
  Proven by `test_live.py::test_live_updates_survive_a_killed_listen_connection` (pg_terminate_backend on the
  listener). Remaining gap: a half-open TCP connection (no FIN, e.g. a network partition) is only noticed when TCP gives
  up; upgrade = a periodic `SELECT 1` on the listener.
- A stream is authorised at connect and can outlive its 15-minute token. Upgrade: end the stream at the token's expiry.
