# ADR 014: The app connects as a least-privilege database role

Status: accepted (2026-10-15).

## Context
Threat model row 3: the API connected as `lifelink`, the database owner and a superuser. Code running inside
the API (a bad dependency, a future injection bug) could `DELETE FROM audit_events`, `DISABLE TRIGGER` on the immutability
trigger, or `DROP TABLE donors`. I proved it first: the DELETE succeeded silently.

## Options
1. Keep one account and trust the code. Rejected: an "immutable" audit log the app can erase is not immutable.
2. A separate app role with explicit grants; the owner runs migrations only.
3. Row-level security policies. Powerful, but solves "which rows", not "which operations". Revisit for multi-tenant data.

## Decision
Option 2 (migration 0008).
- Role `<database>_app` (`lifelink_app`, `lifelink_test_app`): LOGIN, not superuser, no CREATEROLE.
  Naming it after the database means dev and test on one server never share a role or a password.
- Grants: SELECT/INSERT/UPDATE/DELETE on ordinary tables; **SELECT/INSERT only on `audit_events`**; USAGE on sequences.
- The owner URL (`LIFELINK_MIGRATIONS_DATABASE_URL`) is used by Alembic only. The app uses `LIFELINK_DATABASE_URL` = the app role.
- The password is set by the operator (`manage.py migrate` reads `LIFELINK_APP_DB_PASSWORD`), never in a migration file.
- Rule: every migration that creates a table GRANTs what the app needs in the same migration.
  `test_every_table_is_granted_to_the_app` fails the build when one forgets.

## Consequences
- The test suite runs the app as the app role, so every test also proves the grants are sufficient.
- One more secret to manage (two DB passwords). Accepted.
- The retention purge (deleting old audit rows) must run as the owner, as an operator job, not from the app. Correct by design.
