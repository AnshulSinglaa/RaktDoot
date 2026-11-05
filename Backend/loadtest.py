"""SMOKE script only. For real numbers use k6 (load/emergency.js): this single Python process tops out around ~100 req/s
and then measures itself, not the API (found and documented in docs/adr/019-load-test.md).

A small load test:  python loadtest.py --base http://127.0.0.1:8000 --users 40 --seconds 30

Logs in as the seeded demo hospitals (manage.py seed-demo), then each virtual user loops over a realistic mix:
    60%  GET  /v1/emergencies/{id}               a dashboard re-reading its request
    25%  POST /v1/emergencies                    a new emergency: geo search + FOR UPDATE SKIP LOCKED allocation
    15%  GET  /v1/emergencies/{id}/donor-matches PostGIS donor search (audited)
Prints p50/p95/p99 per endpoint and the error rate. At the end it CANCELS every request it created, so the reserved units
go back to stock and the demo data stays usable.

Caveat (say it in an interview): client and server share one laptop, so the numbers include the load generator's own CPU.
They show the shape (where time goes, whether errors appear under concurrency), not production capacity.
"""
import argparse
import asyncio
import random
import statistics
import time
from collections import defaultdict
from pathlib import Path

import httpx

CREDENTIALS = Path(__file__).parent / "demo-credentials.txt"
GROUPS = ["O+", "B+", "A+", "AB+", "O-", "B-", "A-", "AB-"]


def pct(values: list[float], p: float) -> float:
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1] if len(values) > 1 else values[0]


async def main(base: str, users: int, seconds: float) -> None:
    lines = CREDENTIALS.read_text(encoding="utf-8").splitlines()
    password = lines[0].split("ALL accounts: ")[1].strip()
    # the login limiter allows 10 attempts/minute per IP (ADR 013): 8 hospital accounts stay under it
    emails = [line.split()[0] for line in lines if " hospital " in line][:min(users, 8)]
    timings: dict[str, list[float]] = defaultdict(list)
    errors: dict[str, int] = defaultdict(int)
    created: list[tuple[str, str]] = []                 # (request id, token) to clean up

    async with httpx.AsyncClient(base_url=base, timeout=30, limits=httpx.Limits(max_connections=users + 10)) as http:
        tokens = []
        for email in emails:
            r = await http.post("/v1/auth/login", json={"email": email, "password": password})
            r.raise_for_status()
            tokens.append(r.json()["access_token"])

        async def timed(name: str, method: str, url: str, token: str, **kw) -> httpx.Response | None:
            t0 = time.perf_counter()
            try:
                r = await http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
            except httpx.HTTPError:
                errors[name] += 1
                return None
            timings[name].append((time.perf_counter() - t0) * 1000)
            if r.status_code >= 500 or r.status_code in (401, 403):
                errors[name] += 1
            return r

        async def user(n: int) -> None:
            token, rng, mine = tokens[n % len(tokens)], random.Random(n), []
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                roll = rng.random()
                if roll < .25 or not mine:
                    r = await timed("POST /v1/emergencies", "POST", "/v1/emergencies", token,
                                    json={"blood_group": rng.choice(GROUPS), "units_needed": rng.randint(1, 3)})
                    if r is not None and r.status_code == 201:
                        mine.append(r.json()["id"])
                        created.append((r.json()["id"], token))
                elif roll < .85:
                    await timed("GET /v1/emergencies/{id}", "GET", f"/v1/emergencies/{rng.choice(mine)}", token)
                else:
                    await timed("GET .../donor-matches", "GET", f"/v1/emergencies/{rng.choice(mine)}/donor-matches", token)

        start = time.perf_counter()
        await asyncio.gather(*[user(n) for n in range(users)])
        elapsed = time.perf_counter() - start

        for rid, token in created:                       # put the demo stock back
            await http.post(f"/v1/emergencies/{rid}/cancel", json={"collected_from": []},
                            headers={"Authorization": f"Bearer {token}"})

    total = sum(len(v) for v in timings.values())
    print(f"\n{users} concurrent users for {elapsed:.0f} s -> {total} requests, {total / elapsed:.0f} req/s\n")
    print(f"{'endpoint':32} {'count':>6} {'p50 ms':>8} {'p95 ms':>8} {'p99 ms':>8} {'errors':>7}")
    for name, values in sorted(timings.items()):
        print(f"{name:32} {len(values):6} {pct(values, 50):8.1f} {pct(values, 95):8.1f} {pct(values, 99):8.1f} {errors[name]:7}")
    print(f"\ncleaned up: cancelled {len(created)} requests (units back to AVAILABLE)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--users", type=int, default=40)
    ap.add_argument("--seconds", type=float, default=30)
    a = ap.parse_args()
    asyncio.run(main(a.base, a.users, a.seconds))
