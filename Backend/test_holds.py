"""Bank-confirmed holds with expiry, the per-hospital cap, stock freshness and facility settings (ADR 021, migration 0013).

A hold = the allocations rows of one (request, bank). PENDING until that bank's staff confirm (30 min), CONFIRMED until
the hospital collects (6 h); a rejected or expired hold gives its units back and the request reopens."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from conftest import _fetch, app_url, as_role, audit_rows
from db import make_engine, make_sessionmaker
from factories import make_blood_unit, make_organization
from main import get_now
from modules.emergencies import router as emergencies_router
from modules.emergencies import service as emergencies_service
from modules.emergencies.service import expire_holds

AIIMS = {"latitude": 23.2080, "longitude": 77.4600}
HAMIDIA = {"latitude": 23.2599, "longitude": 77.3920}        # ~8 km: the nearest bank
FAR = {"latitude": 23.3300, "longitude": 77.3000}            # ~21 km: the second bank
T0 = datetime.now(timezone.utc).replace(microsecond=0)       # pinned; tokens are checked against the real clock


@pytest.fixture
def clock(app, client):
    """A movable clock for every endpoint. `client` (admin) logged in first, so its token is unaffected."""
    now = {"t": T0}
    app.dependency_overrides[get_now] = lambda: now["t"]
    yield now
    app.dependency_overrides.pop(get_now, None)


@pytest.fixture
def w(client, test_url):
    """A Bhopal hospital, a near and a far blood bank, and a logged-in member of staff for each."""
    def org(kind, at, **kw):
        return client.post("/v1/organizations", json=make_organization(kind=kind, **at, **kw)).json()["id"]

    ids = {"hospital": org("hospital", AIIMS), "near": org("blood_bank", HAMIDIA), "far": org("blood_bank", FAR)}
    return {**ids, "staff": as_role(client, test_url, "hospital", ids["hospital"]),
            "near_staff": as_role(client, test_url, "blood_bank", ids["near"]),
            "far_staff": as_role(client, test_url, "blood_bank", ids["far"])}


def stock(client, bank, n=1, group="O+"):
    return [client.post("/v1/blood-units", json=make_blood_unit(bank, blood_group=group)).json()["id"] for _ in range(n)]


def post(client, w, n=1, headers=None, **kw):
    return client.post("/v1/emergencies", json={"blood_group": "O+", "units_needed": n, **kw}, headers=headers or w["staff"])


def act(client, rid, what, headers, **body):
    return client.post(f"/v1/emergencies/{rid}/holds/{what}", headers=headers, json=body or None)


def fulfil(client, w, rid, *banks, headers=None):
    """Close as received, naming the banks collected from now (default: the near bank)."""
    return client.post(f"/v1/emergencies/{rid}/fulfil", json={"collected_from": [w[b] for b in (banks or ("near",))]},
                       headers=w["staff"] if headers is None else headers)


def cancel(client, w, rid, *banks):
    return client.post(f"/v1/emergencies/{rid}/cancel", json={"collected_from": [w[b] for b in banks]},
                       headers=w["staff"])


def collected(client, w, rid, bank="near", headers=None):
    return client.post(f"/v1/emergencies/{rid}/holds/{w[bank]}/collected",
                       headers=w["staff"] if headers is None else headers)


def holds(client, w, rid):
    view = client.get(f"/v1/emergencies/{rid}", headers=w["staff"]).json()["holds"]
    return {(h["blood_bank_id"], h["status"]): h for h in view}


def unit_status(test_url, ids):
    rows = asyncio.run(_fetch(test_url, "SELECT id::text, status FROM blood_units WHERE id = ANY($1::uuid[])", ids))
    return {r["id"]: r["status"] for r in rows}


def run_expiry(test_url, now, runs=1):
    """`runs` expiry passes CONCURRENTLY, as the app role, like `runs` workers."""
    async def go():
        engine = make_engine(app_url(test_url), 5, 0)
        try:
            sm = make_sessionmaker(engine)
            return await asyncio.gather(*[expire_holds(sm, now) for _ in range(runs)])
        finally:
            await engine.dispose()
    return asyncio.run(go())


@pytest.fixture
def published(monkeypatch):
    """Every live-status event the hold code publishes (the SSE path itself is proven in test_live.py)."""
    kinds = []

    def spy(original):
        async def wrapper(session, request_id, kind):
            kinds.append(kind)
            await original(session, request_id, kind)
        return wrapper
    monkeypatch.setattr(emergencies_router, "publish", spy(emergencies_router.publish))
    monkeypatch.setattr(emergencies_service, "publish", spy(emergencies_service.publish))
    return kinds


# ---------- 3: creation ----------
def test_a_new_hold_is_pending_for_30_minutes(client, w, clock):
    stock(client, w["near"])
    body = post(client, w).json()
    (hold,) = body["holds"]
    assert (hold["status"], hold["units"], hold["blood_bank_id"]) == ("PENDING", 1, w["near"])
    assert datetime.fromisoformat(hold["expires_at"]) == T0 + timedelta(minutes=30)
    assert body["status"] == "ALLOCATED" and body["cap_reached"] is False


def test_an_auto_confirm_bank_starts_confirmed_for_6_hours(client, w, clock):
    assert client.patch(f"/v1/organizations/{w['near']}/bank-settings", json={"auto_confirm_holds": True},
                        headers=w["near_staff"]).json()["auto_confirm_holds"] is True
    stock(client, w["near"])
    (hold,) = post(client, w).json()["holds"]
    assert hold["status"] == "CONFIRMED" and datetime.fromisoformat(hold["expires_at"]) == T0 + timedelta(hours=6)


def test_only_units_that_outlive_the_hold_are_reserved(client, w, clock):
    """B4: FEFO picks the soonest-expiring unit. A hold can last confirm (30 min) + pickup (6 h), so a unit expiring
    before then must never be promised: the ward would collect a bag that is already out of date."""
    def unit(hours):
        at = (T0 + timedelta(hours=hours)).isoformat()
        return client.post("/v1/blood-units", json=make_blood_unit(w["near"], expires_at=at)).json()["id"]

    unit(3)                                                     # expires before the latest pickup: never held
    body = post(client, w).json()
    assert body["units_allocated"] == 0 and body["status"] == "OPEN"
    later = unit(7)                                             # 7 h > 6 h 30 min
    again = client.post(f"/v1/emergencies/{body['id']}/allocate", headers=w["staff"]).json()
    assert [a["blood_unit_id"] for a in again["allocations"]] == [later]


def test_an_expired_unit_is_never_confirmed_or_collected(client, w, clock, test_url):
    """B4: if a held unit passes its label expiry anyway (relabelled, clock skew, an overdue hold), confirm and collect
    refuse with a clear 409, and the unit stays RESERVED for the bank to reject and discard."""
    (u,) = stock(client, w["near"])
    rid = post(client, w).json()["id"]
    asyncio.run(_fetch(test_url, "UPDATE blood_units SET expires_at = $2 WHERE id = $1::uuid", u, T0))
    r = act(client, rid, "confirm", w["near_staff"])
    assert r.status_code == 409 and r.json()["type"] == "urn:lifelink:problem:unit-expired"
    for attempt in (lambda: collected(client, w, rid), lambda: act(client, rid, "issued", w["near_staff"]),
                    lambda: fulfil(client, w, rid)):
        r = attempt()
        assert r.status_code == 409 and r.json()["type"] == "urn:lifelink:problem:unit-expired", r.text
    assert unit_status(test_url, [u]) == {u: "RESERVED"}
    assert act(client, rid, "reject", w["near_staff"], reason="not_in_stock").status_code == 200
    assert unit_status(test_url, [u]) == {u: "DISCARDED"}


# ---------- 4: confirm ----------
def test_the_banks_own_staff_confirm_once_and_it_is_audited_and_published(client, w, clock, test_url, published):
    stock(client, w["near"])
    rid = post(client, w).json()["id"]
    clock["t"] = T0 + timedelta(minutes=10)
    r = act(client, rid, "confirm", w["near_staff"])
    hold = r.json()
    assert r.status_code == 200 and (hold["status"], hold["request_id"], hold["units"]) == ("CONFIRMED", rid, 1)
    assert datetime.fromisoformat(hold["expires_at"]) == T0 + timedelta(minutes=10, hours=6)
    assert "hold_confirmed" in published
    again = act(client, rid, "confirm", w["near_staff"])                      # a repeat click is a no-op
    assert again.status_code == 200 and again.json() == hold
    assert [e["event"] for e in audit_rows(test_url, rid)].count("hold.confirmed") == 1


def test_nobody_else_can_confirm_and_nothing_changes(client, w, clock, test_url):
    stock(client, w["near"])
    rid = post(client, w).json()["id"]
    assert act(client, rid, "confirm", w["far_staff"]).status_code == 404                 # another bank learns nothing
    assert act(client, rid, "confirm", w["staff"]).status_code == 403                     # the hospital
    assert act(client, rid, "confirm", as_role(client, test_url, "donor")).status_code == 403
    assert act(client, rid, "confirm", {}).status_code == 403                              # admin (client default)
    assert act(client, rid, "confirm", {"Authorization": ""}).status_code == 401
    assert [h["status"] for h in holds(client, w, rid).values()] == ["PENDING"]


def test_an_expired_or_released_hold_cannot_be_confirmed(client, w, clock):
    stock(client, w["near"], 2)
    late = post(client, w).json()["id"]
    clock["t"] = T0 + timedelta(minutes=30)                                    # exactly the deadline: expired
    r = act(client, late, "confirm", w["near_staff"])
    assert r.status_code == 409 and r.json()["type"] == "urn:lifelink:problem:hold-not-pending"
    clock["t"] = T0
    gone = post(client, w).json()["id"]
    cancel(client, w, gone)
    assert act(client, gone, "confirm", w["near_staff"]).status_code == 409


# ---------- 5: reject ----------
@pytest.mark.parametrize("reason, unit_after", [("not_in_stock", "DISCARDED"), ("cannot_supply", "AVAILABLE")])
def test_a_rejected_hold_frees_the_request_and_that_bank_is_never_asked_again(client, w, clock, test_url, published,
                                                                              reason, unit_after):
    (unit,) = stock(client, w["near"])
    body = post(client, w).json()
    rid = body["id"]
    r = act(client, rid, "reject", w["near_staff"], reason=reason)
    assert r.status_code == 200 and r.json()["status"] == "REJECTED" and "hold_rejected" in published
    view = client.get(f"/v1/emergencies/{rid}", headers=w["staff"]).json()
    assert (view["status"], view["units_allocated"], [h["status"] for h in view["holds"]]) == ("OPEN", 0, ["REJECTED"])
    assert unit_status(test_url, [unit]) == {unit: unit_after}
    stock(client, w["near"], 3)                                                # the same bank restocks...
    far = stock(client, w["far"])
    again = client.post(f"/v1/emergencies/{rid}/allocate", headers=w["staff"]).json()
    assert [a["blood_unit_id"] for a in again["allocations"]] == far           # ...but only the far bank is asked
    assert [e["details"] for e in audit_rows(test_url, rid) if e["event"] == "hold.rejected"][0].count(reason) == 1


def test_reject_needs_a_known_reason_and_a_pending_hold(client, w, clock):
    stock(client, w["near"])
    rid = post(client, w).json()["id"]
    assert act(client, rid, "reject", w["near_staff"], reason="bored").status_code == 422
    assert act(client, rid, "reject", w["near_staff"]).status_code == 422
    act(client, rid, "confirm", w["near_staff"])
    assert act(client, rid, "reject", w["near_staff"], reason="cannot_supply").status_code == 409   # confirmed: too late


# ---------- 6 + 10: expiry, exactly once ----------
def test_expiry_releases_each_hold_exactly_once_even_with_two_workers(client, w, clock, test_url, published):
    units = stock(client, w["near"], 2)
    expiring = post(client, w, n=2).json()["id"]                               # deadline T0 + 30 min
    clock["t"] = T0 + timedelta(seconds=1)
    stock(client, w["far"])
    survivor = post(client, w).json()["id"]                                    # deadline T0 + 30 min + 1 s
    published.clear()
    deadline = T0 + timedelta(minutes=30)
    assert sorted(run_expiry(test_url, deadline, runs=2)) == [0, 2]            # two concurrent workers: 2 units, once
    assert run_expiry(test_url, deadline) == [0]                               # and again at the same instant: nothing
    after = client.get(f"/v1/emergencies/{expiring}", headers=w["staff"]).json()
    assert after["status"] == "OPEN" and [h["status"] for h in after["holds"]] == ["EXPIRED"]
    assert set(unit_status(test_url, units).values()) == {"AVAILABLE"}
    assert [e["event"] for e in audit_rows(test_url, expiring)].count("hold.expired") == 1   # one per (request, bank)
    assert published == ["hold_expired"]
    assert [h["status"] for h in holds(client, w, survivor).values()] == ["PENDING"]         # +1 s: not yet


def test_a_worker_with_a_stale_list_releases_nothing_twice(client, w, clock, test_url, monkeypatch):
    """The worst interleaving, made deterministic: worker B listed the request BEFORE worker A released its holds,
    and only gets the row lock afterwards. The conditional UPDATE (released_at IS NULL) is what saves us here."""
    stock(client, w["near"], 2)
    rid = post(client, w, n=2).json()["id"]
    from uuid import UUID

    from modules.emergencies.repository import EmergencyRepository

    async def stale(self, now, limit=100):
        return [UUID(rid)]
    monkeypatch.setattr(EmergencyRepository, "requests_with_expired_holds", stale)
    deadline = T0 + timedelta(minutes=30)
    assert run_expiry(test_url, deadline) == [2]
    assert run_expiry(test_url, deadline) == [0]
    assert [e["event"] for e in audit_rows(test_url, rid)].count("hold.expired") == 1


def test_the_worker_never_releases_a_confirmed_hold_it_turns_overdue_and_keeps_counting(client, w, clock, test_url):
    """A confirmed hold past pickup may already be in a patient (hospital forgot to press "received"): auto-releasing it
    would put phantom stock back on the shelf, the double promise ADR 016 exists to prevent."""
    (unit,) = stock(client, w["near"])
    rid = post(client, w).json()["id"]
    act(client, rid, "confirm", w["near_staff"])                               # pickup deadline T0 + 6 h
    clock["t"] = T0 + timedelta(hours=7)
    assert run_expiry(test_url, clock["t"]) == [0]
    view = client.get(f"/v1/emergencies/{rid}", headers=w["staff"]).json()
    assert (view["status"], [h["status"] for h in view["holds"]]) == ("ALLOCATED", ["OVERDUE"])
    assert unit_status(test_url, [unit]) == {unit: "RESERVED"}
    (mine,) = client.get("/v1/emergencies/holds", headers=w["near_staff"]).json()
    assert (mine["request_id"], mine["status"]) == (rid, "OVERDUE")
    stock(client, w["near"], 20)
    assert post(client, w, n=20).json()["units_allocated"] == 19             # the overdue unit still counts (cap 20)
    assert fulfil(client, w, rid).json()["status"] == "FULFILLED"
    assert unit_status(test_url, [unit]) == {unit: "ISSUED"}                   # the hospital resolves it


def test_closed_requests_are_never_touched_and_a_fulfilled_hold_reads_issued(client, w, clock, test_url):
    stock(client, w["near"], 2)
    done = post(client, w).json()["id"]
    act(client, done, "confirm", w["near_staff"])
    fulfilled = fulfil(client, w, done).json()
    assert [(h["status"], h["units"]) for h in fulfilled["holds"]] == [("ISSUED", 1)]   # not "CONFIRMED", deadline past
    cancelled = post(client, w).json()["id"]
    cancel(client, w, cancelled)
    assert run_expiry(test_url, T0 + timedelta(hours=7)) == [0]
    assert [h["status"] for h in holds(client, w, done).values()] == ["ISSUED"]
    assert [h["status"] for h in holds(client, w, cancelled).values()] == ["CANCELLED"]
    assert len(client.get("/v1/blood-units", params={"status": "ISSUED"}).json()["items"]) == 1


def test_the_bank_releases_an_overdue_hold_once_and_only_its_own(client, w, clock, test_url, published):
    (unit,) = stock(client, w["near"])
    rid = post(client, w).json()["id"]
    act(client, rid, "confirm", w["near_staff"])
    r = act(client, rid, "release", w["near_staff"])                           # within the pickup window: too early
    assert r.status_code == 409 and r.json()["type"] == "urn:lifelink:problem:hold-not-overdue"
    clock["t"] = T0 + timedelta(hours=6)                                       # exactly the deadline: OVERDUE
    assert act(client, rid, "release", w["far_staff"]).status_code == 404     # another bank learns nothing
    assert act(client, rid, "release", w["staff"]).status_code == 403         # the hospital
    assert act(client, rid, "release", {}).status_code == 403                 # admin: not at the shelf
    r = act(client, rid, "release", w["near_staff"])
    assert r.status_code == 200 and (r.json()["status"], r.json()["units"]) == ("RELEASED", 1)
    assert "hold_released" in published
    assert unit_status(test_url, [unit]) == {unit: "AVAILABLE"}
    view = client.get(f"/v1/emergencies/{rid}", headers=w["staff"]).json()
    assert (view["status"], [h["status"] for h in view["holds"]]) == ("OPEN", ["RELEASED"])
    again = act(client, rid, "release", w["near_staff"])                       # a repeat click: same answer, no change
    assert again.status_code == 200 and again.json() == r.json()
    assert [e["event"] for e in audit_rows(test_url, rid)].count("hold.released") == 1
    assert unit_status(test_url, [unit]) == {unit: "AVAILABLE"}


def test_release_is_refused_for_a_pending_hold_and_a_closed_request(client, w, clock):
    stock(client, w["near"], 2)
    pending = post(client, w).json()["id"]
    clock["t"] = T0 + timedelta(hours=7)                                       # long past, but never confirmed
    assert act(client, pending, "release", w["near_staff"]).status_code == 409
    clock["t"] = T0
    done = post(client, w).json()["id"]
    act(client, done, "confirm", w["near_staff"])
    fulfil(client, w, done)
    clock["t"] = T0 + timedelta(hours=7)
    assert act(client, done, "release", w["near_staff"]).status_code == 409   # handed over: nothing to take back


def test_a_bank_that_let_its_hold_expire_is_asked_again_but_a_rejecting_bank_is_not(client, w, clock, test_url):
    """Replaces test_a_bank_that_let_its_hold_expire_is_not_asked_again (field review F5a): a silent night shift is not
    a "no". After a phone call the hospital clicks "check blood banks again" and the same bank is asked again."""
    (unit,) = stock(client, w["near"])
    rid = post(client, w).json()["id"]
    assert run_expiry(test_url, T0 + timedelta(minutes=30)) == [1]
    clock["t"] = T0 + timedelta(minutes=31)
    again = client.post(f"/v1/emergencies/{rid}/allocate", headers=w["staff"]).json()
    assert [a["blood_unit_id"] for a in again["allocations"]] == [unit]       # the nearest bank, re-asked
    assert [(h["status"], h["blood_bank_id"]) for h in again["holds"]] == [("EXPIRED", w["near"]), ("PENDING", w["near"])]
    act(client, rid, "reject", w["near_staff"], reason="cannot_supply")       # now an explicit no
    far = stock(client, w["far"])
    third = client.post(f"/v1/emergencies/{rid}/allocate", headers=w["staff"]).json()
    assert [a["blood_unit_id"] for a in third["allocations"]] == far          # only the far bank is asked now


def test_the_expiry_job_takes_the_most_overdue_requests_first(client, w, clock, test_url):
    stock(client, w["near"], 2)
    oldest = post(client, w).json()["id"]
    clock["t"] = T0 + timedelta(minutes=5)
    post(client, w)
    from modules.emergencies.repository import EmergencyRepository

    async def first():
        engine = make_engine(app_url(test_url), 1, 0)
        try:
            async with make_sessionmaker(engine)() as s:
                return await EmergencyRepository(s).requests_with_expired_holds(T0 + timedelta(hours=1), limit=1)
        finally:
            await engine.dispose()
    assert [str(r) for r in asyncio.run(first())] == [oldest]


def test_a_bank_action_returns_only_that_banks_part(client, w, clock):
    """No other bank's hold, no unit ids: the same shape as GET /holds."""
    stock(client, w["near"])
    stock(client, w["far"])
    rid = post(client, w, n=2).json()["id"]
    for r in (act(client, rid, "confirm", w["near_staff"]), act(client, rid, "reject", w["far_staff"], reason="cannot_supply")):
        assert r.status_code == 200
        assert set(r.json()) == {"request_id", "code", "hospital_ref", "hospital_name", "hospital_phone",
                                 "hospital_is_demo", "blood_group", "component", "units", "urgency", "status",
                                 "expires_at"}
        assert r.json()["units"] == 1


# ---------- 7: real concurrent races ----------
def test_confirm_racing_expiry_never_ends_in_both(client, w, clock, test_url):
    """The bank clicks confirm one second before the deadline while the worker runs AT the deadline."""
    stock(client, w["near"], 8)
    rids = [post(client, w).json()["id"] for _ in range(8)]
    clock["t"] = T0 + timedelta(minutes=30) - timedelta(seconds=1)
    with ThreadPoolExecutor(9) as pool:
        expiry = pool.submit(run_expiry, test_url, T0 + timedelta(minutes=30), 1)
        confirms = list(pool.map(lambda rid: act(client, rid, "confirm", w["near_staff"]).status_code, rids))
        expiry.result()
    for rid, code in zip(rids, confirms, strict=True):
        rows = asyncio.run(_fetch(test_url, """SELECT a.released_at, a.release_reason, a.confirmed_at, u.status
            FROM allocations a JOIN blood_units u ON u.id = a.blood_unit_id WHERE a.request_id = $1::uuid""", rid))
        (row,) = rows
        if code == 200:     # confirm won: confirmed, live, unit still held
            assert row["released_at"] is None and row["confirmed_at"] is not None and row["status"] == "RESERVED"
        else:               # expiry won: released as expired, confirm refused, unit back on the shelf
            assert code == 409 and row["release_reason"] == "expired" and row["status"] == "AVAILABLE"
    live_but_available = asyncio.run(_fetch(test_url, """SELECT count(*) FROM allocations a JOIN blood_units u
        ON u.id = a.blood_unit_id WHERE a.released_at IS NULL AND u.status = 'AVAILABLE'"""))[0][0]
    assert live_but_available == 0


def test_two_banks_confirm_their_holds_on_one_request_at_once(client, w, clock):
    stock(client, w["near"])
    stock(client, w["far"])
    body = post(client, w, n=2).json()
    assert {h["blood_bank_id"] for h in body["holds"]} == {w["near"], w["far"]}
    with ThreadPoolExecutor(2) as pool:
        codes = list(pool.map(lambda s: act(client, body["id"], "confirm", s).status_code, [w["near_staff"], w["far_staff"]]))
    assert codes == [200, 200]
    assert {h["status"] for h in holds(client, w, body["id"]).values()} == {"CONFIRMED"}
    assert fulfil(client, w, body["id"], "near", "far").json()["status"] == "FULFILLED"


def test_cancel_racing_confirm_puts_every_unit_back_exactly_once(client, w, clock, test_url):
    units = stock(client, w["near"], 6)
    rids = [post(client, w).json()["id"] for _ in range(6)]
    with ThreadPoolExecutor(12) as pool:
        cancels = [pool.submit(cancel, client, w, rid) for rid in rids]
        confirms = [pool.submit(act, client, rid, "confirm", w["near_staff"]) for rid in rids]
        assert {f.result().status_code for f in cancels} == {200}
        assert {f.result().status_code for f in confirms} <= {200, 409}
    assert set(unit_status(test_url, units).values()) == {"AVAILABLE"}
    rows = asyncio.run(_fetch(test_url, "SELECT release_reason, count(*) FROM allocations GROUP BY 1"))
    assert [tuple(r) for r in rows] == [("cancelled", 6)]                     # each hold released once, as cancelled


# ---------- 8: fulfil no longer waits for every bank (ADR 021 rev 2: it says what was collected instead) ----------
def test_fulfil_collects_the_named_banks_and_releases_the_rest(client, w, clock, test_url):
    """Replaces test_fulfil_waits_for_every_bank: the old 409 holds-unconfirmed is gone. The far bank never confirmed,
    the nurse collected only from the near one: the near unit is ISSUED, the far unit goes back on the shelf."""
    near, far = stock(client, w["near"]) + stock(client, w["far"])
    rid = post(client, w, n=2).json()["id"]
    act(client, rid, "confirm", w["near_staff"])
    done = fulfil(client, w, rid)
    assert done.status_code == 200
    assert (done.json()["status"], done.json()["units_collected"]) == ("FULFILLED", 1)
    assert unit_status(test_url, [near, far]) == {near: "ISSUED", far: "AVAILABLE"}
    assert {(h["blood_bank_id"], h["status"]) for h in done.json()["holds"]} == {(w["near"], "ISSUED"), (w["far"], "CANCELLED")}


# ---------- 9: the anti-hoarding cap ----------
def test_one_hospital_cannot_hold_more_than_20_units_even_in_parallel(client, w, clock, test_url):
    stock(client, w["near"], 60)
    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(lambda _: post(client, w, n=5), range(10)))
    assert {r.status_code for r in results} == {201}                           # over the cap is still a request
    held = sum(r.json()["units_allocated"] for r in results)
    assert held == 20 and sum(r.json()["cap_reached"] for r in results) >= 6
    capped = [r.json()["id"] for r in results if r.json()["cap_reached"]]
    assert '"capped": true' in [e["details"] for e in audit_rows(test_url, capped[0]) if e["event"] == "emergency.created"][0]
    # a second hospital in the same city is not affected
    other = client.post("/v1/organizations", json=make_organization(kind="hospital", **AIIMS)).json()["id"]
    theirs = post(client, w, n=5, headers=as_role(client, test_url, "hospital", other)).json()
    assert theirs["units_allocated"] == 5 and theirs["cap_reached"] is False


# ---------- 11-13: freshness, phone, bank settings, own blood centre ----------
def test_a_facility_phone_reaches_the_public_search(client, w, clock):
    """Stock freshness moved to test_stock_take.py (ADR 023: only a stock-take sets it)."""
    stock(client, w["near"])
    find = {"recipient": "O+", **AIIMS}
    (bank,) = client.get("/v1/public/nearest-banks", params=find, headers={"Authorization": ""}).json()
    assert bank["stock_confirmed_at"] is None and bank["phone"] is None
    client.patch(f"/v1/organizations/{w['near']}", json={"phone": "0755 2345678"})
    (bank,) = client.get("/v1/public/nearest-banks", params=find, headers={"Authorization": ""}).json()
    assert bank["phone"] == "+917552345678"


@pytest.mark.parametrize("raw", ["0755 2345678", "+91 755 234 5678", "917552345678", "98123-45678", "(0755) 2345678"])
def test_facility_phones_normalise_landlines_and_mobiles(client, raw):
    r = client.post("/v1/organizations", json=make_organization(kind="blood_bank", phone=raw))
    assert r.status_code == 201 and r.json()["phone"] in ("+917552345678", "+919812345678")


@pytest.mark.parametrize("raw", ["+91 0755234567", "+910123456789", "07552345678x", "755234567", "75523456789"])
def test_bad_facility_phones_are_rejected(client, raw):
    assert client.post("/v1/organizations", json=make_organization(kind="blood_bank", phone=raw)).status_code == 422


def test_only_an_admin_sets_a_phone_or_the_blood_centre_flag(client, w):
    for body in ({"phone": "0755 2345678"}, {"has_blood_centre": True}):
        assert client.patch(f"/v1/organizations/{w['hospital']}", json=body, headers=w["staff"]).status_code == 403
    r = client.patch(f"/v1/organizations/{w['hospital']}", json={"has_blood_centre": True})
    assert r.status_code == 200 and r.json()["has_blood_centre"] is True


def test_a_blood_centre_flag_is_only_for_hospitals(client, w, test_url):
    r = client.patch(f"/v1/organizations/{w['near']}", json={"has_blood_centre": True})
    assert r.status_code == 422
    assert client.post("/v1/organizations", json=make_organization(kind="blood_bank", has_blood_centre=True)).status_code == 422
    import asyncpg
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        asyncio.run(_fetch(test_url, "UPDATE organizations SET has_blood_centre = true WHERE id = $1::uuid", w["near"]))


def test_bank_settings_are_for_that_bank_or_an_admin(client, w, test_url):
    url = f"/v1/organizations/{w['near']}/bank-settings"
    on = {"auto_confirm_holds": True}
    assert client.patch(url, json=on, headers=w["far_staff"]).status_code == 403
    assert client.patch(url, json=on, headers=w["staff"]).status_code == 403
    assert client.patch(url, json=on, headers={"Authorization": ""}).status_code == 401
    assert client.patch(url, json={"auto_confirm_holds": "maybe"}).status_code == 422
    assert client.patch(url, json=on).json()["auto_confirm_holds"] is True                    # admin
    assert client.patch(url, json={"auto_confirm_holds": False}, headers=w["near_staff"]).status_code == 200
    events = [e["event"] for e in audit_rows(test_url, w["near"])]
    assert events.count("organization.bank_settings_changed") == 2
    assert client.patch(f"/v1/organizations/{w['hospital']}/bank-settings", json=on).status_code == 422


# ---------- 14: what the hospital and the bank see ----------
def test_the_hospital_sees_every_bank_and_why_and_the_bank_sees_no_patient_data(client, w, clock, test_url):
    client.patch(f"/v1/organizations/{w['near']}", json={"phone": "0755 2345678"})
    stock(client, w["near"])
    stock(client, w["far"])
    rid = post(client, w, n=2, urgency="CRITICAL").json()["id"]
    act(client, rid, "reject", w["far_staff"], reason="cannot_supply")
    view = client.get(f"/v1/emergencies/{rid}", headers=w["staff"]).json()["holds"]
    near = next(h for h in view if h["blood_bank_id"] == w["near"])
    assert (near["status"], near["bank_phone"], near["units"]) == ("PENDING", "+917552345678", 1) and near["bank_name"]
    assert [h["status"] for h in view if h["blood_bank_id"] == w["far"]] == ["REJECTED"]
    mine = client.get("/v1/emergencies/holds", headers=w["near_staff"])
    (item,) = mine.json()
    assert set(item) == {"request_id", "code", "hospital_ref", "hospital_name", "hospital_phone", "hospital_is_demo",
                         "blood_group", "component", "units", "urgency", "status", "expires_at"}   # still no patient data
    assert (item["request_id"], item["units"], item["status"], item["urgency"]) == (rid, 1, "PENDING", "CRITICAL")
    assert client.get("/v1/emergencies/holds", headers=w["far_staff"]).json() == []        # released: not listed
    assert client.get("/v1/emergencies/holds", headers=w["staff"]).status_code == 403


def test_even_a_worker_told_to_look_at_it_never_expires_a_confirmed_hold(client, w, clock, test_url, monkeypatch):
    """Two guards: the list skips confirmed holds AND the UPDATE does. This forces the list, so only the UPDATE's
    `confirmed_at IS NULL` stands between an overdue hold and phantom stock."""
    from uuid import UUID

    from modules.emergencies.repository import EmergencyRepository
    (unit,) = stock(client, w["near"])
    rid = post(client, w).json()["id"]
    act(client, rid, "confirm", w["near_staff"])

    async def listed(self, now, limit=100):
        return [UUID(rid)]
    monkeypatch.setattr(EmergencyRepository, "requests_with_expired_holds", listed)
    assert run_expiry(test_url, T0 + timedelta(hours=7)) == [0]
    assert unit_status(test_url, [unit]) == {unit: "RESERVED"}
