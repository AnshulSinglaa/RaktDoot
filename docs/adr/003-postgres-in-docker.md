# ADR 003: Run Postgres in Docker on host port 5433

Status: accepted (2026-10-07).

## Context
Postgres is needed from the start. The dev machine (Windows) has both Docker Desktop and a native PostgreSQL on port 5432.

## Options
1. Use the native Windows Postgres.
2. Run `postgres:16` in a Docker container.
3. Use a hosted dev database.

## Decision
Option 2: container `lifelink-db`, image `postgres:16`, volume `lifelink_pgdata`, published on HOST port 5433 so it cannot clash with the native install on 5432 (left untouched).

Reasons: the version is pinned and reproducible, it is disposable (delete the volume, start clean), and it has the same shape as the later Compose and AWS work.

## Consequences
- The stock image has no PostGIS. Switch to a postgis image when geo arrives.
- The dev password is a throwaway kept in the gitignored `.env`. Never commit it. `.env.example` carries a `CHANGE_ME` placeholder.
- The test database `lifelink_test` lives in the same container.
- Data lives in a Docker volume, so it is not backed up. Fine for dev; a real backup and restore drill arrives in 4.6.
- Revisit when Docker Compose replaces the hand-run `docker run`.
