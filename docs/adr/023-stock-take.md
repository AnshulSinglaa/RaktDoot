# ADR 023: Stock-take: stock leaves the list by count

Date: 2026-10-20. Status: accepted. Item 2c (backend). Migration 0017 (request code, same item). Supersedes the
freshness bullet of ADR 021.

## Context
A blood bank's stock list on Rakt Doot drifts from its shelf: bags are issued at the counter without the app, broken,
quarantined or discarded. Field review F2 / decision D68: the list only ever grew (registering a unit), and the only
way to say "the list is right" was a bare "Stock is accurate" click (`POST /organizations/{id}/stock-confirmed`) that
changed no unit. A tired technician could click it with 40 bags listed and 25 on the shelf, and families would see
"stock confirmed 2 minutes ago" next to 15 bags that do not exist. Freshness meant "someone clicked", not "someone
counted".

## Options
1. **Per-unit removal** (discard this bag, issue that bag). Exact, but needs a bag number on every unit to pick the
   right row (item 9) and is slow: 40 rows to tap at a shift change.
2. **Count reconciliation (chosen).** The technician enters, per group and component, how many bags are really on the
   shelf (not the ones set aside for Rakt Doot holds). The list follows the count; freshness = the time of the last
   count.
3. **Import from e-RaktKosh** (E1). The national system is the real ledger, but it has no public API (D80). Later.

## Decision
- `POST /v1/organizations/{id}/stock-take` `{counts: [{blood_group, component, units >= 0}]}`, `require("blood_bank")`,
  that bank's own staff only (another bank, a hospital, an admin: 403; anonymous: 401). An admin cannot count: a count
  is a physical fact only the people at the shelf can state. Duplicate pairs and extra fields are 422.
- One transaction (`inventory.service.stock_take`):
  - lock the bank's AVAILABLE, unexpired units `FOR UPDATE`. `reserve()` uses `SKIP LOCKED`, so a hold arriving
    mid-count skips them (it may hold at another bank or stay short) instead of taking a unit whose fate the count is
    deciding; a hold that locked a unit first makes the count wait, and the re-checked row (now RESERVED) drops out.
    RESERVED and ISSUED units are never selected, so holds are never touched;
  - every pair with AVAILABLE units must be counted (a zero is a count): else 422 `stock-take-incomplete` naming the
    missing pairs. A count above the list, or a count > 0 for a pair the list does not have: 422 `stock-take-increase`
    ("Add the new bags with their label expiry first"): a count never creates units, because a unit needs its label
    expiry. Nothing changes on any 422;
  - for each lower count the surplus units go `AVAILABLE -> ISSUED`, earliest expiry first, through the guarded
    `set_status` (`WHERE status = 'AVAILABLE'`);
  - `organizations.stock_confirmed_at = now`, written by the organizations router (it owns the column; ADR 011);
  - audit `organization.stock_take` `{"removed": {"B+/red_cells": 2}}`: counts only, no unit ids.
  - Response: the new summary + `stock_confirmed_at`.
- `GET /v1/blood-units/summary?blood_bank_id=` returns `[{blood_group, component, units}]` of AVAILABLE unexpired units
  (that bank's staff or an admin: reading is fine). The count form is pre-filled from it, never from the paginated
  unit list, whose `limit=100` would undercount a district bank's 200-500 units.
- `POST /organizations/{id}/stock-confirmed` is removed (404 now). Registering a unit still never moves
  `stock_confirmed_at`.
- The public search's "counted in the last 24 hours" chip becomes `confirmed_within_h` (1-168) on
  `GET /public/nearest-banks`, filtered inside the nearby SQL before the LIMIT 20 (same trap as ADR 022 / D88, R1).

## Consequences
- **FEFO guess.** The count says how many bags left, not which. Removing the earliest-expiring ones is the best guess
  (banks issue FEFO) but can remove the wrong rows; then a later hold may name a bag whose expiry differs from the
  shelf's. Marked `ponytail:` in the code; exact once bags carry numbers (item 9). Counts are audited, so a wrong guess
  is traceable.
- **Removed units read ISSUED**, not DISCARDED: they left the shelf, and the commonest reason is a counter issue
  outside the app. If the bank later needs "discarded" vs "issued", that is per-unit removal (option 1).
- **Freshness = the last count.** "Stock counted 3 hours ago" now means a person counted 3 hours ago. A technician can
  still type the old numbers without counting; the form copy ("Count bags on the shelf") and the audit trail are the
  mitigation, not a guarantee.
- **A hold during a count** is skipped at this bank for the second the count takes; the hospital can "check blood banks
  again". Measured as a test (`test_a_hold_that_arrives_during_a_count_skips_the_locked_units_instead_of_waiting`);
  removing `with_for_update()` turned it red (the hold took a unit mid-count).
- **Demo banks stay demo.** A stock-take at bank1-3 sets `stock_confirmed_at`, but `stock_is_demo` stays true: the
  units are still synthetic. The other 115 real banks have no staff, cannot count, and keep `stock_confirmed_at` NULL.
