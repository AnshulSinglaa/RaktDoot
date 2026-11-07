"""Emergency requests and allocation. The headline: no blood unit is ever allocated twice, under any concurrency."""
import asyncio
import re
from concurrent.futures import ThreadPoolExecutor

import asyncpg
import pytest

from conftest import _fetch, as_role, audit_rows, make_user, run_sql
from factories import make_blood_unit, make_organization
from modules.emergencies import router as emergencies_router

# Real places, so distances are real: AIIMS Bhopal, Hamidia Hospital (~8 km away), MY Hospital Indore (~170 km away).
AIIMS = {"latitude": 23.2080, "longitude": 77.4600}
HAMIDIA = {"latitude": 23.2599, "longitude": 77.3920}
INDORE = {"latitude": 22.7196, "longitude": 75.8577}


@pytest.fixture
def world(client, test_url):
    """A hospital in Bhopal, a near and a far blood bank, and a logged-in staff member of the hospital."""
    def org(kind, at):
        return client.post("/v1/organizations", json=make_organization(kind=kind, **at)).json()["id"]

    w = {"hospital": org("hospital", AIIMS), "near": org("blood_bank", HAMIDIA), "far": org("blood_bank", INDORE)}
    for bank in ("near", "far"):
        make_user(test_url, "blood_bank", w[bank])           # onboarded: someone can confirm a hold
    w["staff"] = as_role(client, test_url, "hospital", w["hospital"])
    return w


def stock(client, bank, group="O+", n=1, **kw):
    return [client.post("/v1/blood-units", json=make_blood_unit(bank, blood_group=group, **kw)).json()["id"]
            for _ in range(n)]


def post(client, world, **body):
    return client.post("/v1/emergencies", json={"blood_group": "O+", "units_needed": 1, **body}, headers=world["staff"])


def units_with(client, status):
    return {u["id"] for u in client.get("/v1/blood-units", params={"status": status, "limit": 100}).json()["items"]}


# ---------- allocation rules ----------
def test_create_reserves_from_the_nearest_bank_and_prefers_the_exact_group(client, world, test_url):
    stock(client, world["far"], "O+", 5)                      # plenty, but 170 km away: outside the 50 km radius
    exact = stock(client, world["near"], "A+", 1)
    stock(client, world["near"], "O-", 1)                     # compatible with A+, but scarce: must NOT be taken first
    r = post(client, world, blood_group="A+")
    body = r.json()
    assert r.status_code == 201 and r.headers["location"] == f"/v1/emergencies/{body['id']}"
    assert body["status"] == "ALLOCATED" and body["units_allocated"] == 1
    assert [a["blood_unit_id"] for a in body["allocations"]] == exact
    assert units_with(client, "RESERVED") == set(exact)
    assert audit_rows(test_url, body["id"])[0]["event"] == "emergency.created"


def test_partial_stock_leaves_the_request_open_and_allocate_tops_it_up(client, world):
    stock(client, world["near"], "O+", 1)
    body = post(client, world, units_needed=3).json()
    assert body["status"] == "OPEN" and body["units_allocated"] == 1
    stock(client, world["near"], "O-", 2)                     # a donation camp restocks the bank
    again = client.post(f"/v1/emergencies/{body['id']}/allocate", headers=world["staff"]).json()
    assert again["status"] == "ALLOCATED" and again["units_allocated"] == 3


def test_incompatible_groups_are_never_allocated(client, world):
    stock(client, world["near"], "AB+", 3)                    # an O+ patient cannot receive AB+
    assert post(client, world).json()["units_allocated"] == 0


def test_plasma_uses_the_reverse_rule(client, world):
    ab_plasma = stock(client, world["near"], "AB-", 1, component="plasma")     # AB plasma: universal donor
    body = post(client, world, blood_group="O+", component="plasma").json()
    assert [a["blood_unit_id"] for a in body["allocations"]] == ab_plasma


def test_a_hospital_without_a_location_cannot_post(client, test_url):
    hospital = client.post("/v1/organizations", json=make_organization(kind="hospital")).json()["id"]
    staff = as_role(client, test_url, "hospital", hospital)
    r = client.post("/v1/emergencies", json={"blood_group": "O+", "units_needed": 1}, headers=staff)
    assert r.status_code == 422 and r.json()["type"] == "urn:lifelink:problem:hospital-has-no-location"


def test_only_hospital_staff_post_and_other_hospitals_see_nothing(client, world, test_url):
    stock(client, world["near"])
    mine = post(client, world).json()
    other = as_role(client, test_url, "hospital")                      # staff of ANOTHER hospital
    assert client.get(f"/v1/emergencies/{mine['id']}", headers=other).status_code == 404     # BOLA: same as missing
    assert client.post(f"/v1/emergencies/{mine['id']}/cancel", json={"collected_from": []}, headers=other).status_code == 404
    donor = as_role(client, test_url, "donor")
    assert client.post("/v1/emergencies", json={"blood_group": "O+", "units_needed": 1}, headers=donor).status_code == 403
    assert [e["id"] for e in client.get("/v1/emergencies", headers=world["staff"]).json()] == [mine["id"]]
    assert client.get("/v1/emergencies", headers=other).json() == []


def test_hospital_id_cannot_be_spoofed_in_the_body(client, world):
    r = post(client, world, hospital_id="00000000-0000-0000-0000-000000000000")
    assert r.status_code == 422


# ---------- lifecycle ----------
def test_fulfil_issues_the_units(client, world, test_url):
    units = stock(client, world["near"], n=2)
    body = post(client, world, units_needed=2).json()
    client.post(f"/v1/emergencies/{body['id']}/holds/confirm", headers=as_role(client, test_url, "blood_bank", world["near"]))
    done = client.post(f"/v1/emergencies/{body['id']}/fulfil", json={"collected_from": [world["near"]]},
                       headers=world["staff"]).json()
    assert done["status"] == "FULFILLED" and units_with(client, "ISSUED") == set(units)
    r = client.post(f"/v1/emergencies/{body['id']}/cancel", json={"collected_from": []}, headers=world["staff"])
    assert r.status_code == 409 and r.json()["type"] == "urn:lifelink:problem:invalid-transition"


def test_cancel_puts_units_back_and_another_request_can_take_them(client, world):
    units = stock(client, world["near"], n=1)
    first = post(client, world).json()
    cancelled = client.post(f"/v1/emergencies/{first['id']}/cancel", json={"collected_from": []}, headers=world["staff"]).json()
    assert cancelled["status"] == "CANCELLED" and cancelled["units_allocated"] == 0
    assert units_with(client, "AVAILABLE") == set(units)
    second = post(client, world).json()                         # the released unit is allocatable again
    assert [a["blood_unit_id"] for a in second["allocations"]] == units


def test_a_request_with_nothing_collected_cannot_be_fulfilled(client, world):
    """OPEN -> FULFILLED is legal since ADR 021 rev 2 (partly fulfilled), but only if some blood was actually received."""
    body = post(client, world).json()                           # no stock: stays OPEN, nothing held
    r = client.post(f"/v1/emergencies/{body['id']}/fulfil", json={"collected_from": []}, headers=world["staff"])
    assert r.status_code == 409 and r.json()["type"] == "urn:lifelink:problem:nothing-collected"


def test_a_retry_with_the_same_idempotency_key_reserves_nothing_new(client, world):
    stock(client, world["near"], n=4)
    headers = {**world["staff"], "Idempotency-Key": "er-001"}
    body = {"blood_group": "O+", "units_needed": 2}
    first = client.post("/v1/emergencies", json=body, headers=headers)
    retry = client.post("/v1/emergencies", json=body, headers=headers)
    assert retry.headers["idempotent-replayed"] == "true" and retry.json() == first.json()
    assert len(units_with(client, "RESERVED")) == 2           # not 4: the timeout retry did not double-reserve


def test_an_overlong_idempotency_key_is_a_422_not_a_500(client, world):
    """B9: '<user id>:<key>' must fit the 255-char CHECK in idempotency_keys."""
    body = {"blood_group": "O+", "units_needed": 1}
    for n, code in ((201, 422), (200, 201)):
        r = client.post("/v1/emergencies", json=body, headers={**world["staff"], "Idempotency-Key": "k" * n})
        assert r.status_code == code, r.text


# ---------- THE correctness property ----------
def test_twenty_parallel_requests_from_one_hospital_get_the_last_unit_once(client, world):
    """One hospital, twenty parallel posts: its cap lock (ADR 021) queues them, so this proves "no unit twice, no 500"
    for one hospital. The real cross-hospital SKIP LOCKED race is
    test_qa_holds_attacks.py::test_twenty_different_hospitals_race_for_the_last_unit_and_exactly_one_wins."""
    (last,) = stock(client, world["near"], "O-", 1)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: post(client, world, blood_group="O-"), range(20)))
    assert [r.status_code for r in results] == [201] * 20                       # nobody gets a 500
    winners = [r.json() for r in results if r.json()["units_allocated"] == 1]
    assert len(winners) == 1 and winners[0]["allocations"][0]["blood_unit_id"] == last
    assert sum(r.json()["status"] == "OPEN" for r in results) == 19


def test_twelve_requests_share_five_units_without_any_unit_twice(client, world):
    units = set(stock(client, world["near"], "O+", 5))
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = [r.json() for r in pool.map(lambda _: post(client, world), range(12))]
    taken = [a["blood_unit_id"] for r in results for a in r["allocations"]]
    assert sorted(taken) == sorted(units)                     # every unit used exactly once, none twice
    assert units_with(client, "RESERVED") == units


def test_the_database_itself_refuses_a_second_live_allocation(client, world, test_url):
    """Defence in depth: even code that skipped the lock cannot hold one unit in two live allocations."""
    stock(client, world["near"])
    body = post(client, world).json()
    unit = body["allocations"][0]["blood_unit_id"]
    duplicate = (
        "INSERT INTO allocations (id, request_id, blood_unit_id, blood_bank_id, blood_group, allocated_at, expires_at) "
        f"SELECT gen_random_uuid(), request_id, blood_unit_id, blood_bank_id, blood_group, now(), now() "
        f"FROM allocations WHERE blood_unit_id = '{unit}'"
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        asyncio.run(run_sql(test_url, duplicate))


# ---------- only banks that can answer are held ----------
def test_a_bank_without_staff_is_never_held_even_when_nearest_but_families_still_see_it(client, world):
    """Nobody at a staff-less bank can confirm a hold, so its units must not be locked away from everyone else.
    It still appears in public search: a family can phone it."""
    lonely = client.post("/v1/organizations", json=make_organization(kind="blood_bank", **AIIMS)).json()["id"]
    lonely_unit = stock(client, lonely)                       # 0 km from the hospital: nearest of all
    near_unit = stock(client, world["near"])
    body = post(client, world).json()
    assert body["status"] == "ALLOCATED" and [a["blood_unit_id"] for a in body["allocations"]] == near_unit
    assert set(lonely_unit) <= units_with(client, "AVAILABLE")
    public = client.get("/v1/public/nearest-banks", params={"recipient": "O+", **AIIMS}, headers={"Authorization": ""})
    assert public.json()[0]["blood_bank_id"] == lonely


def test_an_inactive_staff_account_does_not_make_a_bank_answerable(client, world, test_url):
    bank = client.post("/v1/organizations", json=make_organization(kind="blood_bank", **AIIMS)).json()["id"]
    user_id, _ = make_user(test_url, "blood_bank", bank)
    asyncio.run(run_sql(test_url, f"UPDATE users SET is_active = false WHERE id = '{user_id}'"))
    stock(client, bank)
    body = post(client, world).json()
    assert body["status"] == "OPEN" and body["units_allocated"] == 0


def test_the_staff_filter_runs_before_the_limit(client, world, test_url):
    """60 staff-less banks are nearer than the one onboarded bank. Dropping them AFTER the LIMIT 50 of nearby() would
    leave nothing to hold; the filter is inside the SQL, so the onboarded bank is still reached."""
    asyncio.run(run_sql(test_url, "INSERT INTO organizations (id, kind, name, city, latitude, longitude) "
                                  "SELECT gen_random_uuid(), 'blood_bank', 'Unboarded ' || i, 'Bhopal', 23.2080, 77.4600 "
                                  "FROM generate_series(1, 60) AS i"))
    unit = stock(client, world["near"])
    body = post(client, world).json()
    assert body["status"] == "ALLOCATED" and [a["blood_unit_id"] for a in body["allocations"]] == unit


def test_staffed_banks_without_stock_do_not_use_up_the_limit(client, world, test_url):
    """B6: 60 STAFFED banks nearer than the one with stock, all empty. The stock filter used to run after nearby()'s
    LIMIT 50, so the 50 empty ones filled the list and the bank with blood was never tried."""
    asyncio.run(run_sql(test_url, """
        WITH banks AS (
            INSERT INTO organizations (id, kind, name, city, latitude, longitude)
            SELECT gen_random_uuid(), 'blood_bank', 'Empty ' || i, 'Bhopal', 23.2080, 77.4600
            FROM generate_series(1, 60) AS i RETURNING id)
        INSERT INTO users (id, email, password_hash, role, organization_id)
        SELECT gen_random_uuid(), 'empty-' || id || '@example.com', 'x', 'blood_bank', id FROM banks"""))
    unit = stock(client, world["near"])
    body = post(client, world).json()
    assert body["status"] == "ALLOCATED" and [a["blood_unit_id"] for a in body["allocations"]] == unit


# ---------- donor matching (PostGIS) ----------
def donor(client, at, group="O+", **kw):
    from factories import make_donor
    return client.post("/v1/donors", json=make_donor(blood_group=group, latitude=at[0], longitude=at[1], **kw)).json()


def test_donor_matches_are_compatible_eligible_available_near_and_minimal(client, world):
    near = donor(client, (23.215, 77.465), "O-", name="Asha Verma")                  # < 1 km from AIIMS
    mid = donor(client, (23.2599, 77.3920), "O+")                                    # ~8 km
    donor(client, (23.21, 77.46), "AB+")                                             # incompatible with O+
    donor(client, (22.72, 75.86), "O+")                                              # Indore: outside 10 km
    recent = donor(client, (23.21, 77.46), "O+", last_donated_at="2026-10-01T00:00:00Z")    # not eligible yet
    away = donor(client, (23.21, 77.46), "O+")
    client.patch(f"/v1/donors/{away['id']}", json={"is_available": False})            # opted out
    req = post(client, world).json()                                                   # O+ patient, no bank stock
    r = client.get(f"/v1/emergencies/{req['id']}/donor-matches", headers=world["staff"])
    assert r.status_code == 200
    got = r.json()
    assert [m["donor_id"] for m in got] == [near["id"], mid["id"]]                    # nearest first
    assert got[0] == {"donor_id": near["id"], "first_name": "Asha", "blood_group": "O-", "distance_band": "under 2 km"}
    assert got[1]["distance_band"] == "5-10 km"
    assert recent["id"] not in [m["donor_id"] for m in got]


def test_donor_location_is_stored_rounded_to_about_a_km(client, world, test_url):
    d = donor(client, (23.214567, 77.465432))
    (row,) = asyncio.run(_fetch(test_url, "SELECT latitude, longitude FROM donors WHERE id = $1::uuid", d["id"]))
    assert (row["latitude"], row["longitude"]) == (23.21, 77.47)


def test_donor_matches_are_for_the_owning_hospital_and_live_requests_only(client, world, test_url):
    req = post(client, world).json()
    other = as_role(client, test_url, "hospital")
    assert client.get(f"/v1/emergencies/{req['id']}/donor-matches", headers=other).status_code == 404
    bank = as_role(client, test_url, "blood_bank")
    assert client.get(f"/v1/emergencies/{req['id']}/donor-matches", headers=bank).status_code == 403
    client.post(f"/v1/emergencies/{req['id']}/cancel", json={"collected_from": []}, headers=world["staff"])
    assert client.get(f"/v1/emergencies/{req['id']}/donor-matches", headers=world["staff"]).status_code == 409
    assert client.get(f"/v1/emergencies/{req['id']}/donor-matches?radius_km=500", headers=world["staff"]).status_code == 422


def test_every_donor_lookup_is_audited(client, world, test_url):
    req = post(client, world).json()
    client.get(f"/v1/emergencies/{req['id']}/donor-matches", headers=world["staff"])
    assert "emergency.donors_viewed" in [r["event"] for r in audit_rows(test_url, req["id"])]


# ---------- 2c: request code, hospital reference, demo-safe phones (migration 0017, R2) ----------
CODE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{8}$")                  # Crockford base32: no I, L, O, U


def test_every_request_gets_a_unique_8_character_code_and_a_replay_keeps_it(client, world):
    codes = [post(client, world).json()["code"] for _ in range(5)]
    assert all(CODE.match(c) for c in codes) and len(set(codes)) == 5
    headers = {**world["staff"], "Idempotency-Key": "code-001"}
    first = client.post("/v1/emergencies", json={"blood_group": "O+", "units_needed": 1}, headers=headers).json()
    replay = client.post("/v1/emergencies", json={"blood_group": "O+", "units_needed": 1}, headers=headers).json()
    assert replay["code"] == first["code"] and replay["id"] == first["id"]
    assert client.get(f"/v1/emergencies/{first['id']}", headers=world["staff"]).json()["code"] == first["code"]


def test_a_code_collision_retries_once_with_a_new_code(client, world, monkeypatch):
    """Break-it (run): without the SAVEPOINT the retry runs in an aborted transaction and the first case is a 500."""
    taken = post(client, world).json()["code"]
    codes = iter([taken, "NEWC0DE1"])
    monkeypatch.setattr(emergencies_router, "short_code", lambda: next(codes))
    r = post(client, world)
    assert r.status_code == 201 and r.json()["code"] == "NEWC0DE1"
    codes = iter([taken, taken])                                 # twice unlucky: a 500, never a duplicate code
    monkeypatch.setattr(emergencies_router, "short_code", lambda: next(codes))
    assert post(client, world).status_code == 500                 # logged as an unhandled IntegrityError


@pytest.mark.parametrize("ref, ok", [("IP 2026/1234 #B-12", True), ("A" * 30, True), ("A" * 31, False),
                                     ("<script>", False), ("Mr. R. Sharma", False), ("", False), ("bed 7\n", False)])
def test_hospital_ref_is_short_and_plain(client, world, ref, ok):
    r = post(client, world, hospital_ref=ref)
    assert r.status_code == (201 if ok else 422), r.text
    if ok:
        assert r.json()["hospital_ref"] == ref


def test_hospital_ref_reaches_the_ward_and_the_holding_bank_only(client, world, test_url):
    stock(client, world["near"], n=2)
    near_staff, far_staff = as_role(client, test_url, "blood_bank", world["near"]), \
        as_role(client, test_url, "blood_bank", world["far"])
    body = post(client, world, hospital_ref="BED-7Q").json()
    rid = body["id"]
    assert client.get(f"/v1/emergencies/{rid}", headers=world["staff"]).json()["hospital_ref"] == "BED-7Q"
    (hold,) = client.get("/v1/emergencies/holds", headers=near_staff).json()
    assert (hold["code"], hold["hospital_ref"]) == (body["code"], "BED-7Q")
    assert client.get("/v1/emergencies/holds", headers=far_staff).json() == []
    assert client.post(f"/v1/emergencies/{rid}/holds/confirm", headers=far_staff).status_code == 404
    assert client.post(f"/v1/emergencies/{rid}/holds/confirm", headers=near_staff).json()["hospital_ref"] == "BED-7Q"
    public = client.get("/v1/public/nearest-banks", params={"recipient": "O+", **AIIMS}, headers={"Authorization": ""})
    assert "BED-7Q" not in public.text and body["code"] not in public.text
    from modules.emergencies.schemas import InboxItem
    assert not {"hospital_ref", "code"} & set(InboxItem.model_fields)              # the donor inbox never carries it
    every_row = asyncio.run(_fetch(test_url, "SELECT * FROM audit_events"))
    assert "BED-7Q" not in str([dict(r) for r in every_row])                       # nor any audit row


def test_hospital_ref_is_nulled_when_the_request_is_closed(client, world, test_url):
    """B13: the bed/IP number is needed only while blood is moving; closing (cancel or fulfil) erases it."""
    cancelled = post(client, world, hospital_ref="BED-1").json()["id"]          # no stock yet: OPEN, nothing held
    r = client.post(f"/v1/emergencies/{cancelled}/cancel", json={"collected_from": []}, headers=world["staff"])
    assert r.status_code == 200 and r.json()["hospital_ref"] is None
    stock(client, world["near"])
    fulfilled = post(client, world, hospital_ref="BED-2").json()["id"]
    r = client.post(f"/v1/emergencies/{fulfilled}/fulfil", json={"collected_from": [world["near"]]},
                    headers=world["staff"])
    assert r.status_code == 200 and r.json()["hospital_ref"] is None
    rows = asyncio.run(_fetch(test_url, "SELECT hospital_ref FROM emergency_requests"))
    assert [r["hospital_ref"] for r in rows] == [None, None]                     # gone from the database too


def test_demo_facilities_are_flagged_so_no_desk_dials_a_real_number_about_a_fake_request(client, world, test_url):
    """R2: HoldRead.bank_is_demo and BankHold.hospital_is_demo follow data_source (osm/synthetic = demo)."""
    near_staff = as_role(client, test_url, "blood_bank", world["near"])
    client.patch(f"/v1/organizations/{world['hospital']}", json={"phone": "0755 2345678"})
    stock(client, world["near"], n=3)
    rid = post(client, world).json()["id"]

    def flags():
        (h,) = client.get(f"/v1/emergencies/{rid}", headers=world["staff"]).json()["holds"]
        (b,) = client.get("/v1/emergencies/holds", headers=near_staff).json()
        return h["bank_is_demo"], b["hospital_is_demo"], b["hospital_phone"]
    assert flags() == (False, False, "+917552345678")                               # manual rows: real desks
    for source in ("osm", "synthetic"):
        asyncio.run(run_sql(test_url, f"UPDATE organizations SET data_source = '{source}', "
                                      "osm_id = 'node/' || abs(hashtext(id::text)) "
                                      f"WHERE id IN ('{world['near']}', '{world['hospital']}')"))
        assert flags() == (True, True, "+917552345678")
