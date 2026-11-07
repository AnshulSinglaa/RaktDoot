# ADR 009: Indexing policy

Status: accepted (2026-10-10).

## Context
`GET /v1/donors?blood_group=...&is_available=...` is a keyset-paginated list. Migration 0003 built `(created_at, id)` for the
UNFILTERED list. For a rare blood group that index forces Postgres to read rows in date order and discard almost all of them
(`Rows Removed by Filter`) until it finds enough matches. For a common group (O+ is about 40%) the same index is fine, because
matches are dense. Indexes cost writes, disk and cache, so "add one just in case" is not acceptable.

## Options
1. Keep `(created_at, id)` only. Zero cost, slow for rare groups.
2. Index `blood_group` alone. Gives the rows but not their order, so Postgres still sorts (or walks and discards).
3. Composite `(blood_group, created_at, id)`: equality column first, then the sort columns.
4. Also index `is_available`, or make it a partial index. Rejected for now (see below).

## Decision
Option 3, migration 0005: `donors_blood_group_created_at_id_idx ON donors (blood_group, created_at, id)`.
- Policy: an index exists only for a query we have MEASURED. Its migration or ADR records the query, the `EXPLAIN (ANALYZE, BUFFERS)` before and after, and the write-cost trade-off.
- Column order: equality columns first, then range/sort columns (leftmost-prefix rule).
- `(created_at, id)` stays: the unfiltered list still needs it.
- Not indexed on purpose: `is_available`. About 86% of donors are available (low selectivity), it flips often (indexing it breaks HOT updates and adds bloat), and the filter costs almost nothing once `blood_group` narrows the rows.
- We tune the query the app really sends (read it with `LIFELINK_SQL_ECHO=true`): `SELECT *`, so these plans do heap fetches. Index-only scans and `INCLUDE` columns apply only to narrow queries.
- Production builds use `CREATE INDEX CONCURRENTLY` (not inside a transaction; a failed build leaves an INVALID index to drop).
- We do not write tests that assert a query plan: on a small test table the planner correctly picks a seq scan, so such a test would be flaky or wrong. Performance budgets arrive with load tests.

## Evidence (fill in from `before.txt` and `after.txt`, produced by `Backend/sql/perf_filtered_list.sql`; 300k rows)
- Rare group AB- (about 0.2%), first page, before: ___ ms, buffers ___, plan: ___
- Rare group AB-, first page, after 0005: ___ ms, buffers ___, plan: Index Scan using donors_blood_group_created_at_id_idx
- Common group O+ (40%), before / after: ___ ms / ___ ms (expect little change: selectivity decides)
- Unfiltered deep page with `SELECT *` (the honest keyset number; ADR 005 used `SELECT id`): ___ ms
- Cost: 300k INSERT time before / after: ___ ms / ___ ms. Index size: ___ MB

## Consequences
- Every write to `donors` now maintains four indexes (primary key, `lower(email)` unique, `(created_at, id)`, and this one).
- A new filter or sort needs its own measured index. Review `pg_stat_user_indexes` periodically and drop indexes with no scans and no planned reader.
- Foreign keys are not indexed automatically in Postgres. `blood_units.blood_bank_id` is covered only by a partial index (AVAILABLE rows); if organizations are ever hard-deleted, add a plain index first.
- Revisit when matching (3.4) adds geo filters (a GiST index) and when a filter combines several rare conditions (partial indexes).
