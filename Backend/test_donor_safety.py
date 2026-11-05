"""Donor safety (migrations 0011 + 0012, ADR 020): the donation interval depends on sex (90 days male, 120 otherwise),
and only donors whose phone was verified IN PERSON are matched or counted. A donor we cannot reach is noise in a crisis.
Verification requires PRESENTING the number, and a number is exclusive only once verified (no squatting, no oracle)."""
import asyncio
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from conftest import _fetch, as_role, audit_rows
from factories import make_donor, make_organization
from main import get_now

AIIMS = {"latitude": 23.2080, "longitude": 77.4600}
NEAR = {"latitude": 23.21, "longitude": 77.46}           # < 1 km from AIIMS
NOW = datetime.now(timezone.utc).replace(microsecond=0)  # pinned: real enough that login tokens stay valid


def ago(days: int) -> str:
    return (NOW - timedelta(days=days)).isoformat()


@pytest.fixture
def pinned(app, client):
    """`client` (admin) has logged in already; from here on every endpoint sees the same instant."""
    app.dependency_overrides[get_now] = lambda: NOW
    yield client
    app.dependency_overrides.pop(get_now, None)


@pytest.fixture
def hospital(client, test_url):
    org = client.post("/v1/organizations", json=make_organization(kind="hospital", **AIIMS)).json()["id"]
    return as_role(client, test_url, "hospital", org)


def emergency(client, hospital):
    return client.post("/v1/emergencies", json={"blood_group": "O+", "units_needed": 1}, headers=hospital).json()["id"]


def matched(client, hospital, req) -> set[str]:
    return {m["donor_id"] for m in client.get(f"/v1/emergencies/{req}/donor-matches", headers=hospital).json()}


def public_total(client) -> int | None:
    """Donors counted in the visible bands (the API hides `total` whenever any band is hidden, B1). None = none shown."""
    params = {"recipient": "O+", **NEAR}
    bands = client.get("/v1/public/donors-nearby", params=params, headers={"Authorization": ""}).json()["bands"]
    shown = [b["donors"] for b in bands if b["donors"] is not None]
    return sum(shown) if shown else None


def self_signup(anon, test_url, phone="9876500001"):
    """A donor creating their own profile: the phone is NOT verified."""
    me = as_role(anon, test_url, "donor")
    return me, anon.post("/v1/donors", json=make_donor(phone=phone), headers=me).json()["id"]


def row(test_url, d, cols="phone_verified_at"):
    return asyncio.run(_fetch(test_url, f"SELECT {cols} FROM donors WHERE id = $1::uuid", d))[0]


# ---------- 5: interval by sex, through the API with a pinned clock ----------
def test_eligible_now_uses_the_interval_for_the_donors_sex(pinned):
    def eligible(sex, days):
        return pinned.post("/v1/donors", json=make_donor(sex=sex, last_donated_at=ago(days))).json()["eligible_now"]

    assert eligible("female", 100) is False
    assert eligible("male", 100) is True
    assert eligible("female", 120) is True
    assert eligible("other", 100) is False                 # anything but male gets the longer, safer interval


def test_matching_uses_the_interval_for_the_donors_sex(hospital, pinned):
    req = emergency(pinned, hospital)
    ids = {key: pinned.post("/v1/donors", json=make_donor(sex=sex, last_donated_at=ago(days), **NEAR)).json()["id"]
           for key, sex, days in (("f100", "female", 100), ("m100", "male", 100), ("f120", "female", 120))}
    assert matched(pinned, hospital, req) == {ids["m100"], ids["f120"]}


def test_public_counts_use_the_interval_for_the_donors_sex(pinned):
    for sex, days in (("female", 100), ("male", 100), ("female", 120)):
        for _ in range(5):
            pinned.post("/v1/donors", json=make_donor(sex=sex, last_donated_at=ago(days), **NEAR))
    assert public_total(pinned) == 10                      # the 5 women who gave 100 days ago are not counted


def test_a_pre_0011_donor_with_unknown_sex_gets_the_longer_interval(pinned, test_url):
    d = pinned.post("/v1/donors", json=make_donor(sex="male", last_donated_at=ago(100))).json()
    asyncio.run(_fetch(test_url, "UPDATE donors SET sex = NULL WHERE id = $1::uuid", d["id"]))
    assert pinned.get(f"/v1/donors/{d['id']}").json()["eligible_now"] is False


# ---------- 6: unverified donors are never matched or counted ----------
def test_unverified_donors_are_not_matched_or_counted_until_verified(client, hospital):
    req = emergency(client, hospital)
    # admin registers without a phone (unverified), then adds one: still unverified until someone sees the donor
    ids = [client.post("/v1/donors", json=make_donor(phone=None, **NEAR)).json()["id"] for _ in range(5)]
    assert matched(client, hospital, req) == set() and public_total(client) is None
    for n, i in enumerate(ids):
        assert client.patch(f"/v1/donors/{i}", json={"phone": f"98000000{n:02d}"}).json()["phone_verified"] is False
    assert matched(client, hospital, req) == set() and public_total(client) is None
    for n, i in enumerate(ids):
        assert client.post(f"/v1/donors/{i}/verify-phone", json={"phone": f"98000000{n:02d}"}).status_code == 200
    assert matched(client, hospital, req) == set(ids) and public_total(client) == 5


def test_donors_without_recorded_consent_are_not_matched_or_counted(client, hospital, test_url):
    """B8 (DPDP): pre-0007 rows have consent_at NULL. No consent on record = no matching, not even a count."""
    req = emergency(client, hospital)
    ids = [client.post("/v1/donors", json=make_donor(**NEAR)).json()["id"] for _ in range(5)]
    assert matched(client, hospital, req) == set(ids) and public_total(client) == 5
    asyncio.run(_fetch(test_url, "UPDATE donors SET consent_at = NULL WHERE id = $1::uuid", ids[0]))
    assert matched(client, hospital, req) == set(ids[1:]) and public_total(client) is None     # 4: below the floor


def test_staff_cannot_open_a_matched_donors_full_record(client, hospital, test_url):
    """B2: donor-matches shows a first name and a distance band on purpose. The id it returns must not unlock the
    full record (name, email, phone) via GET /v1/donors/{id}: only the donor and admins. 404, never 403 (no oracle)."""
    req = emergency(client, hospital)
    d = client.post("/v1/donors", json=make_donor(**NEAR)).json()["id"]
    assert matched(client, hospital, req) == {d}
    for staff in (hospital, as_role(client, test_url, "blood_bank"), as_role(client, test_url, "ngo")):
        assert client.get(f"/v1/donors/{d}", headers=staff).status_code == 404
    assert client.get(f"/v1/donors/{d}").status_code == 200                     # admin


# ---------- 7: who registers decides whether the phone starts verified ----------
@pytest.mark.parametrize("role", ["ngo", "blood_bank", "admin"])
def test_staff_registering_a_walk_in_with_a_phone_verifies_it(anon, test_url, role):
    staff = as_role(anon, test_url, role)
    assert anon.post("/v1/donors", json=make_donor(), headers=staff).json()["phone_verified"] is True
    assert anon.post("/v1/donors", json=make_donor(phone=None), headers=staff).json()["phone_verified"] is False


def test_a_self_registered_phone_is_not_verified(anon, test_url):
    _, d = self_signup(anon, test_url)
    assert row(test_url, d)[0] is None


# ---------- 8: POST /donors/{id}/verify-phone: staff must present the number ----------
@pytest.mark.parametrize("role", ["blood_bank", "ngo", "admin"])
def test_staff_verify_the_number_they_saw_and_every_check_is_audited(anon, test_url, role):
    _, d = self_signup(anon, test_url)
    staff = as_role(anon, test_url, role)
    r = anon.post(f"/v1/donors/{d}/verify-phone", json={"phone": "+91 98765-00001"}, headers=staff)   # any format
    assert r.status_code == 200 and r.json()["phone_verified"] is True
    assert [e["event"] for e in audit_rows(test_url, d)].count("donor.phone_verified") == 1
    first = row(test_url, d)[0]
    assert anon.post(f"/v1/donors/{d}/verify-phone", json={"phone": "9876500001"}, headers=staff).status_code == 200
    assert row(test_url, d)[0] == first                                                        # keeps the first date
    assert [e["event"] for e in audit_rows(test_url, d)].count("donor.phone_verified") == 2    # ...logs each check


def test_a_different_number_is_409_mismatch_and_never_echoes_the_stored_one(anon, test_url):
    _, d = self_signup(anon, test_url)
    r = anon.post(f"/v1/donors/{d}/verify-phone", json={"phone": "9876500002"}, headers=as_role(anon, test_url, "ngo"))
    assert r.status_code == 409 and r.json()["type"] == "urn:lifelink:problem:phone-mismatch"
    assert "98765" not in r.text
    assert row(test_url, d)[0] is None


def test_verify_phone_refuses_donors_hospitals_strangers_and_bad_input(anon, test_url):
    me, d = self_signup(anon, test_url)
    body = {"phone": "9876500001"}
    assert anon.post(f"/v1/donors/{d}/verify-phone", json=body, headers=me).status_code == 403     # cannot vouch for self
    hospital = as_role(anon, test_url, "hospital")
    assert anon.post(f"/v1/donors/{d}/verify-phone", json=body, headers=hospital).status_code == 403
    assert anon.post(f"/v1/donors/{d}/verify-phone", json=body).status_code == 401
    admin = as_role(anon, test_url, "admin")
    assert anon.post(f"/v1/donors/{d}/verify-phone", json={"phone": "5876500001"}, headers=admin).status_code == 422
    assert anon.post(f"/v1/donors/{d}/verify-phone", json={}, headers=admin).status_code == 422        # number required
    assert row(test_url, d)[0] is None


def test_verify_phone_unknown_donor_and_no_phone(client):
    nil = "00000000-0000-0000-0000-000000000000"
    r = client.post(f"/v1/donors/{nil}/verify-phone", json={"phone": "9876500001"})
    assert r.status_code == 404 and r.json()["type"] == "urn:lifelink:problem:donor-not-found"
    d = client.post("/v1/donors", json=make_donor(phone=None)).json()["id"]
    r = client.post(f"/v1/donors/{d}/verify-phone", json={"phone": "9876500001"})
    assert r.status_code == 422 and r.json()["type"] == "urn:lifelink:problem:no-phone"


def test_a_number_verified_on_another_donor_cannot_be_verified_again(anon, test_url):
    admin = as_role(anon, test_url, "admin")
    anon.post("/v1/donors", json=make_donor(phone="9876500001"), headers=admin)                 # verified walk-in
    _, squatter = self_signup(anon, test_url, "9876500001")                                     # allowed: 201
    r = anon.post(f"/v1/donors/{squatter}/verify-phone", json={"phone": "9876500001"}, headers=admin)
    assert r.status_code == 409 and r.json()["type"] == "urn:lifelink:problem:phone-taken"
    assert row(test_url, squatter)[0] is None


# ---------- 9: a donation verifies the phone only if the donor shows the same number ----------
@pytest.mark.parametrize("body, verifies", [({}, False), ({"phone": "9876500009"}, False), ({"phone": "98765 00001"}, True)])
def test_a_donation_is_always_recorded_and_verifies_only_a_matching_number(anon, test_url, body, verifies):
    _, d = self_signup(anon, test_url, "9876500001")
    r = anon.post(f"/v1/donors/{d}/donations", json=body, headers=as_role(anon, test_url, "blood_bank"))
    assert r.status_code == 201 and r.json()["eligible_now"] is False                           # recorded
    assert r.json()["phone_verified"] is verifies


def test_a_donor_without_a_phone_can_still_donate(anon, test_url):
    no_phone = anon.post("/v1/donors", json=make_donor(phone=None), headers=as_role(anon, test_url, "admin")).json()["id"]
    bank = as_role(anon, test_url, "blood_bank")
    r = anon.post(f"/v1/donors/{no_phone}/donations", json={"phone": "9876500001"}, headers=bank)
    assert r.status_code == 201 and r.json()["phone_verified"] is False


def test_a_late_paper_record_never_moves_the_last_donation_back(client, test_url):
    d = client.post("/v1/donors", json=make_donor()).json()["id"]
    client.post(f"/v1/donors/{d}/donations", json={"donated_at": ago(10)})
    client.post(f"/v1/donors/{d}/donations", json={"donated_at": ago(200)})
    assert row(test_url, d, "last_donated_at")[0] == datetime.fromisoformat(ago(10))


def test_a_donation_never_verifies_a_number_that_changed_mid_request(client, test_url):
    """QA's verify-phone attack, on the donation path: a PATCH holds the row, the bank presents the OLD number."""
    d = client.post("/v1/donors", json=make_donor(phone=None)).json()["id"]
    client.patch(f"/v1/donors/{d}", json={"phone": "9811111111"})

    async def scenario():
        conn = await asyncpg.connect(test_url.replace("postgresql+asyncpg://", "postgresql://", 1))
        tx = conn.transaction()
        await tx.start()
        await conn.execute("UPDATE donors SET phone = '+919822222222', phone_verified_at = NULL WHERE id = $1::uuid", d)
        fut = asyncio.get_running_loop().run_in_executor(
            None, lambda: client.post(f"/v1/donors/{d}/donations", json={"phone": "9811111111"}))
        await asyncio.sleep(1.5)             # the donation's UPDATE now waits on our row lock
        await tx.commit()
        await conn.close()
        return await fut

    r = asyncio.run(scenario())
    assert r.status_code == 201 and r.json()["phone_verified"] is False                         # recorded, not verified
    got = row(test_url, d, "phone, phone_verified_at, last_donated_at")
    assert (got["phone"], got["phone_verified_at"]) == ("+919822222222", None) and got["last_donated_at"] is not None


# ---------- 10: a new number must be seen again; the audit never holds it ----------
def test_changing_the_phone_resets_verification_and_audits_field_names_only(anon, test_url):
    admin = as_role(anon, test_url, "admin")
    me, d = self_signup(anon, test_url)
    anon.post(f"/v1/donors/{d}/verify-phone", json={"phone": "9876500001"}, headers=admin)
    r = anon.patch(f"/v1/donors/{d}", json={"phone": "+91 98765-43210"}, headers=me)             # the owner
    assert r.status_code == 200 and r.json()["phone_verified"] is False
    anon.post(f"/v1/donors/{d}/verify-phone", json={"phone": "9876543210"}, headers=admin)
    assert anon.patch(f"/v1/donors/{d}", json={"phone": "9876543211"}, headers=admin).json()["phone_verified"] is False
    rows = audit_rows(test_url, d)
    assert [r["details"] for r in rows if r["event"] == "donor.updated"] == ['{"fields": ["phone"]}'] * 2
    assert "98765" not in str(rows)


# ---------- 11: unique among VERIFIED donors only: no squatting, no "is this number registered?" oracle ----------
def test_self_signup_and_patch_reusing_any_number_never_get_a_phone_409(anon, test_url):
    anon.post("/v1/donors", json=make_donor(phone="9811111111"), headers=as_role(anon, test_url, "admin"))   # verified
    self_signup(anon, test_url, "9811111111")                                                    # 201 (else KeyError)
    me, b = self_signup(anon, test_url, "9811111112")
    r = anon.patch(f"/v1/donors/{b}", json={"phone": "9811111111"}, headers=me)
    assert r.status_code == 200 and r.json()["phone_verified"] is False


def test_staff_registering_a_verified_duplicate_is_409_and_leaves_nothing(client, test_url):
    client.post("/v1/donors", json=make_donor(phone="9811111111"))
    r = client.post("/v1/donors", json=make_donor(phone="+919811111111"), headers={"Idempotency-Key": "dup-phone"})
    assert r.status_code == 409 and r.json()["type"] == "urn:lifelink:problem:phone-taken"
    assert asyncio.run(_fetch(test_url, "SELECT count(*) FROM donors"))[0][0] == 1
    assert [e["event"] for e in audit_rows(test_url)] == ["donor.registered"]
    keys = "SELECT count(*) FROM idempotency_keys WHERE idempotency_key LIKE '%:dup-phone'"
    assert asyncio.run(_fetch(test_url, keys))[0][0] == 0


def test_a_verifying_donation_that_clashes_is_409_and_records_nothing(anon, test_url):
    anon.post("/v1/donors", json=make_donor(phone="9811111111"), headers=as_role(anon, test_url, "admin"))
    _, squatter = self_signup(anon, test_url, "9811111111")
    bank = as_role(anon, test_url, "blood_bank")
    r = anon.post(f"/v1/donors/{squatter}/donations", json={"phone": "9811111111"}, headers=bank)
    assert r.status_code == 409 and r.json()["type"] == "urn:lifelink:problem:phone-taken"
    assert tuple(row(test_url, squatter, "last_donated_at, phone_verified_at")) == (None, None)


def test_an_unverified_squatter_does_not_block_the_real_owner_and_stays_unverified(anon, test_url):
    _, squatter = self_signup(anon, test_url, "9833333333")
    r = anon.post("/v1/donors", json=make_donor(phone="9833333333"), headers=as_role(anon, test_url, "ngo"))
    assert r.status_code == 201 and r.json()["phone_verified"] is True
    assert row(test_url, squatter)[0] is None
