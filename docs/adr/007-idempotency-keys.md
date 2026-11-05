# ADR 007: Idempotency keys on POST /v1/donors

Status: accepted (2026-10-10). DRAFT written by the mentor on Ansh's behalf: read it, edit it, own it.

## Context
A phone on a bad network sends `POST /v1/donors`, the server commits, the response is lost, the client retries. Today the unique
email index turns the retry into a 409, so the client believes signup failed when it succeeded. For future creates with no natural
unique key (emergency requests, reservations) a retry would create a duplicate: two ambulances dispatched for one emergency.

## Options
1. Rely on natural unique constraints only. Works for donors, does not generalise.
2. `Idempotency-Key` header, with the saved result kept in Postgres.
3. Keep the saved result in Redis with a TTL. Fast expiry, but a second store and a window where the claim and the insert can disagree.

## Decision
Option 2, optional on `POST /v1/donors`.
- Table `idempotency_keys(idempotency_key PK, request_hash, response_status, response_body, created_at)`.
- In ONE transaction: claim the key (`INSERT ... ON CONFLICT DO NOTHING`), insert the donor, save the response. All of it commits or none of it does.
- Concurrent requests with the same key: the second INSERT waits on the unique index until the first transaction finishes, then replays its saved answer. The database does the locking; no application locks.
- Same key and same body: replay the stored status and body, plus `Location` and `Idempotent-Replayed: true`, and do nothing new.
- Same key and a different body: 422 `idempotency-key-reuse`. That is a client bug, not a retry.
- Failed attempts (409, 422, 5xx) roll the key back, so errors are never cached and a corrected retry can proceed.
- The fingerprint is sha256 of the validated, normalised body (`model_dump_json`), so cosmetic differences such as email case do not matter.

## Consequences
- Security gap: the key namespace is global because there is no auth yet. A guessed key would replay someone else's saved response. Stage 2 makes the key unique per `(user_id, key)`. Clients must use random keys (UUIDv4).
- Privacy: `response_body` stores a donor's name and blood group a second time. Rows need retention (about 24 hours); the purge job is planned for 3.5 and `idempotency_keys_created_at_idx` exists for it.
- The key is optional, so protection depends on clients sending it. Requiring it on high-risk creates is a later decision.
- If a purge ever deletes a row between `claim` and `get`, the replay path would fail; the 24-hour window makes that a non-issue in practice.
- Revisit if keys must expire without a job (switch to a TTL store) or when more endpoints need the same behaviour (extract a decorator or dependency).
