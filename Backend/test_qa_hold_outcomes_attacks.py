# ruff: noqa: F811  (the imported `w` and `clock` fixtures are requested by name as parameters)
"""QA attacks on hold outcomes (ADR 021 revision 2): the races the engineer's suite does not cover, the second
half of the CHECK, cross-tenant ids on the new endpoints, and closed-request edges."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import asyncpg
import pytest

from conftest import _fetch, as_role, audit_rows, run_sql
from factories import make_organization
from test_holds import (  # noqa: F401 (fixtures)
    T0,
    act,
    cancel,
    clock,
    collected,
    fulfil,
    post,
    stock,
    unit_status,
    w,
)
from test_qa_holds_attacks import overdue

PROBLEM = "urn:lifelink:problem:"


def rows(test_url, rid):
    return asyncio.run(_fetch(test_url, """SELECT a.blood_bank_id::text AS bank, a.released_at, a.release_reason,
        a.confirmed_at, a.collected_at, u.status FROM allocations a JOIN blood_units u ON u.id = a.blood_unit_id
        WHERE a.request_id = $1::uuid""", rid))


def no_contradiction(test_url):
    """Every allocation/unit pair tells one story: collected <-> ISSUED, released -> not RESERVED/ISSUED, live -> held."""
    bad = asyncio.run(_fetch(test_url, """SELECT count(*) FROM allocations a JOIN blood_units u ON u.id = a.blood_unit_id
        WHERE (a.collected_at IS NOT NULL AND u.status <> 'ISSUED')
           OR (a.collected_at IS NULL AND u.status = 'ISSUED')
           OR (a.released_at IS NOT NULL AND u.status IN ('RESERVED', 'ISSUED'))
           OR (a.released_at IS NULL AND a.collected_at IS NULL AND u.status <> 'RESERVED')"""))
    assert bad[0][0] == 0


def collected_events(test_url, rid):
    return [json.loads(e["details"]) for e in audit_rows(test_url, rid) if e["event"] == "hold.collected"]


# ---------- the CHECK, second half: collected must be confirmed ----------
def test_the_database_refuses_a_collected_hold_that_was_never_confirmed(client, w, clock, test_url):
    stock(client, w["near"])
    rid = post(client, w).json()["id"]
    with pytest.raises(asyncpg.exceptions.CheckViolationError, match="allocations_collected_is_live"):
        asyncio.run(run_sql(test_url, f"UPDATE allocations SET collected_at = now() WHERE request_id = '{rid}'"))


def test_the_database_refuses_collecting_an_already_released_hold(client, w, clock, test_url):
    stock(client, w["near"])
    rid = post(client, w).json()["id"]
    cancel(client, w, rid)
    with pytest.raises(asyncpg.exceptions.CheckViolationError, match="allocations_collected_is_live"):
        asyncio.run(run_sql(test_url, f"UPDATE allocations SET collected_at = now(), confirmed_at = now() "
                                      f"WHERE request_id = '{rid}'"))


# ---------- races ----------
def test_collected_racing_the_banks_release_of_an_overdue_hold_ends_in_exactly_one(client, w, clock, test_url):
    cases = []
    for _ in range(4):
        clock["t"] = T0
        cases.append(overdue(client, w, clock)[0])
    with ThreadPoolExecutor(8) as pool:
        futures = [(pool.submit(collected, client, w, rid), pool.submit(act, client, rid, "release", w["near_staff"]), rid)
                   for rid in cases]
        for c, r, rid in futures:
            pair = (c.result().status_code, r.result().status_code)
            assert pair in ((200, 409), (409, 200)), pair
            (row,) = rows(test_url, rid)
            assert row["status"] == ("ISSUED" if pair[0] == 200 else "AVAILABLE")
    no_contradiction(test_url)


def test_fulfil_racing_cancel_ends_in_exactly_one(client, w, clock, test_url):
    stock(client, w["near"], 6)
    rids = [post(client, w).json()["id"] for _ in range(6)]
    with ThreadPoolExecutor(12) as pool:
        futures = [(pool.submit(fulfil, client, w, rid), pool.submit(cancel, client, w, rid), rid) for rid in rids]
        for f, c, rid in futures:
            pair = (f.result().status_code, c.result().status_code)
            assert pair in ((200, 409), (409, 200)), pair
            (row,) = rows(test_url, rid)
            assert row["status"] == ("ISSUED" if pair[0] == 200 else "AVAILABLE")
    no_contradiction(test_url)


def test_the_banks_handover_racing_another_bank_clerks_reject_ends_in_exactly_one(client, w, clock, test_url):
    """Two clerks of the SAME bank: one presses Handed over, the other Reject (not in stock) on the same PENDING hold."""
    stock(client, w["near"], 6)
    rids = [post(client, w).json()["id"] for _ in range(6)]
    clerk2 = as_role(client, test_url, "blood_bank", w["near"])
    with ThreadPoolExecutor(12) as pool:
        futures = [(pool.submit(act, client, rid, "issued", w["near_staff"]),
                    pool.submit(act, client, rid, "reject", clerk2, reason="not_in_stock"), rid) for rid in rids]
        for i, r, rid in futures:
            pair = (i.result().status_code, r.result().status_code)
            assert pair in ((200, 409), (409, 200)), pair
            (row,) = rows(test_url, rid)
            assert row["status"] == ("ISSUED" if pair[0] == 200 else "DISCARDED")
    no_contradiction(test_url)


def test_hospital_collected_racing_bank_issued_audits_once(client, w, clock, test_url):
    stock(client, w["near"], 4)
    rids = [post(client, w).json()["id"] for _ in range(4)]
    with ThreadPoolExecutor(8) as pool:
        futures = [(pool.submit(collected, client, w, rid), pool.submit(act, client, rid, "issued", w["near_staff"]), rid)
                   for rid in rids]
        for c, i, rid in futures:
            assert (c.result().status_code, i.result().status_code) == (200, 200)
            assert len(collected_events(test_url, rid)) == 1
    no_contradiction(test_url)


# ---------- authorization on the new paths ----------
def test_another_hospital_cannot_close_or_collect_with_a_known_bank_id(client, w, clock, test_url):
    (unit,) = stock(client, w["near"])
    rid = post(client, w).json()["id"]
    other = client.post("/v1/organizations", json=make_organization(kind="hospital")).json()["id"]
    them = as_role(client, test_url, "hospital", other)
    assert fulfil(client, w, rid, headers=them).status_code == 404
    assert client.post(f"/v1/emergencies/{rid}/cancel", json={"collected_from": []}, headers=them).status_code == 404
    assert collected(client, w, rid, headers=them).status_code == 404
    donor = as_role(client, test_url, "donor")
    assert fulfil(client, w, rid, headers=donor).status_code == 403
    assert fulfil(client, w, rid, headers=w["near_staff"]).status_code == 403     # a bank cannot close a request
    assert unit_status(test_url, [unit]) == {unit: "RESERVED"} and collected_events(test_url, rid) == []


def test_a_bank_with_only_a_released_hold_cannot_hand_over_a_request_it_lost(client, w, clock, test_url):
    """near rejected, far now holds: near pressing Handed over must not touch far's units."""
    stock(client, w["near"])
    rid = post(client, w).json()["id"]
    act(client, rid, "reject", w["near_staff"], reason="cannot_supply")
    (far,) = stock(client, w["far"])
    client.post(f"/v1/emergencies/{rid}/allocate", headers=w["staff"])
    r = act(client, rid, "issued", w["near_staff"])
    assert r.status_code == 409 and r.json()["type"] == PROBLEM + "hold-not-live"
    assert unit_status(test_url, [far]) == {far: "RESERVED"}


def test_bank_issued_on_a_fulfilled_request_is_an_idempotent_200_and_on_a_cancelled_one_409(client, w, clock, test_url):
    stock(client, w["near"], 2)
    done = post(client, w).json()["id"]
    fulfil(client, w, done)
    r = act(client, done, "issued", w["near_staff"])
    assert r.status_code == 200 and r.json()["status"] == "ISSUED"
    gone = post(client, w).json()["id"]
    cancel(client, w, gone)
    assert act(client, gone, "issued", w["near_staff"]).status_code == 409
    no_contradiction(test_url)


def test_fulfil_naming_a_bank_that_rejected_is_unknown_bank(client, w, clock, test_url):
    stock(client, w["near"])
    (far,) = stock(client, w["far"])
    rid = post(client, w, n=2).json()["id"]
    act(client, rid, "reject", w["near_staff"], reason="cannot_supply")
    r = fulfil(client, w, rid, "near", "far")
    assert r.status_code == 422 and r.json()["type"] == PROBLEM + "unknown-bank"
    assert unit_status(test_url, [far]) == {far: "RESERVED"}


def test_a_fulfilled_request_is_never_reopened_by_a_late_expiry_or_a_second_close(client, w, clock, test_url):
    stock(client, w["near"])
    stock(client, w["far"])
    rid = post(client, w, n=2).json()["id"]
    assert fulfil(client, w, rid).status_code == 200                                  # near collected, far released
    assert fulfil(client, w, rid).json()["type"] == PROBLEM + "invalid-transition"
    assert cancel(client, w, rid).json()["type"] == PROBLEM + "invalid-transition"
    from test_holds import run_expiry
    assert run_expiry(test_url, T0 + timedelta(hours=10)) == [0]
    no_contradiction(test_url)


# ---------- the 503 must be readable by the browser (restart drill, 2026-10-20) ----------
def test_a_database_outage_503_carries_cors_headers_so_the_page_can_show_it(app):
    """Found in the restart drill: the browser got net::ERR_FAILED, not the 503, because the catch-all Exception handler
    runs in Starlette's ServerErrorMiddleware, OUTSIDE CORSMiddleware. Without Access-Control-Allow-Origin the page
    cannot read the problem type and shows "Could not reach Rakt Doot" instead of the database-unavailable message."""
    from fastapi.testclient import TestClient

    from db import make_engine, make_sessionmaker
    with TestClient(app, raise_server_exceptions=False) as c:
        app.state.sessionmaker = make_sessionmaker(make_engine("postgresql+asyncpg://x:y@127.0.0.1:1/down", 1, 0))
        r = c.get("/v1/public/nearest-banks", params={"recipient": "O+", "latitude": 23.2, "longitude": 77.4},
                  headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 503
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"
