# ADR 008: Test strategy (real Postgres, markers, factories, coverage)

Status: accepted (2026-10-10). DRAFT written by the mentor on Ansh's behalf: read it, edit it, own it.

## Context
By lesson 1.6 the suite had 56 tests, and three problems:
1. If `LIFELINK_TEST_DATABASE_URL` was missing, every database test was SKIPPED and pytest still exited green
   (21 passed, 35 skipped, exit code 0). On a fresh clone or in CI that is a build that tested nothing.
2. Test data was copy-pasted dict literals, so a new required field would break many tests for the wrong reason.
3. There was no fast loop (run only tests that need no database) and no measure of what was untested.

## Options
1. Mocks or SQLite for the database. Fast, but SQLite has no row locks, different types and no partial indexes, and a mock only proves the code matches our assumption.
2. A persistent `lifelink_test` database in the dev container (what we had). Fast, but needs manual setup.
3. A throwaway Postgres container started from the tests (testcontainers). Zero setup, a few seconds of startup.

## Decision
- Tests run against a real PostgreSQL 16, never SQLite or mocks. The schema is built with `alembic upgrade head`, the same path production uses.
- `test_url` uses `LIFELINK_TEST_DATABASE_URL` when configured (fast, persistent). Otherwise it starts a `postgres:16` testcontainer on a random port.
- If neither is available: skip locally, but FAIL when the `CI` environment variable is set. A missing database in CI is a failure, never a skip.
- The fixture refuses to run unless the database name ends in `_test`, because it drops the whole schema.
- Any test that uses the database is marked `integration` automatically (a `conftest.py` hook). `pytest -m "not integration"` runs the pure unit tests with no Docker and no database. `--strict-markers` turns marker typos into errors.
- Test data comes from `factories.make_donor(**overrides)`: valid, unique, override only what the test is about. A fixed `GOOD` body stays only where a test needs the same email twice.
- Isolation is TRUNCATE between tests, not rollback-per-test: our code calls commit through `session.begin()`, and rolling back would stop us testing real commits, constraints and concurrency.
- Coverage is measured with branches (`pytest --cov --cov-branch`). It reports execution, not verification, so test quality is checked by hand-made mutants (see Consequences).

## Evidence (fill in from your own runs)
- Unit-only run (`pytest -m "not integration"`): ___ tests in ___ s
- Full run with the configured database: ___ tests in ___ s
- Full run through the testcontainers fallback: ___ tests in ___ s
- Coverage (line / branch): ___ % / ___ %

## Consequences
- Surviving mutant found in 1.7: changing `len(rows) > limit` to `>=` in the list route passed every test, because a last page that is exactly full was never tested. `test_exact_fit_last_page_has_no_cursor` kills it. Coverage showed that line as 100% covered the whole time.
- The testcontainers path needs Docker and pulls `postgres:16`. On Windows the reaper container can fail; `TESTCONTAINERS_RYUK_DISABLED=true` is the escape hatch.
- Tests are not yet parallel. `pytest-xdist` would need one database per worker.
- A coverage floor (for example `--cov-fail-under=80`) is added to CI in Stage 4 once the number is honest. 80% is a floor, not a goal.
- Revisit when the suite is slow enough to skip, or when automated mutation testing (`mutmut`) is cheap enough to run in CI.
