# ADR 011: Module layout (modular monolith)

Status: accepted (2026-10-11).

## Context
At first everything lived in flat files (`models.py`, `schemas.py`, `repository.py`, `main.py`), all about donors.
Organizations is the second noun and inventory the third, so the flat layout would mix three nouns in each file
and nothing would stop code in one area from reaching into another area's tables. ADR 002 already said: revisit when a second
module lands.

## Options
1. Keep flat files. Zero churn, but each file mixes nouns and boundaries are invisible.
2. Layered by type (`routers/`, `models/`, `services/`). Related code is scattered across folders.
3. One folder per feature (`modules/<name>/`), one deployable app: a modular monolith.
4. Separate services per noun. Rejected: network calls, separate deploys and distributed failure modes with no scaling or team pressure to justify them.

## Decision
Option 3.
- New features live in `modules/<name>/` with `models.py`, `schemas.py`, `repository.py` and `router.py`. Today: `organizations`, `inventory`.
- Shared plumbing stays flat at the top of `Backend/`: `config.py`, `db.py` (engine, sessions, the one declarative `Base`), `deps.py` (FastAPI dependencies), `errors.py`, `pagination.py`, `audit.py`, `domain.py` (the shared blood-group vocabulary). `main.py` is the composition root: it wires routers together and nothing imports it.
- Rules: a module owns its tables; a module never imports another module or `main.py`; dependencies point inward to shared plumbing. A cross-module need goes through a function in the owning module. `test_architecture.py` turns the rule into a test.
- `deps.py` exists because `main.py` and the routers both need the shared dependencies, and importing them from `main.py` is a circular import.
- `pagination.py` was extracted on the SECOND use of keyset paging (organizations), not the first, to avoid guessing the abstraction from one example.
- Mounted under `/v1`: `/v1/organizations`, `/v1/blood-units`.

## Deliberately deferred
- Moving donors into `modules/donors/`. It adds no capability and touches about ten files. The layout is mixed for now, on purpose. Do it as one mechanical `git mv` commit when donor code must change alongside another module.
- A `service.py` layer. Routes still hold the transaction and audit calls because there is only one caller. Extract when a second caller needs the logic (for example emergency allocation).
- `DELETE /organizations`: an organization with stock is blocked by the foreign key, and deactivation belongs with the verification flow.
- `Idempotency-Key` on `POST /organizations`: it has no natural unique key, so a client retry creates a duplicate row. Add the key when creation becomes self-service.
- An index on `organizations (created_at, id)`: hundreds of rows, and ADR 009 requires a measured query.

## Consequences
- The two conventions (flat donors, modular everything else) coexist until the donor move.
- `kind` and the other enums exist twice (database CHECK and Python enum). The CHECK is the backstop; drift shows up as a 500, so tests cover the values.
- Reporting queries that join across modules would quietly erase the boundaries. If one is needed, build a dedicated read model.
- Revisit when a service layer is needed, when a module should become its own service (it already owns its tables, so the seam exists), or when authentication lands.
