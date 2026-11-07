# ADR 004: Alembic with hand-written migrations

Status: accepted (2026-10-10).

## Context
The schema was applied by hand from `sql/001_init.sql`. Nothing recorded which version a database was on, tests built their
schema from a different path than dev did, and a table that already holds rows cannot be changed by re-running a create script.

## Options
1. Keep numbered SQL files applied by hand. Simple, but no version record and no downgrade.
2. Alembic with autogenerate (diff `models.py` against the database).
3. Alembic with hand-written revisions (raw SQL via `op.execute`).

## Decision
Option 3.
- `models.py` does not declare the CHECK constraints or the `lower(email)` unique index, and autogenerate does not see CHECKs at all,
  so it would produce wrong diffs. Autogenerate stays a draft tool at most, reviewed line by line.
- The SQL was already written and correct, so it is reused (one statement per `op.execute`, because asyncpg prepared
  statements reject multi-statement strings).
- Migrations live in `Backend/migrations`; `alembic.ini` leaves the URL blank on purpose. `env.py` uses a URL preset by the caller
  and otherwise falls back to settings, so pytest can never migrate the dev database by accident.
- pytest builds the test schema with `alembic upgrade head`, and `test_migrations.py` runs down-and-up round trips.
- Applied revisions are immutable. Changes are new revisions. Changes to populated tables follow expand/contract.

## Consequences
- `models.py` and the migrations are two places that describe the schema, and a human keeps them in sync (tests catch most drift).
- The existing dev DB was adopted with `alembic stamp 0001`; fresh databases get `0001` by running it.
- `sql/001_init.sql` is retired; `seed_dev.sql` remains for dev data.
- Revisit when the schema grows enough that hand-sync hurts: consider autogenerate-then-review, or SQLAlchemy `CheckConstraint`/`Index` declarations in the models.
