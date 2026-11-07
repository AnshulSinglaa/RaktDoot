# ADR 012: Inventory, minimal scope (register, get, list)

Status: accepted (2026-10-11).

## Context
Blood units are the core domain object. We asked, as we did with MongoDB, whether the whole inventory CRUD was really needed now.
The honest answer was: partly. Registering and reading units is needed soon: object-level authorization (a blood bank may
only see its own stock) is best built on a tenant-owned resource, and matching and allocation need data. Changing a unit's
status is not needed now: it belongs to the allocation state machine built later, and a generic PATCH would let anyone bypass rules that do not exist yet.

## Options
1. Full CRUD now, including PATCH and DELETE of any field.
2. Register, get and list only.
3. Defer inventory until allocation is built.

## Decision
Option 2.
- `POST /v1/blood-units`: a new unit is always `AVAILABLE` (a `status` field in the body is a 422). Registered in one transaction with an audit event `blood_unit.registered` (ids only).
- `GET /v1/blood-units/{id}` and `GET /v1/blood-units` with filters `blood_bank_id`, `blood_group`, `component`, `status`.
- The list is ordered soonest-expiry-first (FEFO: first expired, first out) and keyset-paged on `(expires_at, id)`. The existing partial index from migration 0001 already serves the fully filtered case. No new index, per ADR 009.
- The client supplies `collected_at` and `expires_at` (a bank reads the expiry from the bag). The server only refuses impossible values, in a pure function (`shelf_life.py`): expiry before collection, collected in the future, already expired, or stored longer than the maximum for the component.
- The maxima (red cells 42 days, platelets 7 days, plasma 365 days) are upper bounds, not clinical advice. Confirm them against blood-bank / NBTC rules before real use.
- An unknown `blood_bank_id` is a 422 `unknown-organization` (the foreign key is the arbiter, so the check is race-free).
- No PATCH and no DELETE. A test pins the 405.

## Consequences
- Any organization may hold stock (a hospital blood bank is legitimate, and checking `kind = blood_bank` needs a cross-module call that ADR 011 forbids for now). `test_any_organization_may_hold_stock_for_now` records this. Add the check with the first rule that needs it (3.3 or 3.4).
- The schema has no `version` column yet. Optimistic locking arrives with the allocation work (3.3).
- Unit status changes, reservation and expiry handling come with allocation.
- Revisit at 3.3 (allocation), 3.9 (expiry job) and 2.4 (ownership checks).
