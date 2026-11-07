# ADR 021: Bank-confirmed holds with expiry, a per-hospital cap, and stock freshness

Status: accepted (2026-10-19), REVISED the same day after code review (see "Revision: confirmed holds are never
auto-released" below), and again on 2026-10-20 after the field review ("Revision 2: hold outcomes", which REPLACES the
fulfil, cancel and excluded-banks rules of the Decision section). Supersedes the "RESERVED units never time
out" consequence of ADR 016 for PENDING holds only.

## Context
ADR 016 made allocation race-free: a unit is never promised twice. But a promise made by the database is not blood on a
shelf. Three problems remained:
1. **The stock list can be wrong.** A unit registered last week may have been used, broken or never entered correctly.
   The hospital saw "3 of 3 reserved" and sent a driver for blood that did not exist.
2. **Reservations never ended.** A hospital that never fulfilled or cancelled kept units RESERVED forever.
3. **Hoarding.** One hospital could post ten requests "just in case" and lock a city's whole O- stock.

## Options
- **(a) Immediate reserve (status quo).** Fast and simple; trusts the stock list blindly; no expiry.
- **(b) Bank confirms each reservation.** The people at the shelf say "yes, set aside". Correct, but a bank that never
  opens the app freezes stock forever.
- **(c) Bank confirms, with expiry (chosen).** A hold is PENDING until the bank confirms (30 min), then CONFIRMED until the
  hospital collects (6 h). The confirm deadline passing releases the units; the pickup deadline passing marks the hold
  OVERDUE for a human to resolve (revision below). Plus a per-hospital cap and an opt-in auto-confirm for banks whose
  stock list is reliable.

## Decision
- **A hold is the existing `allocations` row** (no new table). State is derived from columns (migration 0013):
  PENDING = live, `confirmed_at` NULL; CONFIRMED = live, `confirmed_at` set, deadline ahead; OVERDUE = live,
  `confirmed_at` set, deadline passed; ISSUED = live on a FULFILLED request (handed over); released = `released_at` set
  with `release_reason` in (rejected, expired, cancelled, released). Migration 0014 makes "released iff it has a reason"
  a CHECK (`allocations_released_has_reason`).
- **Windows** (config, product defaults to tune with real banks, NOT clinical or legal limits): `hold_confirm_minutes=30`,
  `hold_pickup_hours=6`. `organizations.auto_confirm_holds` (default false): new holds start CONFIRMED with the pickup window.
- **Who confirms or rejects:** only `blood_bank` staff whose organization is the hold's bank. Not an admin: an admin is
  not at the shelf, and "the blood is there" is a physical fact only the bank can attest. Another bank gets 404.
  Unit of action = (request, bank).
- **Reject** `{"reason": "not_in_stock" | "cannot_supply"}`: not_in_stock -> units DISCARDED (new unit status: the
  record does not match a shelf, so nobody is promised them again); cannot_supply -> units back to AVAILABLE. Either way
  the hold is released as `rejected` and that bank is never asked again for that request.
- **A lost hold** (rejected, expired or released by the bank) puts units back with a guarded `RESERVED -> AVAILABLE`, and the request is
  recomputed (`ALLOCATED -> OPEN` added to the state machine). No automatic re-allocation: the hospital clicks "check
  blood banks again" (a Trade-off comment names the upgrade).
- **Fulfil** only when every live hold is CONFIRMED (OVERDUE counts: it is confirmed), else `409 holds-unconfirmed`.
  **Cancel** releases PENDING, CONFIRMED and OVERDUE holds as `cancelled`.
- **Banks that rejected OR let a hold expire** are never asked again for that request (`excluded_banks`). Asking a bank
  that ignored the request for 30 minutes would just start the same silence again. A bank that released an OVERDUE hold
  may be asked again (it did confirm once; the hospital was the one that did not come).
- **Concurrency:** every hold mutation (confirm, reject, expire, cancel, fulfil, allocate) runs under the request row lock,
  and every hold UPDATE is conditional (`WHERE released_at IS NULL AND confirmed_at IS NULL [AND expires_at > now]`).
- **Expiry job:** `expire_holds(sessionmaker, now)` in the worker loop every 30 s (not the outbox: a periodic sweep is not
  a message). PENDING holds only. It lists requests with an expired PENDING hold, most overdue first (`GROUP BY
  request_id ORDER BY min(expires_at) LIMIT 100`), locks each `FOR UPDATE SKIP LOCKED` (busy -> next tick), runs ONE
  conditional `UPDATE ... WHERE released_at IS NULL AND confirmed_at IS NULL AND expires_at <= :now RETURNING`
  (exactly-once), releases units, recomputes, writes one `hold.expired` audit row per (request, bank) and publishes
  `hold_expired`. FULFILLED and CANCELLED requests are never touched. A hold expires at exactly its deadline (`<=`).
- **OVERDUE holds are resolved by a person:** the hospital fulfils (it collected) or cancels (it will not), or the bank
  calls `POST /v1/emergencies/{id}/holds/release` (that bank's staff only, never an admin; 409 `hold-not-overdue` unless
  the hold is CONFIRMED and past its pickup deadline on an OPEN/ALLOCATED request; another bank 404). Release = units
  RESERVED -> AVAILABLE, `release_reason='released'`, request recomputed, `hold.released` audited, `hold_released`
  published; a repeat click returns the same released hold (idempotent). OVERDUE holds keep counting against the
  hospital cap.
- **Bank actions return only the bank's part:** confirm, reject and release return a `BankHold` (request facts, this
  bank's units, status, deadline), the same shape as `GET /holds`, never other banks' holds or unit ids.
- **Cap:** `max_held_units_per_hospital=20` live held units across the hospital's OPEN/ALLOCATED requests. Allocation
  holds `min(missing, cap - held)`; an over-cap request is still created (201) with `cap_reached: true` and `capped` in
  the audit row. Races between one hospital's own requests are serialised with
  `pg_advisory_xact_lock(hashtext('hospital-cap:' || hospital_id))` (hash collisions only make two hospitals queue
  briefly; per-hospital overrides deferred, upgrade = a nullable column).
- **Freshness, phone, own centre:** `organizations.stock_confirmed_at` moves ONLY when that bank's staff confirm their
  stock (registering a unit does not count). Since 2c this is a stock-take, a real count that also takes missing bags
  off the list: see [ADR 023](023-stock-take.md); the bare `POST /organizations/{id}/stock-confirmed` is gone. `organizations.phone` (+91 + 10 digits, landlines allowed,
  admin sets it) is public for banks. `has_blood_centre` (hospitals only, DB CHECK + 422). Public nearest-banks returns
  `phone` and `stock_confirmed_at`; ordering stays nearest-first.

## Consequences
- **Banks must use the app** (or auto-confirm), or their holds expire and the hospital sees units "disappear". Mitigated:
  the hospital sees each bank's status, deadline and phone number (call them); auto-confirm for reliable banks. The README
  must not imply that a hold is guaranteed stock.
- **`not_in_stock` destroys a stock record** (DISCARDED). Only the owning bank can do it, and it is audited.
- **The windows and the cap are guesses.** They are config, not code, and should be revisited with real bank data.
- **Worker down -> holds do not expire.** Fail safe: blood stays held (never double-promised); the backlog is released on
  the next pass after restart. The loop itself survives DB errors (log, back off, continue) and compose restarts it.
- **OVERDUE holds need a human.** A hospital that forgets to press "received" leaves units held until the bank releases
  them; the bank's `/holds` list shows them as OVERDUE. That is slower than auto-release, and that is the point.
- **0013 backfill:** live allocations from before 0013 became CONFIRMED with a fixed 6 h pickup window from the upgrade
  (the dev DB had 4). 0013's downgrade fails on purpose if any unit is DISCARDED.
- Load: the k6 lifecycle cancels every request, and 8 load hospitals each hold at most ~15 units at once, so the cap does
  not distort ADR 019's numbers.

## Revision (2026-10-19): confirmed holds are never auto-released
- **Context:** the first version also expired CONFIRMED holds after the 6 h pickup window and put the units back to
  AVAILABLE. Code review found the hole: a hospital that collected and transfused the units but forgot to press
  "received" would get its blood back on the public stock list. That is phantom stock, the exact double promise ADR 016
  exists to prevent, and this time the second hospital would drive to an empty shelf.
- **Options:** (a) keep auto-release (simple, wrong in the forgetful-hospital case); (b) auto-mark them ISSUED (guesses
  the other way: lies when the hospital really never came); (c) mark OVERDUE and let the two parties who know the truth
  resolve it (chosen).
- **Decision:** the worker expires PENDING holds only; CONFIRMED-past-deadline = OVERDUE, shown in `holds()`, `/holds`
  and the SSE snapshot; resolved by fulfil, cancel or the bank's explicit release (above).
- **Consequence:** units can stay RESERVED for longer than 6 h. Accepted: "held too long" is recoverable with a phone
  call; "promised blood that is already in a patient" is not. Upgrade if banks complain: a reminder notification (outbox)
  to both parties when a hold turns OVERDUE.

## Evidence (2026-10-19)
- `pytest --cov --cov-fail-under=85`: 285 tests pass, coverage 93.22%, ruff clean. Hold tests are in `test_holds.py`:
  `test_a_new_hold_is_pending_for_30_minutes`, `test_an_auto_confirm_bank_starts_confirmed_for_6_hours`,
  `test_the_banks_own_staff_confirm_once_and_it_is_audited_and_published`, `test_nobody_else_can_confirm_and_nothing_changes`,
  `test_an_expired_or_released_hold_cannot_be_confirmed`, `test_a_rejected_hold_frees_the_request_and_that_bank_is_never_asked_again[2]`,
  `test_expiry_releases_each_hold_exactly_once_even_with_two_workers`, `test_a_worker_with_a_stale_list_releases_nothing_twice`,
  `test_a_confirmed_hold_expires_after_the_pickup_window_and_closed_requests_are_never_touched`,
  `test_confirm_racing_expiry_never_ends_in_both`, `test_two_banks_confirm_their_holds_on_one_request_at_once`,
  `test_cancel_racing_confirm_puts_every_unit_back_exactly_once`, `test_fulfil_waits_for_every_bank`,
  `test_one_hospital_cannot_hold_more_than_20_units_even_in_parallel`; SSE: `test_live.py::test_the_stream_shows_each_banks_hold_and_its_confirmation_live`;
  migration: `test_migrations.py::test_0013_backfills_live_allocations_as_confirmed_and_refuses_to_forget_discarded_units`.
  The ADR 016 test `test_twenty_hospitals_race_for_the_last_unit_and_exactly_one_wins` is unchanged and green.
- The confirm-vs-expiry race really races: three runs gave 8/0, 7/1 and 6/2 confirm-won / expiry-won, every one consistent.
- Break-its (code changed, test run, code restored):
  - advisory lock removed -> `test_one_hospital_cannot_hold_more_than_20_units_even_in_parallel` red: **50 units held, not 20** (twice).
  - `released_at IS NULL` removed from the expiry UPDATE -> `test_a_worker_with_a_stale_list_releases_nothing_twice` red
    (`[2] == [0]`: the second worker released the same holds again). The two-worker test alone did NOT catch it (the
    second worker usually skips the locked row), which is why the deterministic stale-list test exists.
  - `released_at IS NULL ... expires_at > :now` removed from confirm -> `test_an_expired_or_released_hold_cannot_be_confirmed`
    red (200 instead of 409; a cancelled hold came back to life).
- Two seeds on emptied databases in separate processes gave identical results (12 banks: 4 auto-confirm, 3 never
  stock-confirmed). README demo on a fresh seed: Bhopal General Hospital 1 posts A- x 3 -> 2 units held PENDING at
  Bhopal Blood Centre 2, request OPEN -> the bank confirms (200) -> notify donors queues 4 -> the bank adds 1 A- unit ->
  allocate again: 3 of 3, ALLOCATED, the new unit PENDING -> confirmed -> fulfil: FULFILLED.
- k6 smoke (`emergency.js`, 5 VUs, 30 s, inside the compose network): 4,910 requests, 0 failed, all checks passed. Not a
  throughput claim: ADR 019 (2026-10-15) stays the load reference.

## Evidence for the revision (2026-10-19, later)
- Full suite: 306 passed, coverage 94% (TOTAL 1936 statements, 116 missed), ruff clean. Dev DB migrated to 0014 via
  `docker compose run --rm migrate`; api and worker rebuilt with `restart: unless-stopped`.
- New tests: `test_holds.py::test_the_worker_never_releases_a_confirmed_hold_it_turns_overdue_and_keeps_counting`,
  `test_even_a_worker_told_to_look_at_it_never_expires_a_confirmed_hold`,
  `test_closed_requests_are_never_touched_and_a_fulfilled_hold_reads_issued`,
  `test_the_bank_releases_an_overdue_hold_once_and_only_its_own`,
  `test_release_is_refused_for_a_pending_hold_and_a_closed_request`,
  `test_a_bank_that_let_its_hold_expire_is_not_asked_again`, `test_the_expiry_job_takes_the_most_overdue_requests_first`,
  `test_a_bank_action_returns_only_that_banks_part`; `test_migrations.py::test_0014_requires_a_reason_on_every_released_hold_and_refuses_to_forget_a_bank_release`.
- Break-it: `confirmed_at IS NULL` removed from the expiry UPDATE -> the first overdue test stayed GREEN (the listing
  query also filters), so the stale-list test `test_even_a_worker_told_to_look_at_it...` was added; it goes red without
  the guard and green with it.
- Live drill: `docker compose restart db` under the running stack -> worker logged "outbox pass failed (1, 2, 3 in a
  row); backing off" and kept running; the API logged "LISTEN reconnect failed ... starting up" then stopped failing;
  afterwards `pg_stat_activity` showed 4 LISTEN connections from `lifelink_app` (one per uvicorn worker).

## Revision 2 (2026-10-20): hold outcomes (after a field review; migration 0015)
- **Context:** the system only knew what the screen was told, and the screen could only say "received" (all or nothing,
  every bank confirmed) or "cancel" (everything back). Real life: the nurse collects from Bank A while Bank B never
  answered; the bank issues at the counter without opening the app; a request needing 3 closes with 2. "Cancel" then put
  units that were in a patient back on the public stock list, and an expired PENDING hold returned units the bank had
  issued by hand. Same phantom-stock promise this ADR exists to prevent. Separately, a bank that let a hold expire at
  night was excluded for good, and inside one bank O- was given to a B+ patient whenever it expired first.
- **Options:** (a) keep all-or-nothing close and add warnings (still lies when the nurse picks the wrong button);
  (b) per-unit "issued" with bag numbers (correct, but banks do not carry bag numbers in our data yet);
  (c) a COLLECTED state per (request, bank), set by either side, that nothing can release, and a close that must say
  which banks were collected from (chosen).
- **Decision:**
  - `allocations.collected_at` (0015). Collected = live + confirmed + units ISSUED. CHECK `allocations_collected_is_live`
    (`collected_at IS NULL OR (released_at IS NULL AND confirmed_at IS NOT NULL)`) makes "collected and released" a
    database error, not just a code path we hope nobody writes. Backfill: live holds of FULFILLED requests are collected
    at `updated_at`; released rows are never touched.
  - Hospital: `POST /v1/emergencies/{id}/holds/{bank_id}/collected` (hospital role only, never admin: the ward attests
    receipt). Bank: `POST /v1/emergencies/{id}/holds/issued` (its own staff only). Both work on PENDING, CONFIRMED and
    OVERDUE holds of an OPEN/ALLOCATED request, mark units `RESERVED -> ISSUED`, audit `hold.collected {blood_bank_id,
    units, by}`, publish `hold_collected`, and are idempotent (a repeat is 200, no second audit row). A hold already
    released (e.g. expired) is `409 hold-not-live`: its units may already be promised to someone else, so the nurse must
    call the bank instead of the system guessing.
  - Every release path skips collected rows: `release_all` and `release_overdue` add `collected_at IS NULL`; `confirm`,
    `reject` and `expire` already require `confirmed_at IS NULL`, which a collected row never has.
  - `fulfil` and `cancel` require a body `{collected_from: [bank ids]}` (unique, max 20, `extra=forbid`). Fulfil (now
    allowed from OPEN: partly fulfilled) marks the listed banks collected (422 `unknown-bank` if one has no live hold),
    needs at least one collected unit (409 `nothing-collected`), and releases every other live hold as `cancelled`.
    Cancel is 409 `units-collected` if the body lists a bank or any hold is already collected. The old 409
    `holds-unconfirmed` is gone: a PENDING hold that was physically collected is the normal night-shift case.
  - `excluded_banks` = explicit `rejected` only. An expired bank is asked again on "check blood banks again" (the
    hospital calls first; the UI says so).
  - Stewardship inside one bank: `domain.stewardship_rank` (red cells: exact, same Rh sign, other compatible, O- last for
    anyone not O-; plasma: exact, then the rest) replaces "exact group first" in the reserve ORDER BY, AFTER bank position.
  - Cap: collected units do not count (they are in a patient, not hoarded). `EmergencyRead.units_collected` added.
- **Why not cross-bank preference yet:** sending a B+ patient's driver 20 km for B+ instead of 2 km for O+ is a clinical
  trade-off (time vs stewardship). Inside one bank there is no trade-off: same distance, already-compatible units. The
  cross-bank rule waits for a medical officer's answer.
- **Consequences:** breaking API change to fulfil/cancel (the frontend ships with it; k6 and loadtest.py updated).
  Collected is irreversible by design: a wrong "collected" click removes real stock until the bank registers the bag
  again. Accepted: phantom stock sends a family to an empty shelf; missing stock costs one phone call. Downgrade
  of 0015 is lossy (collected on an open request reads CONFIRMED again) but never resurrects blood: units stay ISSUED.


## Evidence for revision 2 (2026-10-20)
- Full suite: 351 passed, coverage 94.28% (`--cov-fail-under=90`), ruff clean. Dev DB migrated 0014 -> 0015 with
  `docker compose run --rm migrate` (backfill touched 0 rows: the dev DB had no FULFILLED request); api and worker rebuilt.
- New tests: `test_hold_outcomes.py` (collected in every hold state, bank `issued`, roles, close with answer, cap,
  collected-vs-expiry race, CHECK break-it, stewardship order), `test_stewardship.py`, `test_migrations.py::test_0015_...`,
  `test_live.py::test_the_stream_shows_a_collected_hold_as_issued_live`.
- Break-its (code changed, target test run, code restored): 11 of 11 mutations killed: `collected_at IS NULL` removed
  from `release_all`, `release_overdue`, `held_by_hospital`, `holds_of_bank`; ISSUED derivation removed; excluded_banks
  back to `rejected, expired`; cancel's collected guard, `nothing-collected`, `unknown-bank`, `hold-not-live` removed;
  stewardship back to "exact first". The two `release_*` mutations failed on the CHECK constraint itself: the database
  backstop works.
