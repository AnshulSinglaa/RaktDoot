-- DEV ONLY. Filtered keyset list on 300k throwaway donors with a REALISTIC blood-group skew.
-- Safe to run repeatedly: it removes its own rows first (emails load%@example.com), never your real donors.
-- Run from the repo root (PowerShell):
--   cmd /c "docker exec -i lifelink-db psql -U lifelink -d lifelink < Backend\sql\perf_filtered_list.sql"
-- Run it once BEFORE migration 0005 and once AFTER. It LEAVES the rows in place so you can experiment in psql.
-- Remove them with Backend\sql\perf_cleanup.sql.
\timing on

DELETE FROM donors WHERE email LIKE 'load%@example.com';

\echo '--- LOAD 300k rows (watch this time: it includes maintaining every index on donors) ---'
INSERT INTO donors (name, email, age, blood_group, is_available, created_at)
SELECT 'D' || g, 'load' || g || '@example.com', 18 + (g % 40),
       CASE WHEN g % 1000 < 400 THEN 'O+'  WHEN g % 1000 < 700 THEN 'A+'  WHEN g % 1000 < 900 THEN 'B+'
            WHEN g % 1000 < 940 THEN 'O-'  WHEN g % 1000 < 970 THEN 'A-'  WHEN g % 1000 < 990 THEN 'B-'
            WHEN g % 1000 < 998 THEN 'AB+' ELSE 'AB-' END,
       (g % 7) <> 0,
       now() - (g || ' seconds')::interval
FROM generate_series(1, 300000) g;

VACUUM ANALYZE donors;

\echo '--- rows per blood group (rare groups are the interesting ones) ---'
SELECT blood_group, count(*) FROM donors GROUP BY blood_group ORDER BY count(*);

\echo '--- index sizes on donors ---'
SELECT indexrelname AS index, pg_size_pretty(pg_relation_size(indexrelid)) AS size
FROM pg_stat_user_indexes WHERE relname = 'donors' ORDER BY indexrelname;

\echo '--- 1. RARE group (AB-, about 0.2%), first page, the query the app sends ---'
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM donors WHERE blood_group = 'AB-' AND is_available ORDER BY created_at, id LIMIT 21;

\echo '--- 2. COMMON group (O+, 40%), first page ---'
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM donors WHERE blood_group = 'O+' AND is_available ORDER BY created_at, id LIMIT 21;

\echo '--- 3. UNFILTERED deep page with SELECT * (what the app really sends; ADR 005 used SELECT id) ---'
SELECT created_at AS bm_ts, id AS bm_id FROM donors ORDER BY created_at, id OFFSET 150000 LIMIT 1 \gset
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM donors WHERE (created_at, id) > (:'bm_ts'::timestamptz, :'bm_id'::uuid) ORDER BY created_at, id LIMIT 21;
