# ruff: noqa: F811  (the imported `w` and `clock` fixtures are requested by name as parameters)
"""Stock-take (ADR 023): the bank counts its shelf, the list follows the count. Plus the summary the form starts from."""
import asyncio
import threading
import time
from datetime import datetime, timedelta

import pytest

from conftest import _fetch, as_role, audit_rows, run_sql
from factories import make_blood_unit, make_organization
from modules.emergencies import router as emergencies_router
from modules.inventory import service as inventory
from test_holds import AIIMS, HAMIDIA, T0, act, clock, post, w  # noqa: F401  (fixtures)


def units(client, bank, group="O+", n=1, first_day=5, component="red_cells"):
    """n units expiring on consecutive days from first_day: the order the FEFO guess must follow."""
    return [client.post("/v1/blood-units", json=make_blood_unit(
        bank, blood_group=group, component=component,
        expires_at=(T0 + timedelta(days=first_day + i)).isoformat())).json()["id"] for i in range(n)]


def take(client, bank, headers, *counts, component="red_cells"):
    body = {"counts": [{"blood_group": g, "component": component, "units": n} for g, n in counts]}
    return client.post(f"/v1/organizations/{bank}/stock-take", json=body, headers=headers)


def statuses(test_url, bank):
    rows = asyncio.run(_fetch(test_url, "SELECT id::text, status FROM blood_units WHERE blood_bank_id = $1::uuid", bank))
    return {r["id"]: r["status"] for r in rows}


def confirmed_at(test_url, bank):
    return asyncio.run(_fetch(test_url, "SELECT stock_confirmed_at FROM organizations WHERE id = $1::uuid", bank))[0][0]


def takes(test_url, bank):
    return [e for e in audit_rows(test_url, bank) if e["event"] == "organization.stock_take"]


# ---------- AC 7: a lower count takes the earliest-expiring surplus off the list ----------
def test_a_lower_count_issues_the_earliest_expiring_surplus_and_stamps_the_count(client, w, clock, test_url):
    b_pos = units(client, w["near"], "B+", 5)
    o_pos = units(client, w["near"], "O+", 3)
    r = take(client, w["near"], w["near_staff"], ("B+", 3), ("O+", 3))
    assert r.status_code == 200, r.text
    assert r.json()["summary"] == [{"blood_group": "B+", "component": "red_cells", "units": 3},
                                   {"blood_group": "O+", "component": "red_cells", "units": 3}]
    assert datetime.fromisoformat(r.json()["stock_confirmed_at"]) == T0 == confirmed_at(test_url, w["near"])
    s = statuses(test_url, w["near"])
    assert [s[u] for u in b_pos] == ["ISSUED", "ISSUED", "AVAILABLE", "AVAILABLE", "AVAILABLE"]   # FEFO guess
    assert {s[u] for u in o_pos} == {"AVAILABLE"}
    (event,) = takes(test_url, w["near"])
    assert event["details"] == '{"removed": {"B+/red_cells": 2}}'                 # counts only
    assert not any(u in event["details"] for u in b_pos + o_pos)                   # no unit ids


def test_an_exact_count_changes_no_unit_but_still_counts_as_fresh(client, w, clock, test_url):
    units(client, w["near"], "A-", 2)
    r = take(client, w["near"], w["near_staff"], ("A-", 2), ("AB-", 0))           # a zero for an empty pair is fine
    assert r.status_code == 200 and set(statuses(test_url, w["near"]).values()) == {"AVAILABLE"}
    assert confirmed_at(test_url, w["near"]) == T0 and takes(test_url, w["near"])[0]["details"] == '{"removed": {}}'


# ---------- AC 8: refused counts change nothing ----------
@pytest.mark.parametrize("counts, slug, named", [
    ((("B+", 3),), "stock-take-incomplete", "O+/red_cells"),            # O+ is on the list but was not counted
    ((("B+", 6), ("O+", 3)), "stock-take-increase", "B+/red_cells"),    # more than listed
    ((("B+", 5), ("O+", 3), ("A+", 1)), "stock-take-increase", "A+/red_cells"),   # a pair the list does not have
])
def test_an_incomplete_or_higher_count_is_refused_and_nothing_changes(client, w, clock, test_url, counts, slug, named):
    units(client, w["near"], "B+", 5)
    units(client, w["near"], "O+", 3)
    before = statuses(test_url, w["near"])
    r = take(client, w["near"], w["near_staff"], *counts)
    assert r.status_code == 422 and r.json()["type"].endswith(slug) and named in r.json()["detail"]
    assert statuses(test_url, w["near"]) == before
    assert confirmed_at(test_url, w["near"]) is None and takes(test_url, w["near"]) == []


@pytest.mark.parametrize("body", [
    {"counts": [{"blood_group": "B+", "component": "red_cells", "units": 1}] * 2},          # duplicate pair
    {"counts": [{"blood_group": "B+", "component": "red_cells", "units": -1}]},
    {"counts": [{"blood_group": "B+", "component": "red_cells", "units": 1, "unit_ids": []}]},  # extra field
    {"counts": [], "stock_confirmed_at": "2026-01-01T00:00:00Z"},
])
def test_malformed_counts_are_422_and_change_nothing(client, w, test_url, body):
    units(client, w["near"], "B+", 1)
    before = statuses(test_url, w["near"])
    r = client.post(f"/v1/organizations/{w['near']}/stock-take", json=body, headers=w["near_staff"])
    assert r.status_code == 422 and statuses(test_url, w["near"]) == before and confirmed_at(test_url, w["near"]) is None


# ---------- AC 9: who may count, who may read ----------
def test_only_the_banks_own_staff_count_and_the_summary_is_own_bank_or_admin(client, w, test_url):
    units(client, w["near"], "O+", 2)
    for headers in (w["far_staff"], w["staff"], {}):                              # other bank, hospital, admin
        assert take(client, w["near"], headers, ("O+", 0)).status_code == 403
    assert take(client, w["near"], {"Authorization": ""}, ("O+", 0)).status_code == 401
    assert set(statuses(test_url, w["near"]).values()) == {"AVAILABLE"}
    missing = "00000000-0000-0000-0000-000000000000"
    assert take(client, missing, w["near_staff"], ("O+", 0)).status_code == 404
    assert take(client, w["hospital"], as_role(client, test_url, "blood_bank", w["hospital"]),
                ("O+", 0)).status_code == 422                                     # not a blood bank

    def summary(headers):
        return client.get("/v1/blood-units/summary", params={"blood_bank_id": w["near"]}, headers=headers)
    assert summary(w["near_staff"]).json() == [{"blood_group": "O+", "component": "red_cells", "units": 2}]
    assert summary({}).status_code == 200                                         # admin: reading is fine
    assert [summary(h).status_code for h in (w["far_staff"], w["staff"])] == [403, 403]
    assert summary({"Authorization": ""}).status_code == 401


# ---------- AC 10: holds are never touched, and a hold racing a count stays consistent ----------
def test_reserved_and_collected_units_are_never_touched(client, w, clock, test_url):
    held, handed_over = units(client, w["near"], "O+", 1, first_day=1), units(client, w["near"], "O+", 1, first_day=2)
    shelf = units(client, w["near"], "O+", 2, first_day=3)
    post(client, w)                                            # holds the earliest unit (FEFO)
    second = post(client, w).json()["id"]                      # holds the next one ...
    assert act(client, second, "issued", w["near_staff"]).status_code == 200     # ... which the bank hands over
    assert take(client, w["near"], w["near_staff"], ("O+", 0)).status_code == 200
    s = statuses(test_url, w["near"])
    assert (s[held[0]], s[handed_over[0]], {s[u] for u in shelf}) == ("RESERVED", "ISSUED", {"ISSUED"})
    assert takes(test_url, w["near"])[0]["details"] == '{"removed": {"O+/red_cells": 2}}'   # only the 2 on the shelf


def in_background(fn):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", fn()))
    t.start()
    return t, out


def test_a_hold_that_arrives_during_a_count_skips_the_locked_units_instead_of_waiting(client, w, clock, test_url,
                                                                                         monkeypatch):
    """The count holds its FOR UPDATE locks for 1.5 s (slowed down here); reserve() uses SKIP LOCKED, so a hold posted
    meanwhile takes nothing from this bank, at once, instead of a unit whose fate the count is deciding.
    Break-it (run): drop .with_for_update() from inventory.stock_take and the hold takes a unit mid-count."""
    shelf = units(client, w["near"], "O+", 10)
    original = inventory.summary

    async def slow_summary(session, bank_id, now):                 # runs inside the stock-take transaction
        await asyncio.sleep(1.5)
        return await original(session, bank_id, now)
    monkeypatch.setattr(inventory, "summary", slow_summary)
    t, out = in_background(lambda: take(client, w["near"], w["near_staff"], ("O+", 6)))
    time.sleep(0.5)
    started = time.monotonic()
    during = post(client, w).json()
    assert time.monotonic() - started < 0.8 and during["units_allocated"] == 0      # did not wait for the lock
    t.join()
    assert out["r"].status_code == 200
    monkeypatch.setattr(inventory, "summary", original)
    after = post(client, w).json()
    assert after["units_allocated"] == 1
    s = statuses(test_url, w["near"])
    assert sorted(s[u] for u in shelf) == ["AVAILABLE"] * 5 + ["ISSUED"] * 4 + ["RESERVED"]
    assert {a["blood_unit_id"] for a in after["allocations"]} <= {u for u in shelf if s[u] == "RESERVED"}


def test_a_count_that_starts_while_a_hold_is_being_made_waits_and_never_removes_the_held_unit(
        client, w, clock, test_url, monkeypatch):
    """The other order: a hold locked the earliest unit first. The count waits for it, re-checks the row (now RESERVED)
    and leaves it out: 9 listed, 6 counted, 3 removed, and the held unit is never ISSUED under a live hold."""
    shelf = units(client, w["near"], "O+", 10)
    original = emergencies_router.publish

    async def slow_publish(session, request_id, kind):          # runs inside the create transaction, after reserve()
        await asyncio.sleep(1.0)
        await original(session, request_id, kind)
    monkeypatch.setattr(emergencies_router, "publish", slow_publish)
    t, out = in_background(lambda: post(client, w))
    time.sleep(0.4)
    r = take(client, w["near"], w["near_staff"], ("O+", 6))
    t.join()
    assert out["r"].json()["units_allocated"] == 1 and r.status_code == 200
    s = statuses(test_url, w["near"])
    assert sorted(s[u] for u in shelf) == ["AVAILABLE"] * 6 + ["ISSUED"] * 3 + ["RESERVED"]
    assert r.json()["summary"] == [{"blood_group": "O+", "component": "red_cells", "units": 6}]
    assert takes(test_url, w["near"])[0]["details"] == '{"removed": {"O+/red_cells": 3}}'
    assert s[out["r"].json()["allocations"][0]["blood_unit_id"]] == "RESERVED"


# ---------- AC 11: stock-confirmed is gone; registering never counts; demo stays demo ----------
def test_the_bare_stock_confirmed_click_is_gone_and_registering_does_not_count(client, w, clock, test_url):
    assert client.post(f"/v1/organizations/{w['near']}/stock-confirmed", headers=w["near_staff"]).status_code in (404, 405)
    units(client, w["near"], "O+", 1)
    assert confirmed_at(test_url, w["near"]) is None


def test_a_stock_take_at_a_demo_bank_leaves_its_stock_marked_demo(client, w, clock, test_url):
    asyncio.run(run_sql(test_url, f"UPDATE organizations SET data_source = 'synthetic' WHERE id = '{w['near']}'"))
    units(client, w["near"], "O+", 2)
    assert take(client, w["near"], w["near_staff"], ("O+", 1)).status_code == 200
    (bank,) = client.get("/v1/public/nearest-banks", params={"recipient": "O+", **AIIMS},
                         headers={"Authorization": ""}).json()
    assert bank["stock_is_demo"] is True and datetime.fromisoformat(bank["stock_confirmed_at"]) == T0
    assert bank["units"] == 1


# ---------- AC 12: the summary is not the paginated list ----------
def test_the_summary_counts_past_the_lists_100_limit(client, test_url):
    bank = client.post("/v1/organizations", json=make_organization(kind="blood_bank", **HAMIDIA)).json()["id"]
    asyncio.run(run_sql(test_url, f"""
        INSERT INTO blood_units (blood_bank_id, blood_group, component, collected_at, expires_at, status)
        SELECT '{bank}', CASE WHEN i % 3 = 0 THEN 'B+' ELSE 'O+' END, 'red_cells', now() - interval '1 day',
               now() + interval '20 days', 'AVAILABLE' FROM generate_series(1, 150) AS i;
        INSERT INTO blood_units (blood_bank_id, blood_group, component, collected_at, expires_at, status) VALUES
          ('{bank}', 'O+', 'red_cells', now() - interval '30 days', now() - interval '1 day', 'AVAILABLE'),
          ('{bank}', 'O+', 'red_cells', now() - interval '1 day', now() + interval '9 days', 'RESERVED'),
          ('{bank}', 'O+', 'plasma', now() - interval '1 day', now() + interval '300 days', 'AVAILABLE')"""))
    assert client.get("/v1/blood-units/summary", params={"blood_bank_id": bank}).json() == [
        {"blood_group": "B+", "component": "red_cells", "units": 50},
        {"blood_group": "O+", "component": "plasma", "units": 1},
        {"blood_group": "O+", "component": "red_cells", "units": 100}]           # expired and reserved left out
    staff = as_role(client, test_url, "blood_bank", bank)
    r = take(client, bank, staff, ("B+", 50), ("O+", 98))
    assert r.status_code == 422 and "O+/plasma" in r.json()["detail"]            # every component is counted
    body = {"counts": [{"blood_group": "B+", "component": "red_cells", "units": 50},
                       {"blood_group": "O+", "component": "red_cells", "units": 98},
                       {"blood_group": "O+", "component": "plasma", "units": 1}]}
    r = client.post(f"/v1/organizations/{bank}/stock-take", json=body, headers=staff)
    assert r.status_code == 200 and [line["units"] for line in r.json()["summary"]] == [50, 1, 98]

