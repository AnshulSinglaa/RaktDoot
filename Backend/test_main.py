import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from config import Settings
from factories import make_donor
from main import create_app, get_now, get_session

GOOD = {"name": "Asha", "email": "asha@example.com", "age": 30, "blood_group": "O+"}   # fixed email: for duplicate tests
NIL_ID = "00000000-0000-0000-0000-000000000000"


def test_live(client):
    assert client.get("/health/live").json() == {"status": "alive"}


def test_info_uses_settings_given_to_the_app(test_url, fresh_db):
    app = create_app(Settings(database_url=test_url, environment="test-env"))
    with TestClient(app) as c:
        assert c.get("/info").json() == {"app": "LifeLink", "environment": "test-env"}


# ---------- registering donors ----------
def test_register_201_and_hides_email(client):
    r = client.post("/v1/donors", json=GOOD)
    assert r.status_code == 201
    assert "email" not in r.json() and "age" not in r.json()


def test_register_sets_location_header(client):
    r = client.post("/v1/donors", json=make_donor())
    assert r.headers["location"] == f"/v1/donors/{r.json()['id']}"


def test_register_accepts_city(client):
    assert client.post("/v1/donors", json=make_donor(city="Bhopal")).status_code == 201
    assert client.post("/v1/donors", json=make_donor(city="   ")).status_code == 422


def test_duplicate_email_409(client):
    client.post("/v1/donors", json=GOOD)
    assert client.post("/v1/donors", json=GOOD).status_code == 409


def test_underage_422(client):
    assert client.post("/v1/donors", json=make_donor(age=17)).status_code == 422


def test_email_duplicate_is_case_insensitive(client):
    client.post("/v1/donors", json=GOOD)
    assert client.post("/v1/donors", json={**GOOD, "email": "ASHA@EXAMPLE.COM"}).status_code == 409


def test_unknown_field_is_422(client):
    assert client.post("/v1/donors", json=make_donor(is_admin=True)).status_code == 422


def test_get_200_and_404(client):
    created = client.post("/v1/donors", json=make_donor()).json()
    assert client.get(f"/v1/donors/{created['id']}").status_code == 200
    assert client.get(f"/v1/donors/{NIL_ID}").status_code == 404


def test_eligible_now_uses_pinned_clock(app):
    app.dependency_overrides[get_now] = lambda: datetime(2026, 10, 7, tzinfo=timezone.utc)
    with TestClient(app) as c:
        recent = c.post("/v1/donors", json=make_donor(last_donated_at="2026-09-20T00:00:00Z")).json()
        old = c.post("/v1/donors", json=make_donor(last_donated_at="2026-05-01T00:00:00Z")).json()
        assert recent["eligible_now"] is False
        assert old["eligible_now"] is True


def test_patch_updates_only_sent_fields(client):
    d = client.post("/v1/donors", json=make_donor(name="Asha")).json()
    r = client.patch(f"/v1/donors/{d['id']}", json={"is_available": False})
    assert r.status_code == 200
    assert r.json()["is_available"] is False and r.json()["name"] == "Asha"
    assert r.json()["eligible_now"] is False


def test_patch_404_and_422(client):
    assert client.patch(f"/v1/donors/{NIL_ID}", json={"is_available": False}).status_code == 404
    d = client.post("/v1/donors", json=make_donor()).json()
    assert client.patch(f"/v1/donors/{d['id']}", json={}).status_code == 422
    assert client.patch(f"/v1/donors/{d['id']}", json={"age": 99}).status_code == 422


def test_compatible_donors_endpoint(client):
    r = client.get("/v1/blood-groups/AB-/compatible-donors")
    assert r.status_code == 200 and set(r.json()) == {"AB-", "A-", "B-", "O-"}
    assert client.get("/v1/blood-groups/Z+/compatible-donors").status_code == 422


# ---------- listing: filters and keyset pagination ----------
def test_list_filter(client):
    client.post("/v1/donors", json=make_donor())
    client.post("/v1/donors", json=make_donor(blood_group="A+"))
    assert len(client.get("/v1/donors").json()["items"]) == 2
    assert len(client.get("/v1/donors", params={"blood_group": "A+"}).json()["items"]) == 1


def test_filter_by_is_available(client):
    a = client.post("/v1/donors", json=make_donor()).json()
    client.post("/v1/donors", json=make_donor())
    client.patch(f"/v1/donors/{a['id']}", json={"is_available": False})
    items = client.get("/v1/donors", params={"is_available": "false"}).json()["items"]
    assert [d["id"] for d in items] == [a["id"]]


def test_list_is_paged_and_walks_everything_exactly_once(client):
    ids = [client.post("/v1/donors", json=make_donor()).json()["id"] for _ in range(5)]
    seen, cursor, late_sent = [], None, False
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = client.get("/v1/donors", params=params).json()
        seen += [d["id"] for d in page["items"]]
        if not late_sent:       # a new donor arrives mid-walk: paging must not repeat or skip anyone
            client.post("/v1/donors", json=make_donor())
            late_sent = True
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 6
    assert set(ids) <= set(seen)


def test_last_page_has_no_cursor(client):
    client.post("/v1/donors", json=make_donor())
    assert client.get("/v1/donors", params={"limit": 5}).json()["next_cursor"] is None


def test_exact_fit_last_page_has_no_cursor(client):
    # kills the mutant `len(rows) >= limit`: a page that is exactly full must not promise another page
    for _ in range(4):
        client.post("/v1/donors", json=make_donor())
    first = client.get("/v1/donors", params={"limit": 2}).json()
    assert first["next_cursor"] is not None
    last = client.get("/v1/donors", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert len(last["items"]) == 2 and last["next_cursor"] is None


def test_bad_cursor_and_limits_are_422(client):
    r = client.get("/v1/donors", params={"cursor": "garbage"})
    assert r.status_code == 422 and r.json()["type"] == "urn:lifelink:problem:invalid-cursor"
    assert client.get("/v1/donors", params={"limit": 0}).status_code == 422
    assert client.get("/v1/donors", params={"limit": 101}).status_code == 422


def test_cursor_without_a_timezone_is_rejected(client):
    raw = "2026-10-10T00:00:00|00000000-0000-0000-0000-000000000001"
    cursor = base64.urlsafe_b64encode(raw.encode()).decode()
    r = client.get("/v1/donors", params={"cursor": cursor})
    assert r.status_code == 422 and r.json()["type"] == "urn:lifelink:problem:invalid-cursor"


def test_old_unversioned_url_is_gone_but_health_is_not(client):
    assert client.get("/donors").status_code == 404
    assert client.get("/health/live").status_code == 200


# ---------- one error format: problem details ----------
def test_validation_problem_lists_fields_but_never_echoes_input(client):
    r = client.post("/v1/donors", json=make_donor(age=17, email="leak@example.com"))
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "urn:lifelink:problem:validation-error" and body["status"] == 422
    assert [e["field"] for e in body["errors"]] == ["body.age"]
    assert "leak@example.com" not in r.text


def test_domain_problems_have_stable_types(client):
    client.post("/v1/donors", json=GOOD)
    dup = client.post("/v1/donors", json=GOOD)
    assert dup.status_code == 409
    assert dup.headers["content-type"].startswith("application/problem+json")
    assert dup.json()["type"] == "urn:lifelink:problem:duplicate-email"
    assert "asha@example.com" not in dup.text
    missing = client.get(f"/v1/donors/{NIL_ID}")
    assert missing.json()["type"] == "urn:lifelink:problem:donor-not-found"
    assert missing.json()["instance"] == f"/v1/donors/{NIL_ID}"


def test_framework_errors_use_the_same_format(client):
    r = client.get("/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["type"] == "about:blank" and r.json()["title"] == "Not Found"


def test_wrong_method_is_a_problem_and_keeps_the_allow_header(client):
    r = client.delete("/v1/donors")
    assert r.status_code == 405
    assert r.headers["content-type"].startswith("application/problem+json")
    assert "allow" in r.headers


def test_unexpected_error_is_a_generic_problem_and_leaks_nothing(app):
    def boom():
        raise RuntimeError("secret internals")

    app.dependency_overrides[get_now] = boom
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/v1/donors")
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/problem+json")
    assert "secret" not in r.text


# ---------- idempotency keys ----------
def test_retry_with_same_key_replays_the_first_answer(client):
    headers = {"Idempotency-Key": "signup-001"}
    first = client.post("/v1/donors", json=GOOD, headers=headers)
    retry = client.post("/v1/donors", json=GOOD, headers=headers)
    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert retry.headers["idempotent-replayed"] == "true" and "idempotent-replayed" not in first.headers
    assert retry.headers["location"] == first.headers["location"]
    assert len(client.get("/v1/donors").json()["items"]) == 1


def test_same_key_with_a_different_body_is_rejected(client):
    headers = {"Idempotency-Key": "signup-002"}
    client.post("/v1/donors", json=GOOD, headers=headers)
    r = client.post("/v1/donors", json={**GOOD, "age": 31}, headers=headers)
    assert r.status_code == 422
    assert r.json()["type"] == "urn:lifelink:problem:idempotency-key-reuse"


def test_without_a_key_a_repeat_is_still_a_409(client):
    client.post("/v1/donors", json=GOOD)
    assert client.post("/v1/donors", json=GOOD).status_code == 409


def test_empty_key_is_rejected(client):
    assert client.post("/v1/donors", json=GOOD, headers={"Idempotency-Key": ""}).status_code == 422


def test_simultaneous_retries_with_one_key_create_one_donor(client):
    headers = {"Idempotency-Key": "signup-003"}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: client.post("/v1/donors", json=GOOD, headers=headers), range(8)))
    assert [r.status_code for r in results] == [201] * 8
    assert len({r.json()["id"] for r in results}) == 1
    assert sum(r.headers.get("idempotent-replayed") == "true" for r in results) == 7
    assert len(client.get("/v1/donors").json()["items"]) == 1


# ---------- health and persistence ----------
def test_ready_is_200_when_db_is_up(client):
    assert client.get("/health/ready").json() == {"status": "ready"}


def test_ready_is_503_when_db_is_down(app):
    class BrokenSession:
        async def execute(self, *a, **k):
            raise ConnectionError("db down")

    async def broken():
        yield BrokenSession()

    app.dependency_overrides[get_session] = broken
    with TestClient(app) as c:
        r = c.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["type"] == "urn:lifelink:problem:database-unavailable"


def test_data_survives_an_app_restart(settings, fresh_db):
    with TestClient(create_app(settings)) as first:
        donor = first.post("/v1/donors", json=make_donor()).json()
    with TestClient(create_app(settings)) as second:          # a brand-new app, a brand-new pool
        assert second.get(f"/v1/donors/{donor['id']}").status_code == 200


def test_simultaneous_duplicate_signups_have_exactly_one_winner(client):
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: client.post("/v1/donors", json=GOOD).status_code, range(8)))
    assert sorted(codes) == [201] + [409] * 7
