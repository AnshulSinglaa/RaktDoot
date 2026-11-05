-- DEV ONLY. Removes the throwaway rows left by perf_filtered_list.sql, then reclaims the space.
--   cmd /c "docker exec -i lifelink-db psql -U lifelink -d lifelink < Backend\sql\perf_cleanup.sql"
DELETE FROM donors WHERE email LIKE 'load%@example.com';
VACUUM donors;
SELECT count(*) AS donors_left FROM donors;
