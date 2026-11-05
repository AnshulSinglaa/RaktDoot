# ADR 017: Donor notifications through a transactional outbox

Status: accepted (2026-10-15). Drafted by Claude on Ansh's behalf: read it, own it.

## Context
"Ask the 20 nearest donors" has to reach donors exactly when the request really exists. The naive version is
`COMMIT; then call the SMS API`:
- crash between COMMIT and the call → the request exists, nobody is ever notified (lost message);
- call first, then the transaction rolls back → donors are asked about a request that does not exist (ghost message);
- the SMS API is slow or down → the hospital's HTTP request hangs or fails although the data was fine.

## Options
1. Call the provider inside the request. All three problems above.
2. A message broker (RabbitMQ, Kafka, SQS) published after COMMIT. Same dual-write problem: Postgres and the broker cannot share
   a transaction. Plus a new system to run, monitor and back up.
3. Transactional outbox in Postgres: the side effect is a ROW written in the SAME transaction as the business change. A worker
   delivers it afterwards.

## Decision
Option 3 (migration 0010, `outbox.py`, `worker.py`).
- `POST /v1/emergencies/{id}/notify-donors` writes one `outbox` row per matched donor, in the request's transaction, and answers
  202. Rolled back = no rows = nobody contacted (`test_a_rolled_back_request_leaves_no_message`).
- The worker (its own container) takes due rows with `FOR UPDATE SKIP LOCKED`, so N workers never take the same row; each message
  runs in a SAVEPOINT, so one poison message cannot undo the batch.
- Failure → `attempts + 1`, `available_at = now + 2^attempts s` (cap 5 min), `last_error`. After 5 attempts → `dead_at`
  (dead-letter: a human looks, nothing retries forever).
- Delivery is **at-least-once**: a crash after the send but before COMMIT retries the message. So the handler is idempotent:
  `donor_responses (request_id, donor_id)` is a primary key and the handler sends only when its INSERT actually inserted. Two
  "notify" clicks reach each donor once. A request cancelled meanwhile is skipped.
- The same table is the delivery log and the donor's answer (NOTIFIED → ACCEPTED / DECLINED).

## Evidence (2026-10-15)
- 10 tests in `test_outbox.py`: delivery, retry with backoff, dead-letter, poison message, double notify, cancelled request,
  rollback, donor answers, a donor who was never asked.
- Break-it: with `FOR UPDATE SKIP LOCKED` removed, two concurrent workers each took all 3 messages (6 taken, not 3).
- Live, in containers: a CRITICAL B- request for 6 units got 1 unit from stock, matched 9 eligible donors within 15 km,
  queued 9 messages; the worker container delivered all 9 within ~1 s.

## Consequences
- Up to ~1 s of latency (the worker polls). Upgrade: `LISTEN/NOTIFY` to wake it instantly, keeping the poll as a safety net.
- A duplicate SMS is possible after a crash at exactly the wrong moment. Accepted (a duplicate ask beats a lost one); a real
  provider gets the outbox id as its idempotency key to close most of that gap.
- The outbox table grows: processed rows need a retention purge (an operator job, like the audit purge).
- When would Kafka be justified? Replaying event history to new consumers, ordering per key across services, or sustained
  throughput Postgres cannot absorb (thousands of messages per second). None applies at this scale.

## Amendment 2026-10-24 (B7, B8)

- `MAX_ATTEMPTS` is 12, not 5: with 2^n s backoff capped at 5 min, retries now span about 24 minutes, so a short SMS
  provider outage delays donor alerts instead of dead-lettering them.
- The `donor.notify` handler re-checks the donor at send time (available, verified phone, consent on record, donation
  interval) and skips silently (info log, ids only) if they are no longer matchable. A skip is a processed message,
  not a retry.
