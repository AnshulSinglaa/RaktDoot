# ARCHITECTURE

Latest diagram first. Older ones stay below; the diffs are the history of the system.

## 2026-10-20 (later): real facilities from OpenStreetMap (ADR 022, migration 0016)

Same runtime processes as below; a new offline data path fills `organizations`:

```
 Backend/data/osm/                 operator shell (app role)              PostgreSQL :5432 (host 5434)
 +----------------------+          +-----------------------------+        +----------------------------+
 | mp_blood_banks.json  |  read    | manage.py import-osm        | UPSERT | organizations              |
 | mp_hospitals.json    |--------->|  osm.map_snapshot (pure):   |------->|  osm_id (partial UNIQUE)   |
 | Overpass JSON, ODbL  |          |  validate, dedupe, phones,  | 1 tx   |  data_source, ownership,   |
 +----------------------+          |  infer_ownership            |        |  ownership_inferred,       |
                                   +-----------------------------+        |  source_updated_at         |
                                   | manage.py seed-demo         |------->| blood_units (synthetic)    |
                                   |  demo set: 2 hospitals,     | COPY   | donors (synthetic)         |
                                   |  3 banks near Bhopal        |        | users (7 demo accounts)    |
                                   +-----------------------------+        | audit_events (1 per import)|
                                                                          +-------------+--------------+
   browser /find --GET /v1/public/nearest-banks?ownership=&radius_km<=1000--> api :8000 |
                 <-- NearbyBank + lat/lng, ownership, stock_is_demo ----------- nearby() SQL
```

Arrows:
- snapshot -> import-osm: the two JSON files, read once per run; nothing is fetched from the internet.
- import-osm -> organizations: one transaction, `INSERT ... ON CONFLICT (osm_id) DO UPDATE` of OSM-owned columns only.
- seed-demo -> blood_units/donors/users: synthetic stock for every real bank; logins only for the demo set.
- /find -> nearby(): the ownership filter is in the WHERE, before LIMIT 20.

One run travelling through it:
1. `import-osm` reads 119 banks and 2,104 hospitals; `map_snapshot` drops 1 + 175 rows by reason and keeps 2,047.
2. The upsert inserts them (first run) or updates them in place (later runs), ids unchanged.
3. One `organizations.imported` audit row with counts; the report prints per kind.
4. `seed-demo` sees `data_source = 'osm'` rows, picks Sultania Zanana Hospital, Masterlal Singh Hospital and the 3 banks
   nearest hospital1, writes stock, donors and 7 accounts.
5. A family searches `/find?ownership=government`: the 20 nearest government banks with compatible stock, each flagged
   `stock_is_demo`.

What fails here, and what happens:
- **A malformed row in a newer snapshot (no name, outside MP, bad phone):** dropped and counted (or the phone set NULL);
  the rest of the import goes on. A row the DB still refuses (a CHECK) rolls back the whole import: nothing half-done.
- **A facility disappears from OSM:** it is not deleted (holds and audit point at it); the report counts it
  `missing_from_snapshot` and a human decides.

## 2026-10-20: hold outcomes (ADR 021 revision 2, migration 0015)

Same processes and stores as below; what changed is the hold's life. One row in `allocations` per held unit:

```
 hospital page                     bank page /bank                    worker (every 30 s)
   | POST .../holds/{bank}/collected   | POST .../holds/issued            | expire: PENDING only
   | POST .../fulfil|cancel            | POST .../holds/confirm|reject|   |  (WHERE confirmed_at IS NULL)
   |   {collected_from: [banks]}       |      release                     |
   v                                   v                                  v
 +--------------------------------------------------------------------------------------+
 | allocations (request row lock = hold mutex; every UPDATE conditional)                 |
 |                                                                                      |
 |            confirm             pickup deadline                                       |
 |  PENDING ----------> CONFIRMED -------------> OVERDUE                                |
 |     |  \                 |                       |                                   |
 |     |   \ collected      | collected / issued    | collected / issued                |
 |     |    +-------------> ISSUED <----------------+   collected_at set, units ISSUED  |
 |     |                      (CHECK allocations_collected_is_live: never released)     |
 |     +--> EXPIRED (worker)   REJECTED (bank)   CANCELLED (close)   RELEASED (bank)     |
 +--------------------------------------------------------------------------------------+
   blood_units: RESERVED -> ISSUED (collected) | RESERVED -> AVAILABLE (released) | -> DISCARDED
```

Arrows:
- hospital -> allocations: "collected" (any live hold of one bank) and close; close must list the banks collected from.
- bank -> allocations: "issued" (same effect as collected), confirm, reject, release (OVERDUE only, never collected).
- worker -> allocations: expires PENDING holds; a collected hold is confirmed, so it can never match.

One request travelling through it:
1. A hospital posts B+ x 4: 2 units held at Bank A (B+ first, then O+: stewardship), 1 at Bank B; request OPEN (3 of 4).
2. The nurse collects from Bank A before it confirmed: A's hold -> ISSUED, units ISSUED, audit `hold.collected`.
3. Bank B never answers; the worker expires its PENDING hold at +30 min; B's unit back to AVAILABLE.
4. The hospital calls Bank B and clicks "check blood banks again": B (expired, not rejected) is asked again.
5. The patient is stable: "received" with `collected_from: []` -> FULFILLED, units_collected 2, B's new hold released.

What fails here, and what happens:
- **The nurse clicks "cancel" after collecting:** 409 `units-collected`; nothing changes. The only way out is "received",
  so collected blood never returns to the stock list.
- **"Collected" races the expiry pass:** one wins under the request row lock; if expiry won, "collected" is 409
  `hold-not-live` (call the bank). The CHECK refuses the impossible third outcome even if a future query forgets the guard.
- **Postgres goes down (restart, failover):** every DB-touching request gets 503 `database-unavailable` with
  `Retry-After: 5` and the CORS headers (the handlers are registered by exception class, so they run inside the CORS
  middleware). The desks show "Rakt Doot is briefly unavailable" + Try again; sessions survive (a 5xx is never read as
  "logged out"); the SSE stream's LISTEN connection reconnects by itself when Postgres is back (D7 of 2026-10-19, D84).

## 2026-10-19: bank-confirmed holds and their expiry (ADR 021)

```
 hospital page (SSE)            bank page /bank                     worker (worker.py)
       |  POST /v1/emergencies        |  GET /v1/emergencies/holds         |  every 30 s:
       |  POST .../allocate            |  POST .../holds/confirm|reject     |  expire_holds(now)
       v                               v                                    v
+-----------------------------------------------+        +-----------------------------------+
| api :8000 (4 workers)                          |        | 1. SELECT requests with a live    |
| allocate_remaining:                            |        |    hold WHERE expires_at <= now   |
|   pg_advisory_xact_lock(hospital-cap)          |        | 2. per request: SELECT ... FOR    |
|   reserve units FOR UPDATE SKIP LOCKED          |        |    UPDATE SKIP LOCKED (busy: skip)|
|   INSERT allocations (PENDING, +30 min |        |        | 3. UPDATE allocations SET released|
|          auto-confirm: CONFIRMED, +6 h)         |        |    WHERE released_at IS NULL AND  |
| confirm/reject: request row lock +             |        |    expires_at <= now RETURNING    |
|   conditional UPDATE allocations               |        | 4. units RESERVED -> AVAILABLE    |
+---------+----------------------------^---------+        | 5. request ALLOCATED -> OPEN,     |
          | one transaction             | NOTIFY             |    audit hold.expired, pg_notify  |
          v                             | emergency_events   +----------------+------------------+
+-----------------------------------------------+                         |
| Postgres + PostGIS                              |<------------------------+
| allocations: confirmed_at, expires_at,          |   same tables, same conditional UPDATEs
|   release_reason; idx (expires_at) WHERE live   |
| blood_units: AVAILABLE RESERVED ISSUED          |
|   EXPIRED DISCARDED                             |
| organizations: phone, stock_confirmed_at,       |
|   auto_confirm_holds, has_blood_centre          |
| emergency_requests (row lock = hold mutex)      |
| audit_events: hold.confirmed/rejected/expired   |
+-----------------------------------------------+
```

Arrows:
- hospital -> api: creates the request; holds are written PENDING (or CONFIRMED for an auto-confirm bank).
- bank -> api: lists its own live holds (no patient data), confirms or rejects one (request, bank) pair.
- worker -> Postgres: the expiry sweep; exactly-once because the release is one conditional UPDATE under the request lock.
- Postgres -> api (NOTIFY): `hold_confirmed`, `hold_rejected`, `hold_expired` -> a fresh snapshot (with per-bank holds) to every viewer.

One hold travelling through it:
1. Bhopal General Hospital 1 posts A- x 3: the cap lock is taken, 2 units at Bhopal Blood Centre 2 are held PENDING until +30 min.
2. The bank opens /bank, sees the hold (request facts only), clicks confirm: conditional UPDATE -> CONFIRMED until +6 h.
3. pg_notify on COMMIT: the hospital's screen shows "CONFIRMED, collect by ..." without a reload.
4. Had the bank not answered, the worker's next pass after +30 min releases the units and reopens the request.
5. The hospital fulfils only when every bank has confirmed: units ISSUED, request FULFILLED, never touched by the worker again.

What fails here, and what happens:
- **The worker is down:** holds do not expire. Fail safe: blood stays held, never promised twice; the next pass after a
  restart releases the whole backlog (the index on live `expires_at` keeps that query cheap).
- **A bank never uses the app:** its PENDING holds expire every 30 minutes and the hospital sees units "disappear".
  Mitigation: the hospital sees the bank's phone and deadline (call them), and reliable banks can auto-confirm.


## 2026-10-11: end of Stage 1, the whole system as it stands

One FastAPI app, one PostgreSQL. No MongoDB (ADR 010), no Redis, no queue: nothing measured needs them yet.

```
  clients (Swagger / curl / pytest)
        |
        | HTTP+JSON under /v1, every error as problem+json
        v
+----------------------------------------------------------+
| FastAPI app  (create_app factory, main.py)               |
|                                                          |
|  donors (flat)   organizations      inventory            |
|  /v1/donors      /v1/organizations  /v1/blood-units      |
|                                                          |
|  shared: deps, pagination, errors, audit, db, domain     |
+----------------------------------------------------------+
        |
        | asyncpg pool :5433. One transaction per write:
        | change + audit event (+ idempotency key) + COMMIT
        v
+----------------------------------------------------------+
| PostgreSQL 16  (container lifelink-db)  alembic head 0006|
| donors | organizations | blood_units | audit_events      |
| idempotency_keys | alembic_version                       |
+----------------------------------------------------------+
```

One request travelling through it (registering a blood unit):
1. The client sends `POST /v1/blood-units` with the bank id, group, component and the two dates.
2. The router validates the shape (Pydantic), then the shelf-life rules (a pure function using the injected clock).
3. It opens one Postgres transaction and inserts the unit. The foreign key decides whether the bank exists.
4. In the same transaction it inserts the audit event (event name and ids, no personal data), then COMMITs.
5. The API answers 201 with a `Location` header. Any failure along the way is a problem+json body and leaves nothing behind.

What fails here:
- Postgres is down: requests fail with a 503 or 500 and nothing changes, so there is no half-written state and no audit gap.
- A module reaches into another module's code: `test_architecture.py` fails the build. The boundary is a test, not a hope.

Deliberately not built yet: authentication and authorization (Stage 2), unit status changes and allocation (Stage 3), background jobs, caching, any second datastore.

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

## 2026-10-15: Interview build complete (Stages 1-2 + emergency core, geo, outbox, live status)

```
  Browser (Next.js :3000)                       k6 (load/emergency.js)
  hospital / bank / donor pages                 40 virtual hospitals
       |  fetch: Bearer JWT (memory)                 |
       |  + HttpOnly refresh cookie                  |
       |  SSE: GET /v1/emergencies/{id}/events       |
       v                                             v
+--------------------------------------------------------------+
| api :8000   FastAPI, 4 uvicorn workers (stateless)            |
| auth | donors | organizations | inventory | emergencies       |
| EventHub: 1 LISTEN connection per worker -> SSE queues        |
+------+---------------------------+---------------------------+
       | asyncpg, role lifelink_app  ^ NOTIFY emergency_events
       | (least privilege, ADR 014)  | (delivered on COMMIT only)
       v                             |
+--------------------------------------------------------------+
| Postgres 16 + PostGIS  db :5432 (host 127.0.0.1:5434)         |
| users refresh_tokens donors(location GiST) organizations(GiST)|
| blood_units  emergency_requests  allocations(1 live/unit)     |
| donor_responses  outbox  audit_events(insert-only)            |
+--------------------------------------------------------------+
       ^ SELECT ... FOR UPDATE SKIP LOCKED (due outbox rows)
       | UPDATE processed / retry / dead
+------+---------------+        +-------------------------------+
| worker  (worker.py)  |------->| Notifier (LogNotifier today;  |
| outbox drainer       |  send  | SMS / FCM adapter later)      |
+----------------------+        +-------------------------------+
  migrate (one-shot): alembic upgrade head as OWNER, sets lifelink_app password
```

Arrows:
- Browser → api: JSON over HTTP; access token in memory, refresh token as an HttpOnly SameSite=Strict cookie.
- api → Postgres: every write is one transaction (change + audit event + outbox rows + `pg_notify`).
- Postgres → api (NOTIFY): `{"request_id", "kind"}` only; each API worker turns it into a fresh snapshot per open stream.
- worker → Postgres: drains `outbox` with `FOR UPDATE SKIP LOCKED`; idempotent via `donor_responses (request_id, donor_id)`.
- worker → Notifier: one message per donor, at-least-once.

One request travelling through it (a hospital posts an O- emergency):
1. `POST /v1/emergencies` (JWT says role=hospital, org=AIIMS) → one transaction opens.
2. `organizations.service.nearby` (GiST) lists blood banks within 50 km; `inventory.service.reserve` locks matching units with `FOR UPDATE SKIP LOCKED`, marks them RESERVED.
3. Allocation rows (partial unique index: one live allocation per unit), an audit event and `pg_notify` are written in the same transaction → COMMIT.
4. Every API worker's EventHub receives the NOTIFY; the hospital's open SSE stream sends a new snapshot ("1 of 2 reserved").
5. Short on stock → `notify-donors` writes outbox rows → the worker delivers them → donors answer → each answer is another NOTIFY → the screen updates live.

What fails here, and what happens:
- **The worker dies mid-batch:** its transaction rolls back; the outbox rows stay due and are taken by the next run (at-least-once). A donor may get one duplicate message; `donor_responses` keeps our state exactly-once.
- **Postgres goes down:** writes fail with 500 and nothing is half-written (one transaction per change); `/health/ready` reports 503; open SSE streams stop updating until the API's LISTEN connection is re-established (today: API restart). Every reconnecting viewer starts from a full snapshot, so no stale state survives.

Measured (ADR 019, k6, 40 VUs, 4 workers): 429 req/s, 0 errors / 13,256 requests, read p95 125 ms, create p95 306 ms.
