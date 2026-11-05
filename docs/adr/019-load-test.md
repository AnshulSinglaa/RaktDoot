# ADR 019: Load test, SLO and API worker count

Status: accepted (2026-10-15). Drafted by Claude on Ansh's behalf: read it, own it.

## Context
"It's fast" is not a claim, a number is. Before tuning anything we set an SLO (CLAUDE.md 5.1) and measured the real
workload: one emergency lifecycle = create (geo search + `FOR UPDATE SKIP LOCKED` allocation) → 3 reads → donor-matches
(PostGIS) → cancel. Self-cleaning: every reserved unit is released.

**SLO:** errors < 1 %, p95 create < 300 ms, p95 read < 150 ms, at 40 concurrent hospital users.

## What happened (the order matters: it is the interview story)
1. **First tool: a Python asyncio + httpx script from the Windows host.** 40 users: 67 req/s, p50 ~400 ms, p95 ~1.8 s.
   1 user: 91 req/s, p50 8 ms. More users gave LESS throughput.
2. **Hypothesis: one uvicorn process is CPU-bound.** Raised to 4 workers. Result: unchanged (64 req/s). Hypothesis rejected.
3. **Measured instead of guessing:** during load, the API container used 29 % of 12 cores and Postgres 5 %, with every DB
   connection `idle / ClientRead`. The servers were waiting for the CLIENT.
4. **Moved the generator inside the Docker network** (no Docker Desktop port forwarding): 104 req/s. Better, still idle servers.
5. **Calibrated the tool:** k6 against `/health/live` did 4,140 req/s, p95 15 ms. The Python script had been measuring itself.
6. **Real measurement with k6** (`Backend/load/emergency.js`, 40 VUs, 30 s, inside the compose network):

| API workers | req/s | read p95 | create p95 | errors |
|---|---|---|---|---|
| 1 | 170 | 418 ms ✗ | 561 ms ✗ | 0 / 5,318 |
| 4 | **429** | **125 ms ✓** | **306 ms ✗ (by 6 ms)** | **0 / 13,256** |

## Decision
- `WEB_CONCURRENCY=4` uvicorn workers by default (`API_WORKERS` in compose). The API is stateless, so it scales out; the
  connection budget is 4 × (5 + 5) pooled + 4 LISTEN + the worker's ≈ 46 < Postgres `max_connections` 100.
- k6 is the load tool (`Backend/load/`); `loadtest.py` stays only as a quick smoke script. Calibrate any tool on a trivial
  endpoint before trusting it.
- The create SLO is missed by 6 ms at 40 VUs and that is recorded, not hidden. Next steps if it matters: profile create
  (it runs ~6 queries in one transaction: location, nearby banks, reserve, inserts, audit, notify, then 2 reads to present
  the response); cache the hospital location; return the response from data already in memory instead of re-reading it.

## Consequences
- The login rate limiter lives in process memory, so with 4 workers the real limit is ~40 attempts/min per IP, not 10.
  This is exactly the trigger ADR 013 named: move the counter to Redis (`INCR` + `EXPIRE`) or the edge (5.8).
- Numbers come from one laptop (Windows + Docker Desktop, 12 cores shared by client, API and DB). They show shape and
  headroom, not production capacity. Re-run on the deploy target.
- 0 errors across 2,208 concurrent lifecycles at 40 VUs means the locking design (ADR 016) holds under real concurrency, not only in tests.
