import json

from conftest import audit_rows
from factories import make_organization

NIL_ID = "00000000-0000-0000-0000-000000000000"


def test_create_201_with_location_and_one_audit_event(client, test_url):
    r = client.post("/v1/organizations", json=make_organization(name="City Hospital"))
    assert r.status_code == 201 and r.headers["location"] == f"/v1/organizations/{r.json()['id']}"
    assert set(r.json()) == {"id", "kind", "name", "city", "latitude", "longitude", "phone", "stock_confirmed_at",
                             "auto_confirm_holds", "has_blood_centre", "ownership", "ownership_inferred", "data_source"}
    assert (r.json()["data_source"], r.json()["ownership"], r.json()["ownership_inferred"]) == ("manual", "unknown", False)
    for field, value in (("ownership", "government"), ("data_source", "osm"), ("ownership_inferred", True)):
        assert client.post("/v1/organizations", json=make_organization(**{field: value})).status_code == 422
        assert client.patch(f"/v1/organizations/{r.json()['id']}", json={field: value}).status_code == 422
    (row,) = audit_rows(test_url, r.json()["id"])
    assert row["event"] == "organization.created"


def test_create_validation(client):
    assert client.post("/v1/organizations", json=make_organization(kind="clinic")).status_code == 422
    assert client.post("/v1/organizations", json=make_organization(verified_at="2026-01-01")).status_code == 422
    assert client.post("/v1/organizations", json=make_organization(name="   ")).status_code == 422


def test_get_200_and_404(client):
    org = client.post("/v1/organizations", json=make_organization()).json()
    assert client.get(f"/v1/organizations/{org['id']}").json() == org
    missing = client.get(f"/v1/organizations/{NIL_ID}")
    assert missing.status_code == 404
    assert missing.json()["type"] == "urn:lifelink:problem:organization-not-found"


def test_list_filters_by_kind_and_pages(client):
    for kind in ("hospital", "blood_bank", "ngo", "hospital"):
        client.post("/v1/organizations", json=make_organization(kind=kind))
    assert len(client.get("/v1/organizations", params={"kind": "hospital"}).json()["items"]) == 2
    first = client.get("/v1/organizations", params={"limit": 3}).json()
    assert len(first["items"]) == 3 and first["next_cursor"] is not None
    second = client.get("/v1/organizations", params={"limit": 3, "cursor": first["next_cursor"]}).json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None


def test_patch_changes_only_what_was_sent_and_audits_field_names(client, test_url):
    org = client.post("/v1/organizations", json=make_organization(name="City Hospital")).json()
    r = client.patch(f"/v1/organizations/{org['id']}", json={"city": "Indore"})
    assert r.status_code == 200 and r.json()["city"] == "Indore" and r.json()["name"] == "City Hospital"
    _created, updated = audit_rows(test_url, org["id"])
    assert updated["event"] == "organization.updated" and json.loads(updated["details"]) == {"fields": ["city"]}


def test_patch_rejects_kind_empty_and_null(client):
    org = client.post("/v1/organizations", json=make_organization()).json()
    for body in ({"kind": "ngo"}, {}, {"name": None}):
        assert client.patch(f"/v1/organizations/{org['id']}", json=body).status_code == 422


def test_patch_unknown_organization_is_404(client):
    assert client.patch(f"/v1/organizations/{NIL_ID}", json={"city": "Indore"}).status_code == 404


def test_both_lists_work_side_by_side(client):
    client.post("/v1/organizations", json=make_organization())
    assert client.get("/v1/donors").status_code == 200
    assert len(client.get("/v1/organizations").json()["items"]) == 1


def test_list_filters_by_city_case_insensitively(client):
    client.post("/v1/organizations", json=make_organization(kind="blood_bank", city="Bhopal"))
    client.post("/v1/organizations", json=make_organization(kind="blood_bank", city="Indore"))
    items = client.get("/v1/organizations", params={"kind": "blood_bank", "city": " bhopal "}).json()["items"]
    assert [o["city"] for o in items] == ["Bhopal"]


def test_location_is_both_coordinates_or_neither_and_in_range(client):
    assert client.post("/v1/organizations", json=make_organization(latitude=23.2)).status_code == 422
    assert client.post("/v1/organizations", json=make_organization(latitude=91, longitude=77)).status_code == 422
    ok = client.post("/v1/organizations", json=make_organization(latitude=23.2, longitude=77.4)).json()
    assert (ok["latitude"], ok["longitude"]) == (23.2, 77.4)
    assert client.patch(f"/v1/organizations/{ok['id']}", json={"latitude": 23.3}).status_code == 422
    assert client.patch(f"/v1/organizations/{ok['id']}", json={"latitude": 23.3, "longitude": 77.5}).json()["latitude"] == 23.3
