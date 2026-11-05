# ADR 010: Audit log in PostgreSQL (MongoDB evaluated and rejected for now)

Status: accepted (2026-10-10). DRAFT written by the mentor on Ansh's behalf: read it, edit it, own it.

## Context
LifeLink handles health data, so "who did what to which record, and when" must be answerable. The original plan gave MongoDB the
audit-log job. A first Mongo version was written (best-effort, after the Postgres commit) but never committed. Ansh challenged it
with the right question: is Mongo actually required? It was not. Nothing had been measured that Postgres could not handle, and Mongo
brought a second store to run, back up and secure, plus a hole that no retry can close: Postgres and Mongo cannot share a transaction,
so an audit event can be lost (Mongo down, or the process dies between the commit and the insert).

## Options
1. MongoDB, written after the Postgres commit (best-effort). Flexible documents and free TTL expiry, but not atomic with the change.
2. A table in PostgreSQL, inserted in the SAME transaction as the change.
3. Plain log files. Hard to query per entity, no retention control.

## Decision
Option 2.
- Table `audit_events(id, at, event, entity, entity_id, actor_id, details jsonb)` (migration 0006).
- Written by `AuditRepository.add` inside the route's existing `session.begin()` block. Either the change and its event both commit, or neither does: no lost events and no phantom events. A failed or replayed request leaves no trace.
- Fail closed: if the audit insert fails, the request fails (500) and the change does not happen. For health data, completeness of the trail beats availability of a write. Revisit if this ever hurts.
- Privacy: ids and field NAMES only, never values. `donor.updated` records `{"fields": ["age", "is_available"]}`, not the new age. No names, emails or blood groups.
- Immutable: a trigger refuses UPDATE on `audit_events`. DELETE stays allowed for the retention purge job (3.5). Later: revoke UPDATE/DELETE from the application role, and drop old partitions instead of deleting rows.
- `at` comes from the injected clock (timezone-aware UTC). `actor_id` stays NULL until authentication exists (Stage 2).
- No secondary index yet, following ADR 009: nothing reads the table until the first audit endpoint, which needs authorization (Stage 2). Then add `(entity_id, at, id)`, justified with `EXPLAIN`.
- No read endpoint yet: audit data must not be public.
- MongoDB is removed from the project. Add a document store back only when a measured need appears. Candidates: the notification delivery log (3.6) and the rider location trail (Stage 6), where write volume, payload variety and expiry might justify it. Even then, partitioned Postgres tables and time-series stores compete, so decide with numbers.

## Consequences
- One store to run, back up and secure; the same migrations, tests and transactions as everything else.
- No automatic expiry: retention (a compliance decision, see 2.8 and DPDP) needs a purge job, planned for 3.5.
- The audit table lives in the main database, so heavy audit volume competes with the donor workload. Partitioning by time (6.4) or a separate store is the answer if that is ever measured.
- `jsonb` gives schema-flexible `details` without a second database.
- Lesson kept from the Mongo detour: when two stores cannot share a transaction, a write can be lost. We avoid creating that problem until we must (the transactional outbox, 3.7, is the answer when we do).
