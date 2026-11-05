# ADR 005: REST conventions (resources, /v1, keyset pagination)

Status: accepted (2026-10-10). DRAFT written by the mentor on Ansh's behalf: read it, edit it, own it.

## Context
`GET /donors` returned every donor, with no version in the URL. An unbounded list is a slow query and a denial-of-service
we would have written ourselves. Once any client depends on a response shape, changing it breaks them.

## Options
1. Offset pagination (`LIMIT n OFFSET m`). Simple, supports "jump to page 7", but costs O(offset) and shifts under concurrent writes.
2. Keyset pagination: "rows strictly after this bookmark". Constant cost with the right index, stable under writes, no random access.
3. Return everything and let clients cope.

## Decision
- Resources are nouns: `/v1/donors`, `/v1/donors/{id}`. Methods are verbs. Creation answers 201 with a `Location` header.
- Every client-facing route lives under `/v1`. Health checks and `/info` stay unversioned (infrastructure, not API clients).
- Lists use keyset pagination ordered by `(created_at, id)`. `id` is the unique tie-breaker, so ordering is total.
- The cursor is opaque (base64 of `created_at|id`). It carries sort values only, so a crafted cursor can only move the caller's own bookmark. A malformed one is a 422.
- The repository fetches `limit + 1` rows to learn whether a next page exists. There is no total count (`COUNT(*)` is slow on big Postgres tables).
- `limit` defaults to 20 and is capped at 100. A list is an object `{"items": [...], "next_cursor": ...}`, never a bare array.
- Filters are an allowlist of declared query parameters (`blood_group`, `is_available`). Client strings never reach `ORDER BY`.
- Migration 0003 adds `donors_created_at_id_idx (created_at, id)`, which matches the sort exactly.

## Evidence (measured 2026-10-10 with `Backend/sql/perf_pagination.sql`, 200k rows, before and after migration 0003)
- OFFSET 150000, no index: 46.2 ms (Seq Scan, then a sort that spilled to disk: external merge, 6.6 MB)
- Keyset after the same row, no index: 13.1 ms (Parallel Seq Scan, top-N heapsort; it filtered 150k rows to return 20)
- OFFSET 150000, with index: 15.4 ms (Index Only Scan, but it still walked 150,020 index entries to throw 150,000 away)
- Keyset after the same row, with index: 0.034 ms (Index Only Scan using donors_created_at_id_idx, Index Cond on the bookmark, 20 rows touched). About 450x faster than offset-with-index and 1,350x faster than the original.

## Consequences
- No random access to "page N" and no total count. Admin screens that need them get a separate, deliberate endpoint later.
- Unknown query parameters are silently ignored by FastAPI, so a typo like `?blod_group=A%2B` returns everything. Strict rejection is deferred.
- Breaking changes (removing or renaming a field, changing a type, tightening validation) require `/v2` beside `/v1`. Adding an optional field is not breaking.
- Index cost: slightly slower inserts and more disk. A plain `CREATE INDEX` blocks writes while building, which is fine today; on a big table use `CREATE INDEX CONCURRENTLY`.
- Revisit when a client needs sorting by another column (each sort needs its own matching index) or a signed cursor.
