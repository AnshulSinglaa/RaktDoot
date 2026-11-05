# ARCHITECTURE

Latest diagram first. Older ones stay below; the diffs are the history of the system.

## 2026-10-10 (later): the audit trail lives in the same transaction (lesson 1.9, revised: no MongoDB)

A MongoDB audit log was designed first and dropped before it was ever committed (ADR 010): a second store means no shared
transaction and therefore lost events. Postgres is the only store.

```
  client (Swagger / curl / TestClient)
        |
        | HTTP+JSON   POST /v1/donors   PATCH /v1/donors/{id}
        v
+----------------------------------------------------+
| lifelink API  (FastAPI, uvicorn :8000)             |
| route -> repositories (SQL only)                   |
+----------------------------------------------------+
        |
        | asyncpg :5433
        | ONE transaction: change + audit event + COMMIT
        v
+----------------------------------------------------+
| Postgres  lifelink-db  (system of record)          |
| donors  organizations  blood_units                 |
| idempotency_keys                                   |
| audit_events  (insert only, UPDATE refused)        |
| alembic_version                                    |
+----------------------------------------------------+
```

One request travelling through it:
1. The client sends `POST /v1/donors`.
2. The route validates, then opens a Postgres transaction.
3. Inside it: claim the idempotency key (if sent), insert the donor, insert the audit event (event name, donor id, time, no personal data), save the response.
4. COMMIT: the donor, its audit event and the saved response all exist, or none of them does.
5. FastAPI sends the 201 response.

What fails here:
- Postgres is down: the request fails and nothing changes, so there is no audit gap.
- The audit insert itself fails: the whole request rolls back and returns 500 (fail closed). For health data, a complete trail beats availability of a write. The trade-off is recorded in ADR 010.

## 2026-10-10: schema changes reach the databases (lesson 1.5)

```
  PowerShell                              pytest
  alembic upgrade head                    command.upgrade(cfg, "head")
        |                                       |
        | no URL preset: env.py reads           | URL preset to the TEST db:
        | LIFELINK_DATABASE_URL                 | env.py leaves it alone
        v                                       v
+-----------------------------------------------------------------+
| alembic runner  (Backend/migrations/env.py)                     |
| choose URL | open ONE transaction | run pending revisions       |
+-----------------------------------------------------------------+
        |                                       |
        | asyncpg :5433                         | asyncpg :5433
        | DDL, one statement per execute        | DROP SCHEMA, then DDL
        v                                       v
+------------------------------+     +------------------------------+
| Postgres: lifelink (dev)     |     | Postgres: lifelink_test      |
| donors, organizations,       |     | same tables, rebuilt from    |
| blood_units                  |     | zero every pytest session    |
| alembic_version = 0002       |     | alembic_version = 0002       |
+------------------------------+     +------------------------------+
     both live in container lifelink-db (postgres:16, host :5433)
```

One migration travelling through it:
1. You write revision `0002` and run `alembic upgrade head` from PowerShell.
2. `env.py` finds no preset URL, reads `LIFELINK_DATABASE_URL` and converts it to the asyncpg form.
3. It opens one transaction and reads `alembic_version`, which says 0001.
4. It runs `0002`'s `ALTER TABLE`, then updates `alembic_version` to 0002, in the same transaction.
5. COMMIT: the schema change and the bookmark move together, or neither does.

What fails here:
- Wrong database targeted: a bad `.env` points the migration at the wrong DB. Protection: the URL in `alembic.ini` is blank, and the test path presets its own URL behind the `_test` assert.
- Migration waits on a lock: a long query holds a lock on `donors`, the `ALTER` queues behind it, and every request behind the `ALTER` queues too. Nothing crashes, the site freezes. Protection (Stage 4): set `lock_timeout` so the migration gives up fast.
