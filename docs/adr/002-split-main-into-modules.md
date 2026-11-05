# ADR 002: Split main.py into config, db, models, repository

Status: accepted (2026-10-07). DRAFT written by the mentor on Ansh's behalf: read it, edit it, own it.

## Context
Lessons 1.1 to 1.3 lived in one `main.py`. Lesson 1.4 added a database, and the code now has five distinct jobs:
settings, engine/pool creation, table mapping, SQL, and HTTP. `db.py` needs settings, and tests need to build the
app with different settings, so keeping everything in `main.py` forced a module-level `app` that read config at import time.

## Options
1. Keep one file. Zero ceremony, but importing `main` needs a live config, and SQL sits inside route code.
2. Split by job into flat files next to `main.py` (`config.py`, `db.py`, `models.py`, `repository.py`).
3. Jump to the full modular-monolith layout (`modules/donors/{router,service,repository}.py`).

## Decision
Option 2. A second concern (the database) forced the split, which is the trigger our ponytail rule requires.
Option 3 is the destination, not the start: there is still only one module (donors), so a `modules/` tree would be empty ceremony.

## Consequences
- `create_app(settings)` is a factory, started with `uvicorn main:create_app --factory`. Importing `main` touches nothing.
- Tests pass their own settings in, so no global state is patched.
- Repositories never commit; the route that opens the transaction ends it.
- Revisit when a second module (organizations or inventory) lands: that is the signal to move to `modules/<name>/`.
