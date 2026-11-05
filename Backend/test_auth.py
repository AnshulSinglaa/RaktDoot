"""Stage 2: identity. Who may log in, how tokens live and die, and who may call what (the role matrix)."""
import asyncio
import base64
import json
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

from conftest import TEST_PASSWORD, TEST_SECRET, _fetch, as_role, audit_rows, make_user, run_sql
from factories import make_blood_unit, make_donor, make_organization
from main import create_app

NEW_PASSWORD = "a long enough passphrase"


def login(c, email, password=TEST_PASSWORD):
    return c.post("/v1/auth/login", json={"email": email, "password": password})


# ---------- register, login, me ----------
def test_register_creates_a_donor_and_login_works(anon):
    r = anon.post("/v1/auth/register", json={"email": "Asha@Example.com", "password": NEW_PASSWORD})
    assert r.status_code == 201 and r.json()["role"] == "donor" and r.json()["email"] == "asha@example.com"
    assert "password" not in r.text and "hash" not in r.text
    tokens = login(anon, "asha@example.com", NEW_PASSWORD).json()
    assert tokens["token_type"] == "bearer" and tokens["expires_in"] == 15 * 60
    me = anon.get("/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.json()["email"] == "asha@example.com" and me.json()["organization_id"] is None


@pytest.mark.parametrize("body", [
    {"email": "a@example.com", "password": "short"},                             # under 12 characters
    {"email": "not-an-email", "password": NEW_PASSWORD},
    {"email": "a@example.com", "password": NEW_PASSWORD, "role": "admin"},      # the classic: sign yourself up as admin
])
def test_register_rejects_bad_input_and_self_chosen_roles(anon, body):
    assert anon.post("/v1/auth/register", json=body).status_code == 422


def test_register_twice_is_409_case_insensitively(anon):
    anon.post("/v1/auth/register", json={"email": "dup@example.com", "password": NEW_PASSWORD})
    assert anon.post("/v1/auth/register", json={"email": "DUP@example.com", "password": NEW_PASSWORD}).status_code == 409


def test_wrong_password_and_unknown_email_look_identical(anon, test_url):
    _, email = make_user(test_url, "donor")
    wrong, unknown = login(anon, email, "wrong password!!"), login(anon, "nobody@example.com")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]
    assert wrong.headers["www-authenticate"] == "Bearer"


def test_too_many_failures_lock_the_account_even_for_the_right_password(app, test_url):
    _, email = make_user(test_url, "donor")
    with TestClient(app) as c:
        for _ in range(5):
            assert login(c, email, "wrong password!!").status_code == 401
        assert login(c, email).status_code == 401                        # locked: the right password fails too
    events = [r["event"] for r in audit_rows(test_url)]
    assert events.count("user.login_failed") == 4 and events.count("user.locked") == 1


def test_lockout_is_per_ip_so_an_attacker_cannot_lock_staff_out(app, test_url):
    """B10: 5 wrong passwords from the attacker's IP lock THAT IP only; the real user on another IP still logs in,
    and their success does not unlock the attacker."""
    _, email = make_user(test_url, "blood_bank")
    # ONE TestClient per app (two would run the lifespan twice, on two event loops). The app reads the IP from the
    # ASGI scope's "client", which uvicorn fills from the socket, so this wrapper sets it per request the same way.
    ip = ["203.0.113.9"]

    async def from_ip(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (ip[0], 50000)}
        await app(scope, receive, send)

    def login_from(addr, password=TEST_PASSWORD):
        ip[0] = addr
        return login(c, email, password)

    with TestClient(from_ip) as c:
        for _ in range(5):
            assert login_from("203.0.113.9", "wrong password!!").status_code == 401
        assert login_from("203.0.113.9").status_code == 401            # attacker's IP: locked even with the password
        assert login_from("10.0.0.7").status_code == 200               # the bank's own network: not locked
        assert login_from("203.0.113.9").status_code == 401            # staff's success does not unlock the attacker
    rows = asyncio.run(_fetch(test_url, "SELECT ip, failures, locked_until FROM login_failures"))
    assert [(r["ip"], r["locked_until"] is not None) for r in rows] == [("203.0.113.9", True)]   # staff's row cleared


def test_auth_endpoints_are_rate_limited_per_ip(anon, monkeypatch):
    # pin the clock: a fixed 60 s window means 11 calls straddling a minute boundary would reset the count (a flaky test)
    monkeypatch.setattr("deps.time.monotonic", lambda: 1000.0)
    codes = [login(anon, "nobody@example.com").status_code for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


def test_refresh_is_not_rate_limited(anon):
    # found by clicking through the UI: page loads refresh, and a shared IP (hospital NAT) hit 429 after a few views
    assert {anon.post("/v1/auth/refresh").status_code for _ in range(15)} == {401}


# ---------- the access token ----------
def test_protected_endpoints_need_a_token(anon):
    r = anon.get("/v1/donors")
    assert r.status_code == 401 and r.json()["type"] == "urn:lifelink:problem:unauthorized"


@pytest.mark.parametrize("make_token", [
    lambda claims: jwt.encode(claims, "some-other-secret-" + "y" * 32, algorithm="HS256"),      # forged signature
    lambda claims: jwt.encode({**claims, "exp": 1}, TEST_SECRET, algorithm="HS256"),          # expired
    lambda claims: jwt.encode({**claims, "role": "superuser"}, TEST_SECRET, algorithm="HS256"),  # unknown role
    lambda claims: (                                                                          # alg: none, unsigned
        base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode() + "."
        + base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode() + "."
    ),
])
def test_tampered_tokens_are_rejected(anon, make_token):
    claims = {"sub": "00000000-0000-0000-0000-000000000000", "role": "admin",
              "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())}
    assert anon.get("/v1/donors", headers={"Authorization": f"Bearer {make_token(claims)}"}).status_code == 401


# ---------- refresh rotation and reuse detection ----------
def test_refresh_rotates_and_reuse_kills_the_whole_login(anon, test_url):
    _, email = make_user(test_url, "donor")
    login(anon, email)
    stolen = anon.cookies.get("refresh_token")
    first = anon.post("/v1/auth/refresh")
    assert first.status_code == 200 and anon.cookies.get("refresh_token") != stolen      # rotated
    current = anon.cookies.get("refresh_token")

    anon.cookies.set("refresh_token", stolen, path="/v1/auth")                         # the thief replays the old one
    assert anon.post("/v1/auth/refresh").status_code == 401
    anon.cookies.set("refresh_token", current, path="/v1/auth")                        # the family is dead for everyone
    assert anon.post("/v1/auth/refresh").status_code == 401
    assert "auth.refresh_reuse" in [r["event"] for r in audit_rows(test_url)]


def test_refresh_cookie_is_httponly_strict_and_scoped(anon, test_url):
    _, email = make_user(test_url, "donor")
    cookie = login(anon, email).headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/v1/auth" in cookie


def test_logout_revokes_the_refresh_token_and_is_idempotent(anon, test_url):
    _, email = make_user(test_url, "donor")
    login(anon, email)
    token = anon.cookies.get("refresh_token")
    assert anon.post("/v1/auth/logout").status_code == 204
    anon.cookies.set("refresh_token", token, path="/v1/auth")
    assert anon.post("/v1/auth/refresh").status_code == 401
    assert anon.post("/v1/auth/logout").status_code == 204
    assert anon.post("/v1/auth/refresh").status_code == 401                           # no cookie at all


def test_a_deactivated_user_cannot_log_in_or_refresh(app, test_url):
    import asyncio

    from conftest import _fetch

    user_id, email = make_user(test_url, "donor")
    with TestClient(app) as c:
        login(c, email)
        asyncio.run(_fetch(test_url, "UPDATE users SET is_active = false WHERE id = $1::uuid", user_id))
        assert c.post("/v1/auth/refresh").status_code == 401
        assert login(c, email).status_code == 401


def test_the_app_refuses_to_start_without_a_strong_secret(test_url):
    from pydantic import ValidationError

    from config import Settings

    with pytest.raises(ValidationError):
        create_app(Settings(database_url=test_url, jwt_secret="CHANGE_ME"))
    with pytest.raises(ValidationError):
        Settings(database_url=test_url, jwt_secret="s" * 32, environment="production", cookie_secure=False)


# ---------- staff accounts ----------
def test_only_admins_create_staff_and_staff_need_an_organization(client, test_url):
    # ONE TestClient per app: two would run two lifespans on two event loops sharing one pool
    org = client.post("/v1/organizations", json=make_organization(kind="hospital")).json()["id"]
    body = {"email": "doc@example.com", "password": NEW_PASSWORD, "role": "hospital", "organization_id": org}
    created = client.post("/v1/auth/users", json=body)
    assert created.status_code == 201 and created.json()["organization_id"] == org
    assert client.post("/v1/auth/users", json={**body, "email": "x@example.com", "organization_id": None}).status_code == 422
    unknown = {**body, "email": "y@example.com", "organization_id": "00000000-0000-0000-0000-000000000000"}
    assert client.post("/v1/auth/users", json=unknown).json()["type"] == "urn:lifelink:problem:unknown-organization"
    donor = as_role(client, test_url, "donor")              # a per-request header overrides the client's admin one
    assert client.post("/v1/auth/users", json={**body, "email": "z@example.com"}, headers=donor).status_code == 403


# ---------- the role matrix (RBAC) and object-level checks (BOLA) ----------
ROLES = ["donor", "hospital", "blood_bank", "ngo", "admin"]

# endpoint -> roles allowed. Anything else must get 403 (or 401 without a token).
MATRIX = {
    ("GET", "/v1/donors"): {"admin"},                     # need-to-know: threat model #2
    ("POST", "/v1/donors/00000000-0000-0000-0000-000000000000/donations"): {"blood_bank", "admin"},
    ("POST", "/v1/donors/00000000-0000-0000-0000-000000000000/verify-phone"): {"blood_bank", "ngo", "admin"},
    ("POST", "/v1/organizations"): {"admin"},
    ("POST", "/v1/auth/users"): {"admin"},
    ("GET", "/v1/blood-units"): {"blood_bank", "admin"},           # B5
    ("GET", "/v1/blood-units/00000000-0000-0000-0000-000000000000"): {"blood_bank", "admin"},    # B5 (404 when allowed)
}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("endpoint", list(MATRIX))
def test_role_matrix(anon, test_url, role, endpoint):
    method, path = endpoint
    r = anon.request(method, path, json={}, headers=as_role(anon, test_url, role))
    if role in MATRIX[endpoint]:
        assert r.status_code not in (401, 403), r.text            # allowed (an empty body may still be a 422)
    else:
        assert r.status_code == 403


# 2c: the two bank-desk endpoints act on ONE bank, so the matrix runs against a real bank, with each role's user
# belonging to it where the role has an organization. Allowed -> not 401/403; anything else -> 403.
OWN_BANK_MATRIX = {
    ("POST", "/v1/organizations/{bank}/stock-take"): {"blood_bank"},        # a count is a physical fact: no admin
    ("GET", "/v1/blood-units/summary?blood_bank_id={bank}"): {"blood_bank", "admin"},
}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("endpoint", list(OWN_BANK_MATRIX))
def test_role_matrix_for_the_bank_desk(anon, test_url, role, endpoint):
    method, path = endpoint
    bank = str(uuid.uuid4())
    asyncio.run(run_sql(test_url, f"INSERT INTO organizations (id, kind, name, city) VALUES ('{bank}', 'blood_bank', "
                                  "'B', 'Bhopal')"))
    headers = as_role(anon, test_url, role, bank if role == "blood_bank" else None)
    r = anon.request(method, path.format(bank=bank), json={"counts": []}, headers=headers)
    if role in OWN_BANK_MATRIX[endpoint]:
        assert r.status_code == 200, r.text
    else:
        assert r.status_code == 403
    assert anon.request(method, path.format(bank=bank), json={"counts": []}).status_code == 401


def test_a_donor_sees_only_their_own_profile(anon, test_url):
    alice, bob = as_role(anon, test_url, "donor"), as_role(anon, test_url, "donor")
    mine = anon.post("/v1/donors", json=make_donor(), headers=alice).json()
    assert anon.get("/v1/donors/me", headers=alice).json()["id"] == mine["id"]
    assert anon.get(f"/v1/donors/{mine['id']}", headers=alice).status_code == 200
    # Bob probing Alice's id gets the SAME 404 as a missing row: a 403 would confirm the id exists
    assert anon.get(f"/v1/donors/{mine['id']}", headers=bob).status_code == 404
    assert anon.patch(f"/v1/donors/{mine['id']}", json={"name": "x"}, headers=bob).status_code == 404
    assert anon.get("/v1/donors/me", headers=bob).status_code == 404
    assert anon.post("/v1/donors", json=make_donor(), headers=alice).json()["type"] == \
        "urn:lifelink:problem:donor-profile-exists"


def test_a_donor_cannot_reset_his_own_eligibility_but_a_blood_bank_records_donations(anon, test_url):
    """Threat model #1. Before the fix: PATCH last_donated_at=null -> eligible_now flipped to True."""
    donor = as_role(anon, test_url, "donor")
    me = anon.post("/v1/donors", json=make_donor(), headers=donor).json()
    assert me["eligible_now"] is True
    assert anon.patch(f"/v1/donors/{me['id']}", json={"last_donated_at": None}, headers=donor).status_code == 422
    bank = as_role(anon, test_url, "blood_bank")
    r = anon.post(f"/v1/donors/{me['id']}/donations", json={}, headers=bank)
    assert r.status_code == 201 and r.json()["eligible_now"] is False           # gave blood just now
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert anon.post(f"/v1/donors/{me['id']}/donations", json={"donated_at": future}, headers=bank).status_code == 422
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()      # a late paper record
    assert anon.post(f"/v1/donors/{me['id']}/donations", json={"donated_at": old}, headers=bank).json()["eligible_now"] is False
    assert "donor.donation_recorded" in [r["event"] for r in audit_rows(test_url, me["id"])]


def test_a_blood_bank_adds_stock_only_to_its_own_bank(anon, test_url):
    admin = as_role(anon, test_url, "admin")
    other_bank = anon.post("/v1/organizations", json=make_organization(kind="blood_bank"), headers=admin).json()["id"]
    own_bank = anon.post("/v1/organizations", json=make_organization(kind="blood_bank"), headers=admin).json()["id"]
    staff = as_role(anon, test_url, "blood_bank", own_bank)
    assert anon.post("/v1/blood-units", json=make_blood_unit(own_bank), headers=staff).status_code == 201
    assert anon.post("/v1/blood-units", json=make_blood_unit(other_bank), headers=staff).status_code == 403
    hospital = as_role(anon, test_url, "hospital")
    assert anon.post("/v1/blood-units", json=make_blood_unit(own_bank), headers=hospital).status_code == 403


def test_staff_edit_only_their_own_organization(anon, test_url):
    admin = as_role(anon, test_url, "admin")
    mine = anon.post("/v1/organizations", json=make_organization(), headers=admin).json()["id"]
    theirs = anon.post("/v1/organizations", json=make_organization(), headers=admin).json()["id"]
    staff = as_role(anon, test_url, "hospital", mine)
    assert anon.patch(f"/v1/organizations/{mine}", json={"city": "Indore"}, headers=staff).status_code == 200
    assert anon.patch(f"/v1/organizations/{theirs}", json={"city": "Indore"}, headers=staff).status_code == 403


def test_staff_cannot_move_or_rename_their_own_organization(anon, test_url):
    """B3: the pin decides where donors are searched and alerted. Staff moving it would aim alerts anywhere."""
    admin = as_role(anon, test_url, "admin")
    mine = anon.post("/v1/organizations", json=make_organization(), headers=admin).json()["id"]
    staff = as_role(anon, test_url, "hospital", mine)
    for body in ({"latitude": 23.3, "longitude": 77.5}, {"name": "Elsewhere Hospital"}):
        assert anon.patch(f"/v1/organizations/{mine}", json=body, headers=staff).status_code == 403
        assert anon.patch(f"/v1/organizations/{mine}", json=body, headers=admin).status_code == 200


def test_public_read_endpoints_need_no_login(anon):
    assert anon.get("/v1/organizations").status_code == 200
    assert anon.get("/v1/blood-units").status_code == 401          # B5: staff-only now (live hold activity per bank)
    assert anon.get("/v1/blood-groups/O-/compatible-donors").status_code == 200


def test_security_headers_are_on_every_response(anon):
    h = anon.get("/health/live").headers
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert h["cache-control"] == "no-store" and h["referrer-policy"] == "no-referrer"
