import asyncio
import json

import asyncpg
import pytest
from fastapi.testclient import TestClient

from conftest import as_role, audit_rows, run_sql
from factories import make_donor
from main import get_audit_repo


def test_registering_a_donor_writes_one_audit_event_without_personal_data(client, test_url):
    donor = client.post("/v1/donors", json=make_donor(name="Asha Verma", email="asha.verma@example.com")).json()
    (row,) = audit_rows(test_url, donor["id"])
    assert row["event"] == "donor.registered" and row["entity"] == "donor"
    assert row["actor_id"] is not None and json.loads(row["details"]) == {}      # the admin who did it
    assert "Asha" not in str(dict(row)) and "asha.verma" not in str(dict(row))


def test_patch_records_which_fields_changed_but_not_their_values(client, test_url):
    donor = client.post("/v1/donors", json=make_donor()).json()
    # age is not patchable (DonorUpdate forbids it): use two fields that are, and check the PATCH really succeeded
    assert client.patch(f"/v1/donors/{donor['id']}", json={"is_available": False, "name": "Asha"}).status_code == 200
    registered, updated = audit_rows(test_url, donor["id"])
    assert registered["event"] == "donor.registered"
    assert updated["event"] == "donor.updated" and json.loads(updated["details"]) == {"fields": ["is_available", "name"]}
    assert "Asha" not in str(dict(updated))


def test_a_replay_and_a_failed_signup_leave_exactly_one_event(client, test_url):
    headers, body = {"Idempotency-Key": "audit-001"}, make_donor()
    client.post("/v1/donors", json=body, headers=headers)
    client.post("/v1/donors", json=body, headers=headers)                  # replay: nothing new happened
    assert client.post("/v1/donors", json=body).status_code == 409          # rejected: rolled back, no trace
    assert len(audit_rows(test_url)) == 1


def test_if_the_audit_event_cannot_be_written_the_change_does_not_happen(app, test_url):
    class BrokenAudit:
        async def add(self, *args, **kwargs):
            raise RuntimeError("audit store down")

    with TestClient(app, raise_server_exceptions=False) as c:
        c.headers.update(as_role(c, test_url, "admin"))      # log in first: login writes an audit event too
        app.dependency_overrides[get_audit_repo] = lambda: BrokenAudit()
        assert c.post("/v1/donors", json=make_donor()).status_code == 500
        assert c.get("/v1/donors").json()["items"] == []                   # same transaction: no donor without its event


def test_audit_rows_cannot_be_updated(test_url, fresh_db):
    asyncio.run(run_sql(
        test_url, "INSERT INTO audit_events (at, event, entity, entity_id) VALUES (now(), 'x.y', 'donor', gen_random_uuid())"
    ))
    with pytest.raises(asyncpg.PostgresError):
        asyncio.run(run_sql(test_url, "UPDATE audit_events SET event = 'tampered'"))
