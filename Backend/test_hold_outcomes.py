# ruff: noqa: F811  (the imported `w` and `clock` fixtures are requested by name as parameters)
"""Hold outcomes (ADR 021 revision 2): blood that physically left a bank never comes back as AVAILABLE,
whichever path the hospital or the bank takes. Collected = live + confirmed + units ISSUED; nothing releases it."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from conftest import _fetch, as_role, audit_rows, run_sql
from factories import make_blood_unit, make_organization
from test_holds import (  # noqa: F401 (fixtures)
    T0,
    act,
    cancel,
    clock,
    collected,
    fulfil,
    holds,
    post,
    published,
    run_expiry,
    stock,
    unit_status,
    w,
)

PROBLEM = "urn:lifelink:problem:"
NOTHING_MORE = {"collected_from": []}


def events(test_url, rid, name):
    return [json.loads(e["details"]) for e in audit_rows(test_url, rid) if e["event"] == name]


def hold_rows(test_url, rid):
    return asyncio.run(_fetch(test_url, """SELECT a.released_at, a.release_reason, a.confirmed_at, a.collected_at, u.status
        FROM allocations a JOIN blood_units u ON u.id = a.blood_unit_id WHERE a.request_id = $1::uuid""", rid))


# ---------- AC 2-3: the hospital marks a hold collected, whatever its state ----------
def test_a_pending_hold_marked_collected_is_issued_and_the_expiry_pass_ignores_it(client, w, clock, test_url):
    (unit,) = stock(client, w["near"])
    rid = post(client, w).json()["id"]
    clock["t"] = T0 + timedelta(minutes=5)
    r = collected(client, w, rid)
    assert r.status_code == 200
    assert (r.json()["units_collected"], [h["status"] for h in r.json()["holds"]]) == (1, ["ISSUED"])
    assert unit_status(test_url, [unit]) == {unit: "ISSUED"}
    assert run_expiry(test_url, T0 + timedelta(hours=1)) == [0]                   # past the confirm deadline: nothing
    assert unit_status(test_url, [unit]) == {unit: "ISSUED"}
    (row,) = hold_rows(test_url, rid)
    assert row["released_at"] is None and row["confirmed_at"] == row["collected_at"]   # collecting confirms too
    assert client.get("/v1/emergencies/holds", headers=w["near_staff"]).json() == []    # not the bank's to act on


@pytest.mark.parametrize("state", ["CONFIRMED", "OVERDUE"])
def test_a_confirmed_or_overdue_hold_marked_collected_cannot_be_released_by_the_bank(client, w, clock, test_url, state):
    (unit,) = stock(client, w["near"])
    rid = post(client, w).json()["id"]
    act(client, rid, "confirm", w["near_staff"])
    if state == "OVERDUE":
        clock["t"] = T0 + timedelta(hours=7)
    assert [h["status"] for h in holds(client, w, rid).values()] == [state]
    assert collected(client, w, rid).json()["holds"][0]["status"] == "ISSUED"
    clock["t"] = T0 + timedelta(hours=8)                                          # well past pickup
    r = act(client, rid, "release", w["near_staff"])
    assert r.status_code == 409 and r.json()["type"] == PROBLEM + "hold-not-overdue"
    assert unit_status(test_url, [unit]) == {unit: "ISSUED"}
    assert "hold.released" not in [e["event"] for e in audit_rows(test_url, rid)]


# ---------- AC 4: the bank says "handed over" ----------
def test_the_bank_marks_its_own_pending_hold_issued_and_nobody_else_can(client, w, clock, test_url, published):
    (unit,) = stock(client, w["near"])
    rid = post(client, w).json()["id"]
    assert act(client, rid, "issued", w["far_staff"]).status_code == 404          # another bank learns nothing
    assert act(client, rid, "issued", w["staff"]).status_code == 403              # the hospital uses /collected
    assert act(client, rid, "issued", {}).status_code == 403                      # admin: not at the counter
    r = act(client, rid, "issued", w["near_staff"])
    assert r.status_code == 200 and (r.json()["status"], r.json()["units"]) == ("ISSUED", 1)
    assert unit_status(test_url, [unit]) == {unit: "ISSUED"} and "hold_collected" in published
    assert run_expiry(test_url, T0 + timedelta(hours=1)) == [0]
    again = act(client, rid, "issued", w["near_staff"])                           # repeat: same answer, no new audit
    assert again.status_code == 200 and again.json() == r.json()
    assert events(test_url, rid, "hold.collected") == [{"blood_bank_id": w["near"], "units": 1, "by": "blood_bank"}]


def test_the_bank_cannot_mark_a_released_hold_issued(client, w, clock, test_url):
    (unit,) = stock(client, w["near"])
    rid = post(client, w).json()["id"]
    assert run_expiry(test_url, T0 + timedelta(minutes=30)) == [1]
    r = act(client, rid, "issued", w["near_staff"])
    assert r.status_code == 409 and r.json()["type"] == PROBLEM + "hold-not-live"
    assert unit_status(test_url, [unit]) == {unit: "AVAILABLE"}


# ---------- AC 5: who may say "collected" ----------
def test_only_the_requesting_hospital_marks_collected_once(client, w, clock, test_url, published):
    (unit,) = stock(client, w["near"])
    rid = post(client, w).json()["id"]
    other = client.post("/v1/organizations", json=make_organization(kind="hospital")).json()["id"]
    assert collected(client, w, rid, headers=as_role(client, test_url, "hospital", other)).status_code == 404
    assert collected(client, w, rid, headers=as_role(client, test_url, "donor")).status_code == 403
    assert collected(client, w, rid, headers={}).status_code == 403                # admin: did not receive the blood
    assert collected(client, w, rid, headers=w["near_staff"]).status_code == 403   # the bank uses /holds/issued
    assert collected(client, w, rid, headers={"Authorization": ""}).status_code == 401
    assert collected(client, w, rid, bank="far").status_code == 404               # a bank with no hold here
    assert unit_status(test_url, [unit]) == {unit: "RESERVED"}
    first = collected(client, w, rid)
    again = collected(client, w, rid)
    assert first.status_code == again.status_code == 200 and again.json() == first.json()
    assert events(test_url, rid, "hold.collected") == [{"blood_bank_id": w["near"], "units": 1, "by": "hospital"}]
    assert published.count("hold_collected") == 1


def test_collected_on_a_closed_request_or_an_expired_hold_is_refused(client, w, clock, test_url):
    stock(client, w["near"], 2)
    gone = post(client, w).json()["id"]
    cancel(client, w, gone)
    r = collected(client, w, gone)
    assert r.status_code == 409 and r.json()["type"] == PROBLEM + "invalid-transition"
    late = post(client, w).json()["id"]
    assert run_expiry(test_url, T0 + timedelta(minutes=30)) == [1]
    r = collected(client, w, late)
    assert r.status_code == 409 and r.json()["type"] == PROBLEM + "hold-not-live"   # call the bank: units re-shelved


# ---------- AC 6-8: "blood received" answers which banks ----------
def test_a_partly_fulfilled_open_request_keeps_what_was_collected_and_releases_the_rest(client, w, clock, test_url):
    """4 needed, 2 held at the near bank (collected), 1 at the far bank (PENDING): OPEN -> FULFILLED."""
    near = stock(client, w["near"], 2)
    (far,) = stock(client, w["far"])
    body = post(client, w, n=4).json()
    assert (body["status"], body["units_allocated"]) == ("OPEN", 3)
    collected(client, w, body["id"])
    done = client.post(f"/v1/emergencies/{body['id']}/fulfil", json=NOTHING_MORE, headers=w["staff"])
    assert done.status_code == 200
    assert (done.json()["status"], done.json()["units_collected"]) == ("FULFILLED", 2)
    assert unit_status(test_url, [*near, far]) == {**dict.fromkeys(near, "ISSUED"), far: "AVAILABLE"}
    assert events(test_url, body["id"], "emergency.fulfilled") == [
        {"units_needed": 4, "units_collected": 2, "units_released": 1}]


def test_fulfil_naming_a_bank_without_a_live_hold_is_422_and_changes_nothing(client, w, clock, test_url):
    (unit,) = stock(client, w["near"])
    rid = post(client, w).json()["id"]
    r = fulfil(client, w, rid, "near", "far")
    assert r.status_code == 422 and r.json()["type"] == PROBLEM + "unknown-bank"
    assert unit_status(test_url, [unit]) == {unit: "RESERVED"}
    assert [h["status"] for h in holds(client, w, rid).values()] == ["PENDING"]
    assert "hold.collected" not in [e["event"] for e in audit_rows(test_url, rid)]


def test_fulfil_with_nothing_collected_is_409_and_changes_nothing(client, w, clock, test_url):
    (unit,) = stock(client, w["near"])
    rid = post(client, w).json()["id"]
    r = client.post(f"/v1/emergencies/{rid}/fulfil", json=NOTHING_MORE, headers=w["staff"])
    assert r.status_code == 409 and r.json()["type"] == PROBLEM + "nothing-collected"
    assert unit_status(test_url, [unit]) == {unit: "RESERVED"}
    view = client.get(f"/v1/emergencies/{rid}", headers=w["staff"]).json()
    assert (view["status"], [h["status"] for h in view["holds"]]) == ("ALLOCATED", ["PENDING"])


# ---------- AC 9-10: cancel refuses once blood was collected; both need the answer ----------
def test_cancel_is_refused_once_anything_was_collected(client, w, clock, test_url):
    (unit,) = stock(client, w["near"])
    rid = post(client, w).json()["id"]
    r = cancel(client, w, rid, "near")
    assert r.status_code == 409 and r.json()["type"] == PROBLEM + "units-collected"
    assert unit_status(test_url, [unit]) == {unit: "RESERVED"}                    # the refused answer changed nothing
    collected(client, w, rid)
    r = cancel(client, w, rid)
    assert r.status_code == 409 and r.json()["type"] == PROBLEM + "units-collected"
    assert unit_status(test_url, [unit]) == {unit: "ISSUED"}                      # F1: never back on the shelf
    done = client.post(f"/v1/emergencies/{rid}/fulfil", json=NOTHING_MORE, headers=w["staff"]).json()
    assert (done["status"], done["units_collected"]) == ("FULFILLED", 1)          # the honest way to close it


@pytest.mark.parametrize("what", ["fulfil", "cancel"])
@pytest.mark.parametrize("body", [None, {}, {"collected_from": ["not-a-uuid"]}, {"collected_from": [], "extra": 1},
                                  "dup", "too_many"])
def test_closing_without_a_valid_answer_is_422(client, w, clock, what, body):
    stock(client, w["near"])
    rid = post(client, w).json()["id"]
    if body == "dup":
        body = {"collected_from": [w["near"], w["near"]]}
    elif body == "too_many":
        body = {"collected_from": [w["near"]] + [f"00000000-0000-0000-0000-{i:012d}" for i in range(20)]}
    r = client.post(f"/v1/emergencies/{rid}/{what}", json=body, headers=w["staff"])
    assert r.status_code == 422 and r.json()["type"] == PROBLEM + "validation-error"
    assert [h["status"] for h in holds(client, w, rid).values()] == ["PENDING"]


# ---------- AC 12: collected units are not hoarding ----------
def test_collected_units_do_not_count_against_the_cap(client, w, clock):
    stock(client, w["near"], 25)
    first = post(client, w, n=2).json()["id"]
    collected(client, w, first)                                                    # 2 collected
    assert post(client, w, n=18).json()["units_allocated"] == 18                  # 20 live, 18 of them held
    more = post(client, w, n=3).json()
    assert (more["units_allocated"], more["cap_reached"]) == (2, True)            # room for exactly 2 more


# ---------- AC 13: the race the column exists for ----------
def test_collected_racing_expiry_ends_in_exactly_one(client, w, clock, test_url):
    """The nurse clicks "collected" one second before the deadline while the worker runs AT the deadline."""
    stock(client, w["near"], 8)
    rids = [post(client, w).json()["id"] for _ in range(8)]
    clock["t"] = T0 + timedelta(minutes=30) - timedelta(seconds=1)
    with ThreadPoolExecutor(9) as pool:
        expiry = pool.submit(run_expiry, test_url, T0 + timedelta(minutes=30), 1)
        codes = list(pool.map(lambda rid: collected(client, w, rid).status_code, rids))
        expiry.result()
    for rid, code in zip(rids, codes, strict=True):
        (row,) = hold_rows(test_url, rid)
        if code == 200:     # collected won: live, collected, unit ISSUED
            assert (row["released_at"], row["status"]) == (None, "ISSUED") and row["collected_at"] is not None
        else:               # expiry won: released, never collected, unit back on the shelf
            assert (code, row["release_reason"], row["collected_at"], row["status"]) == (409, "expired", None, "AVAILABLE")
    bad = asyncio.run(_fetch(test_url, """SELECT count(*) FROM allocations a JOIN blood_units u ON u.id = a.blood_unit_id
        WHERE (a.collected_at IS NOT NULL AND u.status <> 'ISSUED') OR (a.released_at IS NOT NULL AND u.status = 'ISSUED')
           OR (a.released_at IS NULL AND u.status = 'AVAILABLE')"""))
    assert bad[0][0] == 0


def test_the_database_refuses_a_collected_hold_that_is_released(client, w, clock, test_url):
    """Break-it for the CHECK allocations_collected_is_live: even a buggy UPDATE cannot release collected blood."""
    stock(client, w["near"])
    rid = post(client, w).json()["id"]
    collected(client, w, rid)
    with pytest.raises(asyncpg.exceptions.CheckViolationError, match="allocations_collected_is_live"):
        asyncio.run(run_sql(test_url, f"UPDATE allocations SET released_at = now(), release_reason = 'cancelled' "
                                      f"WHERE request_id = '{rid}'"))


# ---------- AC 14: stewardship inside one bank ----------
def test_inside_a_bank_a_b_positive_patient_gets_b_pos_o_pos_b_neg_then_o_neg(client, w, clock):
    units = {g: stock(client, w["near"], 1, group=g)[0] for g in ("O-", "B-", "O+", "B+")}   # same expiry
    order = [post(client, w, blood_group="B+").json()["allocations"][0]["blood_unit_id"] for _ in range(4)]
    assert order == [units["B+"], units["O+"], units["B-"], units["O-"]]


def test_o_negative_goes_last_even_when_it_expires_first(client, w, clock):
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    o_neg = client.post("/v1/blood-units", json=make_blood_unit(w["near"], blood_group="O-", expires_at=soon)).json()["id"]
    (o_pos,) = stock(client, w["near"], 1, group="O+")
    first = post(client, w, blood_group="B+").json()["allocations"][0]["blood_unit_id"]
    assert (first, o_neg != first) == (o_pos, True)


def test_stewardship_never_beats_distance(client, w, clock):
    """Bank position stays first: an O- at the near bank beats a B+ at the far bank (cross-bank preference is 2e)."""
    (near_o_neg,) = stock(client, w["near"], 1, group="O-")
    stock(client, w["far"], 1, group="B+")
    assert post(client, w, blood_group="B+").json()["allocations"][0]["blood_unit_id"] == near_o_neg


# ---------- 2b tail T1: an admin cannot attest receipt through fulfil ----------
def test_an_admin_cannot_fulfil_naming_banks_but_may_close_what_the_ward_already_collected(client, w, clock, test_url):
    (unit,) = stock(client, w["near"])
    rid = post(client, w).json()["id"]
    before = audit_rows(test_url, rid)
    r = fulfil(client, w, rid, headers={})                                       # admin + collected_from: [near]
    assert r.status_code == 403 and r.json()["type"] == PROBLEM + "forbidden"    # the same problem require() returns
    assert unit_status(test_url, [unit]) == {unit: "RESERVED"} and audit_rows(test_url, rid) == before
    assert [h["status"] for h in holds(client, w, rid).values()] == ["PENDING"]
    collected(client, w, rid)                                                    # the ward says it received the units
    done = client.post(f"/v1/emergencies/{rid}/fulfil", json=NOTHING_MORE, headers={})
    assert (done.status_code, done.json()["status"]) == (200, "FULFILLED")


def test_an_admin_may_still_cancel(client, w, clock):
    stock(client, w["near"])
    rid = post(client, w).json()["id"]
    r = client.post(f"/v1/emergencies/{rid}/cancel", json=NOTHING_MORE, headers={})
    assert (r.status_code, r.json()["status"]) == (200, "CANCELLED")
