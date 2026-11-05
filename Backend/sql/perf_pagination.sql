-- DEV ONLY. Measures offset vs keyset pagination on 200k throwaway donors, then deletes them.
-- Run from the repo root (PowerShell):
--   cmd /c "docker exec -i lifelink-db psql -U lifelink -d lifelink < Backend\sql\perf_pagination.sql"
-- Run it once BEFORE `alembic upgrade head` applies 0003 (no index) and once AFTER. Keep both outputs.
\timing on

INSERT INTO donors (name, email, age, blood_group, created_at)
SELECT 'D' || g, 'load' || g || '@example.com', 18 + (g % 40), 'O+', now() - (g || ' seconds')::interval
FROM generate_series(1, 200000) g;

VACUUM ANALYZE donors;

-- the bookmark: the row a client would hold after reading 150000 rows
SELECT created_at AS bm_ts, id AS bm_id FROM donors ORDER BY created_at, id OFFSET 150000 LIMIT 1 \gset

\echo '--- OFFSET 150000, LIMIT 20 ---'
EXPLAIN ANALYZE SELECT id FROM donors ORDER BY created_at, id LIMIT 20 OFFSET 150000;

\echo '--- KEYSET: 20 rows after the same bookmark ---'
EXPLAIN ANALYZE SELECT id FROM donors
WHERE (created_at, id) > (:'bm_ts'::timestamptz, :'bm_id'::uuid)
ORDER BY created_at, id LIMIT 20;

DELETE FROM donors WHERE email LIKE 'load%@example.com';
