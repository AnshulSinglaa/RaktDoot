# ruff: noqa: F811  (the imported `w` and `clock` fixtures are requested by name as parameters)
"""QA attacks on bank-confirmed holds (ADR 021): races the engineer's suite does not cover, hoarding, closed requests,
time zones, and the multi-hospital last-unit race that the per-hospital cap lock now hides in test_emergencies.py."""
import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from conftest import TEST_SECRET, _fetch, app_url, as_role, audit_rows, make_user
from db import make_engine, make_sessionmaker
from factories import make_organization
from modules.emergencies.repository import EmergencyRepository
from security import make_access_token
from test_holds import (  # noqa: F401 (fixtures)
    AIIMS,
    T0,
    act,
    cancel,
    clock,
    fulfil,
    holds,
    post,
    run_expiry,
    stock,
    unit_status,
    w,
)


def staff_of(test_url, org_id):
    """A hospital staff token minted directly: 20 logins would trip the per-IP login rate limit."""
    user_id, _ = make_user(test_url, "hospital", org_id)
    token = make_access_token(uuid.UUID(user_id), "hospital", uuid.UUID(org_id), datetime.now(timezone.utc), 15,
                              TEST_SECRET)
    return {"Authorization": f"Bearer {token}"}


def alloc_rows(test_url, rid):
    return asyncio.run(_fetch(test_url, """SELECT a.released_at, a.release_reason, a.confirmed_at, u.status
        FROM allocations a JOIN blood_units u ON u.id = a.blood_unit_id WHERE a.request_id = $1::uuid""", rid))


# ---------- the original SKIP LOCKED property: MANY hospitals (each under its own cap lock) race for one unit ----------
def test_twenty_different_hospitals_race_for_the_last_unit_and_exactly_one_wins(client, w, test_url):
    (last,) = stock(client, w["near"], 1, group="O-")
    hospitals = [client.post("/v1/organizations", json=make_organization(kind="hospital", **AIIMS)).json()["id"]
                 for _ in range(20)]
    headers = [staff_of(test_url, h) for h in hospitals]
    with ThreadPoolExecutor(20) as pool:
        results = list(pool.map(lambda h: client.post("/v1/emergencies", json={"blood_group": "O-", "units_needed": 1},
                                                      headers=h), headers))
    assert [r.status_code for r in results] == [201] * 20
    winners = [r.json() for r in results if r.json()["units_allocated"] == 1]
    assert len(winners) == 1 and winners[0]["allocations"][0]["blood_unit_id"] == last
    live = asyncio.run(_fetch(test_url, "SELECT count(*) FROM allocations WHERE released_at IS NULL"))[0][0]
    assert live == 1


# ---------- closed requests ----------
def test_a_bank_cannot_confirm_or_reject_after_the_request_was_cancelled(client, w, clock, test_url):
    (unit,) = stock(client, w["near"])
    rid = post(client, w).json()["id"]
    assert cancel(client, w, rid).status_code == 200
    assert act(client, rid, "confirm", w["near_staff"]).status_code == 409
    assert act(client, rid, "reject", w["near_staff"], reason="not_in_stock").status_code == 409
    (row,) = alloc_rows(test_url, rid)
    assert (row["release_reason"], row["confirmed_at"], row["status"]) == ("cancelled", None, "AVAILABLE")
    assert unit_status(test_url, [unit]) == {unit: "AVAILABLE"}            # never DISCARDED by a late reject


def test_reject_is_for_the_owning_bank_staff_only(client, w, clock, test_url):
    stock(client, w["near"])
    rid = post(client, w).json()["id"]
    body = {"reason": "not_in_stock"}
    assert act(client, rid, "reject", w["far_staff"], **body).status_code == 404
    assert act(client, rid, "reject", w["staff"], **body).status_code == 403
    assert act(client, rid, "reject", {}, **body).status_code == 403                     # admin
    assert act(client, rid, "reject", as_role(client, test_url, "donor"), **body).status_code == 403
    assert act(client, str(uuid.uuid4()), "reject", w["near_staff"], **body).status_code == 404
    assert [h["status"] for h in holds(client, w, rid).values()] == ["PENDING"]
    assert client.get("/v1/emergencies/holds", headers={"Authorization": ""}).status_code == 401
    assert client.get("/v1/emergencies/holds").status_code == 403                       # admin: not a bank


# ---------- races the engineer did not run ----------
def test_reject_racing_expiry_never_discards_an_expired_hold_twice(client, w, clock, test_url):
    stock(client, w["near"], 8)
    rids = [post(client, w).json()["id"] for _ in range(8)]
    clock["t"] = T0 + timedelta(minutes=30) - timedelta(seconds=1)
    with ThreadPoolExecutor(9) as pool:
        expiry = pool.submit(run_expiry, test_url, T0 + timedelta(minutes=30), 1)
        codes = list(pool.map(lambda rid: act(client, rid, "reject", w["near_staff"], reason="not_in_stock").status_code,
                              rids))
        expiry.result()
    for rid, code in zip(rids, codes, strict=True):
        (row,) = alloc_rows(test_url, rid)
        if code == 200:
            assert (row["release_reason"], row["status"]) == ("rejected", "DISCARDED")
        else:
            assert (code, row["release_reason"], row["status"]) == (409, "expired", "AVAILABLE")


def test_a_reject_after_expiry_is_409_and_does_not_discard(client, w, clock, test_url):
    (unit,) = stock(client, w["near"])
    rid = post(client, w).json()["id"]
    assert run_expiry(test_url, T0 + timedelta(minutes=30)) == [1]
    assert act(client, rid, "reject", w["near_staff"], reason="not_in_stock").status_code == 409
    assert unit_status(test_url, [unit]) == {unit: "AVAILABLE"}


def test_fulfil_racing_expiry_never_issues_a_released_unit(client, w, clock, test_url):
    stock(client, w["near"], 8)
    rids = [post(client, w).json()["id"] for _ in range(8)]
    for rid in rids:
        assert act(client, rid, "confirm", w["near_staff"]).status_code == 200      # pickup deadline T0 + 6 h
    clock["t"] = T0 + timedelta(hours=6) - timedelta(seconds=1)
    with ThreadPoolExecutor(9) as pool:
        expiry = pool.submit(run_expiry, test_url, T0 + timedelta(hours=6), 1)
        codes = list(pool.map(lambda rid: fulfil(client, w, rid).status_code,
                              rids))
        expiry.result()
    for rid, code in zip(rids, codes, strict=True):
        (row,) = alloc_rows(test_url, rid)
        if code == 200:
            assert row["released_at"] is None and row["status"] == "ISSUED"
        else:
            assert (code, row["release_reason"], row["status"]) == (409, "expired", "AVAILABLE")
    bad = asyncio.run(_fetch(test_url, """SELECT count(*) FROM allocations a JOIN blood_units u ON u.id = a.blood_unit_id
        WHERE (a.released_at IS NOT NULL AND u.status = 'ISSUED') OR (a.released_at IS NULL AND u.status = 'AVAILABLE')"""))
    assert bad[0][0] == 0


# ---------- hoarding ----------
def test_allocate_repeats_cannot_get_past_the_cap(client, w, clock, test_url):
    stock(client, w["near"], 30)
    big = post(client, w, n=20).json()
    assert big["units_allocated"] == 20 and big["cap_reached"] is False
    small = post(client, w, n=5).json()
    assert small["units_allocated"] == 0 and small["cap_reached"] is True and small["status"] == "OPEN"
    for _ in range(3):
        again = client.post(f"/v1/emergencies/{small['id']}/allocate", headers=w["staff"])
        assert again.status_code == 200 and again.json()["units_allocated"] == 0 and again.json()["cap_reached"] is True
    reserved = asyncio.run(_fetch(test_url, "SELECT count(*) FROM blood_units WHERE status = 'RESERVED'"))[0][0]
    assert reserved == 20
    cancel(client, w, big['id'])        # room again
    freed = client.post(f"/v1/emergencies/{small['id']}/allocate", headers=w["staff"]).json()
    assert freed["units_allocated"] == 5 and freed["cap_reached"] is False
    assert [(h["status"], h["units"]) for h in freed["holds"]] == [("PENDING", 5)]   # /allocate returns the holds


def test_fulfilled_units_no_longer_count_against_the_cap(client, w, clock):
    stock(client, w["near"], 25)
    rid = post(client, w, n=20).json()["id"]
    act(client, rid, "confirm", w["near_staff"])
    done = fulfil(client, w, rid).json()
    assert done["status"] == "FULFILLED" and [h["status"] for h in done["holds"]] == ["ISSUED"]
    assert post(client, w, n=5).json()["units_allocated"] == 5


# ---------- time zones ----------
def test_expiry_with_a_non_utc_clock_uses_the_same_instant(client, w, clock, test_url):
    stock(client, w["near"], 2)
    rid = post(client, w).json()["id"]
    ist = timezone(timedelta(hours=5, minutes=30))
    assert run_expiry(test_url, (T0 + timedelta(minutes=30) - timedelta(seconds=1)).astimezone(ist)) == [0]
    assert run_expiry(test_url, (T0 + timedelta(minutes=30)).astimezone(ist)) == [1]
    assert [h["status"] for h in holds(client, w, rid).values()] == ["EXPIRED"]


def test_a_rejected_hold_cannot_be_confirmed_afterwards(client, w, clock, test_url):
    """Guards `released_at IS NULL` in the confirm UPDATE: a rejected hold still has a future expires_at."""
    (unit,) = stock(client, w["near"])
    rid = post(client, w).json()["id"]
    assert act(client, rid, "reject", w["near_staff"], reason="cannot_supply").status_code == 200
    r = act(client, rid, "confirm", w["near_staff"])
    assert r.status_code == 409 and r.json()["type"] == "urn:lifelink:problem:hold-not-pending"
    (row,) = alloc_rows(test_url, rid)
    assert (row["release_reason"], row["confirmed_at"], row["status"]) == ("rejected", None, "AVAILABLE")


# ---------- gate 2 (2026-10-20): the release endpoint and the revised expiry rules ----------
def overdue(client, w, clock, n=1, banks=("near",)):
    """A request whose holds at `banks` are confirmed and past pickup (clock left at T0 + 6 h)."""
    units = [u for b in banks for u in stock(client, w[b], n)]
    rid = post(client, w, n=n * len(banks)).json()["id"]
    for b in banks:
        assert act(client, rid, "confirm", w[f"{b}_staff"]).status_code == 200
    clock["t"] = T0 + timedelta(hours=6)
    return rid, units


def test_a_bank_action_returns_its_own_status_and_unit_count(client, w, clock):
    """Breaks if bank_view stops filtering by bank: near holds 2, far holds 1."""
    stock(client, w["near"], 2)
    stock(client, w["far"], 1)
    rid = post(client, w, n=3).json()["id"]
    near = act(client, rid, "confirm", w["near_staff"]).json()
    far = act(client, rid, "reject", w["far_staff"], reason="cannot_supply").json()
    assert (near["status"], near["units"]) == ("CONFIRMED", 2)
    assert (far["status"], far["units"]) == ("REJECTED", 1)


def test_overdue_confirmed_holds_never_starve_the_expiry_listing(client, w, clock, test_url):
    """Confirmed holds past pickup are never auto-released, so if the listing took them they would fill every page."""
    stock(client, w["near"], 2)
    old = post(client, w).json()["id"]
    act(client, old, "confirm", w["near_staff"])                              # deadline T0 + 6 h
    clock["t"] = T0 + timedelta(hours=6)
    pending = post(client, w).json()["id"]                                    # deadline T0 + 6 h 30 min

    async def first():
        engine = make_engine(app_url(test_url), 1, 0)
        try:
            async with make_sessionmaker(engine)() as s:
                return await EmergencyRepository(s).requests_with_expired_holds(T0 + timedelta(hours=7), limit=1)
        finally:
            await engine.dispose()
    assert [str(r) for r in asyncio.run(first())] == [pending]


def test_release_after_cancel_is_409_and_changes_nothing(client, w, clock, test_url):
    rid, (unit,) = overdue(client, w, clock)
    assert cancel(client, w, rid).status_code == 200
    r = act(client, rid, "release", w["near_staff"])
    assert r.status_code == 409 and r.json()["type"] == "urn:lifelink:problem:hold-not-overdue"
    (row,) = alloc_rows(test_url, rid)
    assert (row["release_reason"], row["status"]) == ("cancelled", "AVAILABLE")
    assert "hold.released" not in [e["event"] for e in audit_rows(test_url, rid)]


def test_release_on_an_unknown_request_is_404(client, w, clock):
    assert act(client, str(uuid.uuid4()), "release", w["near_staff"]).status_code == 404


def test_one_bank_releasing_leaves_the_other_banks_overdue_hold_alone(client, w, clock, test_url):
    rid, units = overdue(client, w, clock, banks=("near", "far"))
    assert act(client, rid, "release", w["near_staff"]).status_code == 200
    statuses = {h["blood_bank_id"]: h["status"] for h in client.get(f"/v1/emergencies/{rid}", headers=w["staff"]).json()["holds"]}
    assert statuses == {w["near"]: "RELEASED", w["far"]: "OVERDUE"}
    assert sorted(unit_status(test_url, units).values()) == ["AVAILABLE", "RESERVED"]


def test_two_release_clicks_at_once_release_once(client, w, clock, test_url):
    rid, units = overdue(client, w, clock, n=3)
    with ThreadPoolExecutor(6) as pool:
        codes = list(pool.map(lambda _: act(client, rid, "release", w["near_staff"]).status_code, range(6)))
    assert codes == [200] * 6
    assert [e["event"] for e in audit_rows(test_url, rid)].count("hold.released") == 1
    assert set(unit_status(test_url, units).values()) == {"AVAILABLE"}
    assert {(r["release_reason"], r["status"]) for r in alloc_rows(test_url, rid)} == {("released", "AVAILABLE")}


def test_release_racing_fulfil_ends_in_exactly_one(client, w, clock, test_url):
    """Fulfil (units ISSUED) and release (units AVAILABLE) on the same overdue hold: one wins, never both."""
    cases = []
    for _ in range(4):
        clock["t"] = T0
        cases.append(overdue(client, w, clock))
    with ThreadPoolExecutor(8) as pool:
        futures = [(pool.submit(fulfil, client, w, rid),
                    pool.submit(act, client, rid, "release", w["near_staff"]), rid) for rid, _ in cases]
        for closing, release, rid in futures:
            f, r = closing.result().status_code, release.result().status_code
            # ADR 021 rev 2: fulfil names the bank collected from. If release won, that bank has no live hold left,
            # so fulfil is 422 unknown-bank (before: 409 invalid-transition, OPEN could not be fulfilled); if fulfil
            # won, release is 409.
            assert (f, r) in ((200, 409), (422, 200)), (f, r)
            (row,) = alloc_rows(test_url, rid)
            if f == 200:
                assert (row["released_at"], row["status"]) == (None, "ISSUED")
            else:
                assert (row["release_reason"], row["status"]) == ("released", "AVAILABLE")
