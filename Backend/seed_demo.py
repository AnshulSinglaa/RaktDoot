"""Demo data for a walkthrough:  python manage.py seed-demo [--donors N]

Stock, holds, donors and accounts are SYNTHETIC and labelled so (@demo.raktdoot.in emails, +910 phones). Real donor
data would be illegal to use (DPDP Act). Facilities: if real MP facilities were imported first (`import-osm`, ADR 022),
they are used: every real bank gets synthetic stock, but only a fixed demo set (2 hospitals, 3 banks near the Bhopal
centre) gets login accounts and a stock-confirmation time, so we never claim a real bank confirmed anything. Without
imported rows, synthetic "(demo)" facilities are created around four Madhya Pradesh cities.

Reproducible: a fixed random seed gives the same data every run on an empty database (names, groups, places, dates
relative to now, sex, phones, verification; only uuids and the timestamps' absolute values differ). Idempotent: refuses
to run twice.
Demo password: LIFELINK_DEMO_PASSWORD if set (accounts are printed, the password never is); otherwise a random one
written to Backend/demo-credentials.txt (gitignored).
"""
import os
import random
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

from security import hash_password

CITIES = {   # city centre (lat, lng)
    "Bhopal": (23.2599, 77.4126),
    "Indore": (22.7196, 75.8577),
    "Jabalpur": (23.1815, 79.9864),
    "Gwalior": (26.2183, 78.1828),
}
# Approximate ABO/Rh shares for India (O+ and B+ dominate; negatives are ~5% in total)
GROUP_SHARES = {"O+": .32, "B+": .30, "A+": .22, "AB+": .08, "O-": .02, "B-": .02, "A-": .02, "AB-": .02}
FIRST_NAMES = ["Aarav", "Vivaan", "Aditya", "Ishaan", "Arjun", "Saanvi", "Ananya", "Diya", "Aadhya", "Kavya",
               "Rohan", "Priya", "Neha", "Rahul", "Sneha", "Vikram", "Pooja", "Amit", "Riya", "Karan"]
FRESHNESS = (timedelta(minutes=20), timedelta(hours=5), timedelta(days=3))     # demo banks 1-3: fresh, hours, stale
CREDENTIALS = Path(__file__).parent / "demo-credentials.txt"


def jitter(lat: float, lng: float, km: float, rng: random.Random) -> tuple[float, float]:
    # 1 degree of latitude is ~111 km; good enough for scattering points around a city
    return lat + rng.uniform(-km, km) / 111, lng + rng.uniform(-km, km) / 111


async def seed(database_url: str, donors: int = 2000, credentials: Path = CREDENTIALS) -> None:
    rng = random.Random(42)
    now = datetime.now(timezone.utc)
    conn = await asyncpg.connect(database_url.replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        if await conn.fetchval("SELECT count(*) FROM users WHERE email LIKE '%@demo.raktdoot.in'"):
            print("[seed-demo] already seeded: nothing to do")
            return
        async with conn.transaction():          # all of it or none of it
            await _seed(conn, rng, now, donors, credentials)
    finally:
        await conn.close()


NEAREST_WITH_PHONE = """
    SELECT id, kind, city, latitude, longitude FROM organizations
    WHERE data_source = 'osm' AND kind = $1 AND phone IS NOT NULL
    ORDER BY ST_Distance(location, CAST(ST_SetSRID(ST_MakePoint($3, $2), 4326) AS geography)), osm_id
    LIMIT $4"""


async def _seed(conn: asyncpg.Connection, rng: random.Random, now: datetime, donor_count: int, credentials: Path) -> None:
    # ---------- facilities: the imported real ones (osm), else synthetic ----------
    facilities = await conn.fetch(
        "SELECT id, kind, city, latitude, longitude FROM organizations WHERE data_source = 'osm' "
        "AND kind IN ('hospital', 'blood_bank') AND latitude IS NOT NULL ORDER BY kind, osm_id")
    demo_logins: list = []              # (email, facility row) for the real demo set; empty = one login per facility
    if facilities:
        # the demo set, deterministic for one snapshot (ties by OSM id): the 2 hospitals with a phone nearest the Bhopal
        # centre, and the 3 banks with a phone nearest hospital1, so hospital1's requests land on banks that can log in
        demo_hospitals = await conn.fetch(NEAREST_WITH_PHONE, "hospital", *CITIES["Bhopal"], 2)
        demo_banks = await conn.fetch(NEAREST_WITH_PHONE, "blood_bank", demo_hospitals[0]["latitude"],
                                      demo_hospitals[0]["longitude"], 3) if demo_hospitals else []
        demo_logins = ([(f"hospital{i}@demo.raktdoot.in", f) for i, f in enumerate(demo_hospitals, 1)]
                       + [(f"bank{i}@demo.raktdoot.in", f) for i, f in enumerate(demo_banks, 1)])
        # synthetic freshness on the demo banks only; every other real bank stays "never confirmed" (NULL).
        # bank1 auto-confirms its holds; bank2 and bank3 confirm by hand on /bank.
        for i, (bank, ago) in enumerate(zip(demo_banks, FRESHNESS, strict=False)):
            await conn.execute("UPDATE organizations SET stock_confirmed_at = $2, auto_confirm_holds = $3 WHERE id = $1",
                               bank["id"], now - ago, i == 0)
    else:
        facilities = await conn.fetch(
            "SELECT id, kind, city, latitude, longitude FROM organizations "
            "WHERE kind IN ('hospital', 'blood_bank') AND latitude IS NOT NULL ORDER BY kind, name")
    if not facilities:
        rows = []
        # stock freshness spread over the banks (ADR 021): confirmed minutes ago, hours ago, days ago, never
        fresh = [now - timedelta(minutes=20), now - timedelta(hours=5), now - timedelta(days=3), None]
        k = 0
        for city, (lat, lng) in CITIES.items():
            for i in range(1, 5):
                rows.append((uuid.uuid4(), "hospital", f"{city} General Hospital {i} (demo)", city, *jitter(lat, lng, 6, rng),
                             None, None, False, "synthetic"))
            for i in range(1, 4):
                # +910 + 9 digits: the DB CHECK accepts it, no real Indian number has a 0 after +91, so it is never
                # dialable. Bank 1 of each city auto-confirms its holds; the others confirm by hand on /bank.
                rows.append((uuid.uuid4(), "blood_bank", f"{city} Blood Centre {i} (demo)", city, *jitter(lat, lng, 6, rng),
                             f"+9109{k:08d}", fresh[k % len(fresh)], i == 1, "synthetic"))
                k += 1
        await conn.copy_records_to_table("organizations", records=rows, columns=[
            "id", "kind", "name", "city", "latitude", "longitude", "phone", "stock_confirmed_at", "auto_confirm_holds",
            "data_source"])
        facilities = await conn.fetch(
            "SELECT id, kind, city, latitude, longitude FROM organizations WHERE kind IN ('hospital', 'blood_bank') "
            "ORDER BY kind, name")
    banks = [f for f in facilities if f["kind"] == "blood_bank"]
    hospitals = [f for f in facilities if f["kind"] == "hospital"]

    # ---------- stock: 15-40 units per bank, FEFO-realistic dates ----------
    groups, weights = list(GROUP_SHARES), list(GROUP_SHARES.values())
    units = []
    for bank in banks:
        for _ in range(rng.randint(15, 40)):
            component = "red_cells" if rng.random() < .8 else "plasma"
            collected = now - timedelta(days=rng.randint(1, 30), hours=rng.randint(0, 23))
            shelf = timedelta(days=35 if component == "red_cells" else 300)
            units.append((uuid.uuid4(), bank["id"], rng.choices(groups, weights)[0], component, collected, collected + shelf))
    await conn.copy_records_to_table("blood_units", records=units,
                                     columns=["id", "blood_bank_id", "blood_group", "component", "collected_at", "expires_at"])

    # ---------- donors: walk-in records (no login), approximate locations ----------
    # ponytail: COPY writes straight to the table and bypasses the API on purpose (2,000 rows in well under a second).
    # The DB CHECKs still apply. Phones are +910xxxxxxxxx: the CHECK accepts them, the API never does, and no Indian
    # number has a 0 right after +91, so a seeded number can never collide with a real signup or ring anyone (ADR 020).
    # sorted: a set's iteration order changes per process (string hashing is randomised), and rng.choice over it
    # would scatter donors differently on every run
    cities = sorted({(f["city"], f["latitude"], f["longitude"]) for f in hospitals} or
                    {(c, *p) for c, p in CITIES.items()})
    near_demo = sorted((f["city"], f["latitude"], f["longitude"]) for _, f in demo_logins if f["kind"] == "hospital")
    donor_rows = []
    for n in range(1, donor_count + 1):
        # 40% around the demo hospitals, so the walkthrough's donor alert finds verified donors on any snapshot
        _, lat, lng = rng.choice(near_demo if near_demo and n % 5 < 2 else cities)
        dlat, dlng = jitter(lat, lng, 15, rng)
        donated = None if rng.random() < .4 else now - timedelta(days=rng.randint(5, 365))
        donor_rows.append((
            uuid.uuid4(), f"{rng.choice(FIRST_NAMES)} Demo{n}", f"donor{n}@demo.raktdoot.in", rng.randint(18, 60),
            rng.choices(groups, weights)[0], rng.random() < .85, donated, now,
            round(dlat, 2), round(dlng, 2),        # the same ~1 km rounding the API applies
            rng.choices(("male", "female", "other"), (.48, .48, .04))[0],
            f"+910{n:09d}", now if rng.random() < .9 else None,     # ~10% not yet verified: never matched or counted
        ))
    await conn.copy_records_to_table("donors", records=donor_rows, columns=[
        "id", "name", "email", "age", "blood_group", "is_available", "last_donated_at", "consent_at", "latitude", "longitude",
        "sex", "phone", "phone_verified_at"])

    # ---------- demo accounts: one admin, one staff per facility, one donor login ----------
    chosen = os.environ.get("LIFELINK_DEMO_PASSWORD")      # e.g. inside the API container, which cannot write files
    password = chosen or secrets.token_urlsafe(12)
    password_hash = hash_password(password)
    accounts = [(uuid.uuid4(), "admin@demo.raktdoot.in", "admin", None)]
    for email, f in demo_logins or [
            (f"{f['kind'].replace('_', '')}-{str(f['id'])[:8]}@demo.raktdoot.in", f) for f in facilities]:
        accounts.append((uuid.uuid4(), email, f["kind"], f["id"]))
    donor_user = uuid.uuid4()
    accounts.append((donor_user, "donor@demo.raktdoot.in", "donor", None))
    await conn.executemany(
        "INSERT INTO users (id, email, password_hash, role, organization_id) VALUES ($1, $2, $3, $4, $5)",
        [(i, e, password_hash, r, o) for i, e, r, o in accounts])
    lat, lng = CITIES["Bhopal"]
    await conn.execute(
        "INSERT INTO donors (id, name, email, age, blood_group, consent_at, user_id, latitude, longitude, "
        "sex, phone, phone_verified_at) "
        "VALUES ($1, 'Demo Donor', 'donor@demo.raktdoot.in', 28, 'O-', $2, $3, $4, $5, 'female', '+910000000000', $2)",
        uuid.uuid4(), now, donor_user, round(lat, 2), round(lng, 2))

    names = {r["id"]: r["name"] for r in await conn.fetch("SELECT id, name FROM organizations")}
    accounts_text = [f"{e:50} {r:11} {names.get(o, '')}" for _, e, r, o in accounts]
    if chosen:      # the operator already knows the password: print the accounts, never the password
        print("\n".join(["[seed-demo] accounts (password: the LIFELINK_DEMO_PASSWORD you chose)", *accounts_text]))
    else:
        lines = [f"# Rakt Doot demo accounts (synthetic data). Password for ALL accounts: {password}", "", *accounts_text]
        credentials.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[seed-demo] {len(hospitals)} hospitals, {len(banks)} blood banks, {len(units)} units, "
          f"{donor_count + 1} donors, {len(accounts)} accounts"
          + ("" if chosen else f". Credentials: {credentials.name}"))
