# ADR 016: Allocating blood units to emergency requests

Status: accepted (2026-10-15). Drafted by Claude on Ansh's behalf: read it, own it.

## Context
Two hospitals post emergencies at the same moment, and one O- unit is left. Allocating it to both is the worst bug this
system can have: one patient's blood does not exist. "Check it's AVAILABLE, then mark it RESERVED" is a race: both
transactions can read AVAILABLE before either writes.

## Options
1. Optimistic: a `version` column, `UPDATE ... WHERE version = :seen`, retry on conflict. Fine at low contention, but in a
   disaster (many requests, few units) everyone retries at once.
2. `SELECT ... FOR UPDATE` (no SKIP LOCKED). Correct, but request 2 WAITS for request 1's lock, then finds the unit taken
   and returns nothing, even if the next unit on the shelf was free. Everyone queues behind the first row.
3. `SELECT ... FOR UPDATE SKIP LOCKED LIMIT n`. Each transaction locks the rows it picks; a concurrent transaction skips
   locked rows and takes the NEXT matching unit. No waiting chain, no double allocation.
4. A single allocation worker (one process, a queue). Serialises everything, adds infrastructure, and a single point of failure.

## Decision
Option 3, plus a database backstop.
- `modules/inventory/service.reserve`: one query picks up to `n` usable units, ordered by
  nearest bank → exact blood group before merely compatible (saves scarce O- for O- patients) → first-expired-first-out,
  `FOR UPDATE SKIP LOCKED`, then marks them RESERVED in the SAME transaction as the request and its allocation rows.
- Backstop: a partial UNIQUE index `allocations (blood_unit_id) WHERE released_at IS NULL`. Even code that forgets the lock
  cannot hold one unit in two live allocations; a cancelled (released) unit can be allocated again.
- The request row itself is locked (`FOR UPDATE`) for allocate / fulfil / cancel, so two clicks on the same request queue up.
- Status is a state machine (`modules/emergencies/state.py`): OPEN → ALLOCATED → FULFILLED, or → CANCELLED.
  Releasing / issuing units uses `WHERE status = 'RESERVED'`, so a replay changes nothing.
- `Idempotency-Key` on create: a timed-out retry replays the first answer instead of reserving a second set (ADR 007).
- Modules: emergencies reaches organizations and inventory ONLY through their `service.py` (ADR 011, enforced by
  `test_architecture.py`). Allocation rows snapshot `blood_bank_id` and `blood_group`, so reading a request never joins
  inventory's table.

## Evidence (2026-10-15)
- `test_twenty_hospitals_race_for_the_last_unit_and_exactly_one_wins`: 20 concurrent requests, 1 unit → 20 × 201,
  exactly 1 allocated, 19 OPEN, 0 errors.
- `test_twelve_requests_share_five_units_without_any_unit_twice`: every unit used exactly once.
- Break-it: with `.with_for_update(skip_locked=True)` removed, the same tests FAIL with
  `UniqueViolationError: allocations_one_live_per_unit`. The race is real, and the backstop catches it loudly
  (a 500, not a silent double booking). The lock is what turns those failures into clean 201s.

## Consequences
- ~~RESERVED units never time out~~ Superseded by [ADR 021](021-bank-confirmed-holds.md) (2026-10-19): every reservation
  is now a hold that the bank confirms within 30 minutes and the hospital collects within 6 hours; the worker releases
  expired holds, and a per-hospital cap (20 units) stops hoarding.
- Distance is straight-line (PostGIS), not road time. Fine for choosing a bank; ETA needs OSRM (Stage 5).
- Platelets are rejected: their compatibility policy differs per bank. Red cells and plasma only.
- The system PROPOSES units; the blood bank's cross-match is the clinical decision (CLAUDE.md 4, medical safety).
