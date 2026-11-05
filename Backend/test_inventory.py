from datetime import datetime, timedelta, timezone

from conftest import as_role, audit_rows
from factories import make_blood_unit, make_organization

NIL_ID = "00000000-0000-0000-0000-000000000000"


def bank(client, kind="blood_bank") -> str:
    return client.post("/v1/organizations", json=make_organization(kind=kind)).json()["id"]


def test_register_201_available_with_location_and_one_audit_event(client, test_url):
    r = client.post("/v1/blood-units", json=make_blood_unit(bank(client), blood_group="AB-"))
    assert r.status_code == 201 and r.headers["location"] == f"/v1/blood-units/{r.json()['id']}"
    assert r.json()["status"] == "AVAILABLE" and r.json()["blood_group"] == "AB-"
    assert set(r.json()) == {"id", "blood_bank_id", "blood_group", "component", "collected_at", "expires_at", "status"}
    (row,) = audit_rows(test_url, r.json()["id"])
    assert row["event"] == "blood_unit.registered"


def test_an_unknown_organization_is_a_422_and_leaves_nothing_behind(client, test_url):
    r = client.post("/v1/blood-units", json=make_blood_unit(NIL_ID))
    assert r.status_code == 422 and r.json()["type"] == "urn:lifelink:problem:unknown-organization"
    assert client.get("/v1/blood-units").json()["items"] == []
    assert audit_rows(test_url) == []


def test_shelf_life_violations_are_422(client):
    bank_id = bank(client)
    now = datetime.now(timezone.utc)
    too_long = make_blood_unit(bank_id, component="platelets", expires_at=(now + timedelta(days=7)).isoformat())
    r = client.post("/v1/blood-units", json=too_long)          # collected yesterday + expires in 7 days = 8 days > 7
    assert r.status_code == 422 and r.json()["type"] == "urn:lifelink:problem:invalid-shelf-life"
    assert "platelets" in r.json()["detail"]
    expired = make_blood_unit(
        bank_id, collected_at=(now - timedelta(days=2)).isoformat(), expires_at=(now - timedelta(hours=1)).isoformat()
    )
    assert client.post("/v1/blood-units", json=expired).status_code == 422


def test_a_new_unit_cannot_arrive_in_any_status_but_available(client):
    assert client.post("/v1/blood-units", json=make_blood_unit(bank(client), status="ISSUED")).status_code == 422


def test_get_200_and_404(client):
    unit = client.post("/v1/blood-units", json=make_blood_unit(bank(client))).json()
    assert client.get(f"/v1/blood-units/{unit['id']}").json() == unit
    missing = client.get(f"/v1/blood-units/{NIL_ID}")
    assert missing.status_code == 404 and missing.json()["type"] == "urn:lifelink:problem:blood-unit-not-found"


def test_list_is_soonest_expiry_first_and_pages_without_repeats(client):
    bank_id = bank(client)
    now = datetime.now(timezone.utc)
    for days in (9, 3, 5):
        client.post("/v1/blood-units", json=make_blood_unit(bank_id, expires_at=(now + timedelta(days=days)).isoformat()))
    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = client.get("/v1/blood-units", params=params).json()
        seen += page["items"]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 3 and len({u["id"] for u in seen}) == 3
    assert [u["expires_at"] for u in seen] == sorted(u["expires_at"] for u in seen)


def test_list_filters(client):
    a, b = bank(client), bank(client)
    client.post("/v1/blood-units", json=make_blood_unit(a, blood_group="O+"))
    client.post("/v1/blood-units", json=make_blood_unit(a, blood_group="A-"))
    client.post("/v1/blood-units", json=make_blood_unit(b, blood_group="O+"))

    def count(**params):
        return len(client.get("/v1/blood-units", params=params).json()["items"])

    assert count(blood_bank_id=a) == 2
    assert count(blood_group="O+") == 2
    assert count(blood_bank_id=a, blood_group="A-") == 1
    assert count(status="ISSUED") == 0
    assert client.get("/v1/blood-units", params={"blood_group": "Z+"}).status_code == 422


def test_units_cannot_be_patched_or_deleted_yet(client):
    unit = client.post("/v1/blood-units", json=make_blood_unit(bank(client))).json()
    assert client.patch(f"/v1/blood-units/{unit['id']}", json={"status": "ISSUED"}).status_code == 405
    assert client.delete(f"/v1/blood-units/{unit['id']}").status_code == 405     # status moves through Stage 3 rules


def test_any_organization_may_hold_stock_for_now(client):
    # a deliberate limit (docs/adr/012): when only blood banks may hold stock, this test changes on purpose
    assert client.post("/v1/blood-units", json=make_blood_unit(bank(client, kind="ngo"))).status_code == 201


def test_availability_was_removed(client):
    """B5: superseded by /v1/public/nearest-banks (rounded location, rate limit); it leaked per-bank stock to anyone."""
    assert client.get("/v1/blood-units/availability", params={"recipient": "O-"}).status_code in (404, 422)


def test_unit_lists_are_staff_only_and_a_bank_sees_only_its_own(anon, test_url):
    """B5: unit lists show live hold activity per bank. Anonymous 401, hospital/ngo/donor 403, a bank its own only."""
    admin = as_role(anon, test_url, "admin")
    mine = anon.post("/v1/organizations", json=make_organization(kind="blood_bank"), headers=admin).json()["id"]
    theirs = anon.post("/v1/organizations", json=make_organization(kind="blood_bank"), headers=admin).json()["id"]
    a = anon.post("/v1/blood-units", json=make_blood_unit(mine), headers=admin).json()["id"]
    b = anon.post("/v1/blood-units", json=make_blood_unit(theirs), headers=admin).json()["id"]
    staff = as_role(anon, test_url, "blood_bank", mine)
    assert anon.get("/v1/blood-units").status_code == 401
    assert anon.get(f"/v1/blood-units/{a}").status_code == 401
    for role in ("donor", "hospital", "ngo"):
        assert anon.get("/v1/blood-units", headers=as_role(anon, test_url, role)).status_code == 403
    ids = lambda **p: [u["id"] for u in anon.get("/v1/blood-units", params=p, headers=staff).json()["items"]]  # noqa: E731
    assert ids() == [a] and ids(blood_bank_id=mine) == [a]
    assert anon.get("/v1/blood-units", params={"blood_bank_id": theirs}, headers=staff).status_code == 403
    assert anon.get(f"/v1/blood-units/{a}", headers=staff).status_code == 200
    assert anon.get(f"/v1/blood-units/{b}", headers=staff).status_code == 404        # no oracle for other banks' ids
    assert sorted(u["id"] for u in anon.get("/v1/blood-units", headers=admin).json()["items"]) == sorted([a, b])
