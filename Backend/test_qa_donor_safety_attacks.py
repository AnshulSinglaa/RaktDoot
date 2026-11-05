"""QA attacks on donor safety (plan item 1, ADR 020). Written by the QA gatekeeper.

Both findings are FIXED (ADR 020, migration 0012): the xfail markers are gone and these tests now guard the fixes.
verify-phone now requires the presented number (ruling 1), so the attack sends the OLD number."""
import asyncio

import asyncpg

from conftest import _fetch, as_role
from factories import make_donor


def plain(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def test_verify_phone_never_verifies_a_number_that_changed_mid_request(client, test_url):
    d = client.post("/v1/donors", json=make_donor(phone=None)).json()["id"]
    assert client.patch(f"/v1/donors/{d}", json={"phone": "9811111111"}).json()["phone_verified"] is False

    async def scenario():
        # A concurrent PATCH (the owner changing the number) holds the row; staff press "verify" for the OLD number.
        conn = await asyncpg.connect(plain(test_url))
        tx = conn.transaction()
        await tx.start()
        await conn.execute("UPDATE donors SET phone = '+919822222222', phone_verified_at = NULL WHERE id = $1::uuid", d)
        fut = asyncio.get_running_loop().run_in_executor(
            None, lambda: client.post(f"/v1/donors/{d}/verify-phone", json={"phone": "9811111111"}))
        await asyncio.sleep(1.5)         # verify has read +919811111111 and now waits on our row lock
        await tx.commit()
        await conn.close()
        return await fut

    assert asyncio.run(scenario()).status_code in (200, 409)
    row = asyncio.run(_fetch(test_url, "SELECT phone, phone_verified_at FROM donors WHERE id = $1::uuid", d))[0]
    assert row["phone"] == "+919822222222"
    assert row["phone_verified_at"] is None, "a number no staff member ever saw is now 'verified in person'"


def test_an_unverified_self_signup_cannot_block_a_walk_in_with_the_same_number(anon, test_url):
    squatter = as_role(anon, test_url, "donor")
    assert anon.post("/v1/donors", json=make_donor(phone="9833333333"), headers=squatter).status_code == 201
    ngo = as_role(anon, test_url, "ngo")
    r = anon.post("/v1/donors", json=make_donor(phone="9833333333"), headers=ngo)   # the real owner, at a camp
    assert r.status_code == 201, r.json()
