"""The demo must never break in front of an interviewer: the seed runs, makes usable data, and refuses to run twice."""
import asyncio

from fastapi.testclient import TestClient

from conftest import _fetch, app_url, run_sql
from seed_demo import seed
from test_outbox import Recorder, drain


def test_seed_creates_a_working_demo_and_is_idempotent(test_url, fresh_db, tmp_path, settings):
    creds = tmp_path / "creds.txt"
    asyncio.run(seed(app_url(test_url), donors=50, credentials=creds))
    def count(sql):
        return asyncio.run(_fetch(test_url, sql))[0][0]

    assert count("SELECT count(*) FROM organizations WHERE kind = 'blood_bank'") == 12
    assert count("SELECT count(*) FROM donors") == 51
    assert count("SELECT count(*) FROM donors WHERE latitude <> round(latitude::numeric, 2)") == 0   # ~1 km rounding
    assert "admin@demo.raktdoot.in" in creds.read_text()
    asyncio.run(seed(app_url(test_url), donors=50, credentials=creds))                 # second run: no-op
    assert count("SELECT count(*) FROM donors") == 51

    # the seeded hospital account can log in and get blood allocated end to end
    password = creds.read_text().split("ALL accounts: ")[1].split()[0]
    hospital_email = next(line.split()[0] for line in creds.read_text().splitlines() if " hospital " in line)
    from main import create_app
    with TestClient(create_app(settings)) as c:
        token = c.post("/v1/auth/login", json={"email": hospital_email, "password": password}).json()["access_token"]
        hospital = {"Authorization": f"Bearer {token}"}
        r = c.post("/v1/emergencies", json={"blood_group": "AB+", "units_needed": 2}, headers=hospital)
        assert r.status_code == 201 and r.json()["units_allocated"] == 2         # AB+ can receive any red cells
        rid = r.json()["id"]
        # every PENDING hold is confirmed by that bank's own staff (auto-confirm banks start CONFIRMED), then fulfil
        for hold in [h for h in r.json()["holds"] if h["status"] == "PENDING"]:
            bank_email = f"bloodbank-{hold['blood_bank_id'][:8]}@demo.raktdoot.in"
            bank_token = c.post("/v1/auth/login", json={"email": bank_email, "password": password}).json()["access_token"]
            ok = c.post(f"/v1/emergencies/{rid}/holds/confirm", headers={"Authorization": f"Bearer {bank_token}"})
            assert ok.status_code == 200
        assert {h["status"] for h in c.get(f"/v1/emergencies/{rid}", headers=hospital).json()["holds"]} == {"CONFIRMED"}
        banks = sorted({h["blood_bank_id"] for h in r.json()["holds"]})
        done = c.post(f"/v1/emergencies/{rid}/fulfil", json={"collected_from": banks}, headers=hospital).json()
        assert (done["status"], done["units_collected"]) == ("FULFILLED", 2)

        # a Bhopal hospital alerts donors: verified ones are queued, and the demo donor can say yes
        bhopal = next(line.split()[0] for line in creds.read_text().splitlines() if " hospital " in line and "Bhopal" in line)
        token = c.post("/v1/auth/login", json={"email": bhopal, "password": password}).json()["access_token"]
        staff = {"Authorization": f"Bearer {token}"}
        req = c.post("/v1/emergencies", json={"blood_group": "O+", "units_needed": 1}, headers=staff).json()["id"]
        r = c.post(f"/v1/emergencies/{req}/notify-donors", params={"radius_km": 50, "limit": 50}, headers=staff)
        assert r.status_code == 202 and r.json()["queued"] > 0
        drain(test_url, Recorder())
        token = c.post("/v1/auth/login", json={"email": "donor@demo.raktdoot.in", "password": password}).json()["access_token"]
        donor = {"Authorization": f"Bearer {token}"}
        assert req in [i["request_id"] for i in c.get("/v1/emergencies/donor-inbox", headers=donor).json()]
        assert c.post(f"/v1/emergencies/{req}/responses", json={"accept": True}, headers=donor).status_code == 200

    # donor safety (ADR 020): every seeded donor has a sex and a unique, never-dialable +910 phone; some are unverified
    assert count("SELECT count(*) FROM donors WHERE sex IS NULL") == 0
    assert count("SELECT count(*) FROM donors WHERE phone !~ '^[+]910[0-9]{9}$'") == 0
    assert count("SELECT count(DISTINCT phone) FROM donors") == 51
    # banks (ADR 021): synthetic +910 phones, some auto-confirm and some do not, stock freshness from fresh to never
    assert count("SELECT count(*) FROM organizations WHERE kind = 'blood_bank' AND (phone IS NULL OR "
                 "phone !~ '^[+]910[0-9]{9}$')") == 0
    assert count("SELECT count(*) FROM organizations WHERE kind = 'blood_bank' AND auto_confirm_holds") >= 1
    assert count("SELECT count(*) FROM organizations WHERE kind = 'blood_bank' AND NOT auto_confirm_holds") >= 1
    assert count("SELECT count(*) FROM organizations WHERE kind = 'blood_bank' AND stock_confirmed_at IS NULL") >= 1
    assert count("SELECT count(*) FROM organizations WHERE kind = 'blood_bank' AND stock_confirmed_at IS NOT NULL") >= 1
    assert count("SELECT count(*) FROM donors WHERE phone_verified_at IS NOT NULL") >= 1
    assert count("SELECT count(*) FROM donors WHERE phone_verified_at IS NULL") >= 1
    assert count("SELECT count(*) FROM donors WHERE email = 'donor@demo.raktdoot.in' AND phone_verified_at IS NOT NULL") == 1


def test_a_chosen_demo_password_is_used_and_never_printed_or_written(test_url, fresh_db, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LIFELINK_DEMO_PASSWORD", "operator-chosen-demo-pw")
    creds = tmp_path / "creds.txt"
    asyncio.run(seed(app_url(test_url), donors=5, credentials=creds))
    out = capsys.readouterr().out
    assert not creds.exists() and "operator-chosen-demo-pw" not in out and "admin@demo.raktdoot.in" in out


# ---------- 2r (ADR 022): the demo on the real MP facilities ----------
TRUNCATE = ("TRUNCATE outbox, donor_responses, allocations, emergency_requests, refresh_tokens, login_failures, blood_units, "
            "donors, users, organizations, idempotency_keys, audit_events")


def walkthrough(test_url, settings, tmp_path) -> tuple:
    """The README walkthrough on a fresh import + seed, as numbers. Returns everything a reader would see."""
    from pathlib import Path

    from modules.organizations.service import import_osm_dir
    asyncio.run(run_sql(test_url, TRUNCATE))
    asyncio.run(import_osm_dir(app_url(test_url), Path(__file__).parent / "data" / "osm"))
    creds = tmp_path / "creds.txt"
    asyncio.run(seed(app_url(test_url), donors=2000, credentials=creds))
    password = creds.read_text().split("ALL accounts: ")[1].split()[0]
    from main import create_app
    with TestClient(create_app(settings)) as c:
        def login(email):
            r = c.post("/v1/auth/login", json={"email": email, "password": password})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}
        hospital, bank2, bank3 = (login(f"{e}@demo.raktdoot.in") for e in ("hospital1", "bank2", "bank3"))
        names = {r[0]: r[1] for r in asyncio.run(_fetch(test_url, "SELECT id::text, name FROM organizations "
                                                                   "WHERE id IN (SELECT organization_id FROM users)"))}
        # 1. AB- x 3, critical
        r = c.post("/v1/emergencies", json={"blood_group": "AB-", "units_needed": 3, "urgency": "CRITICAL"}, headers=hospital)
        assert r.status_code == 201
        rid = r.json()["id"]
        held = sorted((names[h["blood_bank_id"]], h["status"], h["units"]) for h in r.json()["holds"])
        # 2. the banks confirm on /bank
        for staff in (bank2, bank3):
            assert c.post(f"/v1/emergencies/{rid}/holds/confirm", headers=staff).status_code == 200
        # 3. ask nearby donors (the UI's defaults); the demo donor says yes
        queued = c.post(f"/v1/emergencies/{rid}/notify-donors", headers=hospital).json()["queued"]
        drain(test_url, Recorder())
        donor = login("donor@demo.raktdoot.in")
        assert c.post(f"/v1/emergencies/{rid}/responses", json={"accept": True}, headers=donor).status_code == 200
        # 4. collected from bank1 (the nurse) and bank2 (the bank's "Handed over"); close: bank3's unit goes back
        assert c.post(f"/v1/emergencies/{rid}/holds/issued", headers=bank2).status_code == 200
        b1 = asyncio.run(_fetch(test_url, "SELECT organization_id::text FROM users WHERE email = 'bank1@demo.raktdoot.in'"))[0][0]
        assert c.post(f"/v1/emergencies/{rid}/holds/{b1}/collected", headers=hospital).status_code == 200
        done = c.post(f"/v1/emergencies/{rid}/fulfil", json={"collected_from": []}, headers=hospital).json()
    users = asyncio.run(_fetch(test_url, "SELECT count(*) FROM users"))[0][0]
    confirmed_banks = asyncio.run(_fetch(test_url, "SELECT count(*) FROM organizations WHERE kind = 'blood_bank' "
                                                   "AND stock_confirmed_at IS NOT NULL"))[0][0]
    return held, queued, (done["status"], done["units_collected"]), users, confirmed_banks


def test_the_demo_on_real_facilities_logs_in_only_the_demo_set_and_is_reproducible(test_url, fresh_db, tmp_path,
                                                                                     settings):
    """AC 12: accounts only for 2 hospitals + 3 banks + admin + demo donor; other real banks have stock but never a
    confirmation time; the README walkthrough gives the same numbers on two fresh seeds."""
    first = walkthrough(test_url, settings, tmp_path)
    count = lambda sql: asyncio.run(_fetch(test_url, sql))[0][0]  # noqa: E731
    assert count("SELECT count(*) FROM users") == 7
    assert count("SELECT count(*) FROM users WHERE organization_id IS NOT NULL AND organization_id NOT IN "
                 "(SELECT id FROM organizations WHERE data_source = 'osm')") == 0
    assert count("SELECT count(*) FROM organizations WHERE data_source <> 'osm'") == 0      # no synthetic facilities
    assert count("SELECT count(*) FROM organizations o WHERE kind = 'blood_bank' AND NOT EXISTS "
                 "(SELECT 1 FROM blood_units b WHERE b.blood_bank_id = o.id)") == 0          # every real bank has stock
    assert count("SELECT count(*) FROM organizations WHERE auto_confirm_holds") == 1
    assert count("SELECT count(*) FROM organizations o WHERE stock_confirmed_at IS NOT NULL AND NOT EXISTS "
                 "(SELECT 1 FROM users u WHERE u.organization_id = o.id)") == 0               # only demo banks confirmed
    print("\n[walkthrough]", first)
    assert first[1] > 0 and first[2] == ("FULFILLED", 2) and first[3:] == (7, 3)
    assert walkthrough(test_url, settings, tmp_path) == first
