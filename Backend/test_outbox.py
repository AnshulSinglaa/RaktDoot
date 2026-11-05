"""The transactional outbox and its worker: notifications are never lost, never sent for a rolled-back request,
retried with backoff, dead-lettered when hopeless, and never duplicated by two workers."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import _fetch, app_url, as_role, run_sql
from db import make_engine, make_sessionmaker
from factories import make_donor, make_organization
from main import get_audit_repo
from modules.emergencies.service import HANDLERS
from worker import MAX_ATTEMPTS, backoff, process_batch, run_loop

AIIMS = {"latitude": 23.2080, "longitude": 77.4600}


class Recorder:
    def __init__(self, fail: bool = False):
        self.sent, self.fail = [], fail

    async def send(self, donor_id, request_id):
        if self.fail:
            raise ConnectionError("SMS provider down")
        self.sent.append((str(donor_id), str(request_id)))


def drain(test_url, notifier, now=None, workers=1, handlers=HANDLERS):
    """Run `workers` worker passes CONCURRENTLY against the test database, as the app role."""
    async def go():
        engine = make_engine(app_url(test_url), 5, 0)
        try:
            sm = make_sessionmaker(engine)
            when = now or datetime.now(timezone.utc)
            return await asyncio.gather(*[process_batch(sm, handlers, notifier, when) for _ in range(workers)])
        finally:
            await engine.dispose()
    return asyncio.run(go())


def outbox(test_url):
    return asyncio.run(_fetch(
        test_url, "SELECT topic, attempts, processed_at, dead_at, available_at, last_error FROM outbox ORDER BY id"
    ))


@pytest.fixture
def scene(client, test_url):
    """A hospital with an OPEN O+ request, and three compatible donors nearby (one with a login)."""
    hospital = client.post("/v1/organizations", json=make_organization(kind="hospital", **AIIMS)).json()["id"]
    staff = as_role(client, test_url, "hospital", hospital)
    donor_login = as_role(client, test_url, "donor")
    body = make_donor(blood_group="O-", latitude=23.21, longitude=77.46)
    me = client.post("/v1/donors", json=body, headers=donor_login).json()
    # self-signup is unverified until staff see the donor and the number
    assert client.post(f"/v1/donors/{me['id']}/verify-phone", json={"phone": body["phone"]}).status_code == 200
    others = [client.post("/v1/donors", json=make_donor(blood_group="O+", latitude=23.21, longitude=77.47)).json()["id"]
              for _ in range(2)]
    req = client.post("/v1/emergencies", json={"blood_group": "O+", "units_needed": 1}, headers=staff).json()
    return {"staff": staff, "donor": donor_login, "me": me["id"], "donors": {me["id"], *others}, "req": req["id"]}


def notify(client, scene):
    return client.post(f"/v1/emergencies/{scene['req']}/notify-donors", headers=scene["staff"])


def test_notify_queues_messages_and_only_the_worker_sends_them(client, scene, test_url):
    r = notify(client, scene)
    assert r.status_code == 202 and r.json() == {"queued": 3}
    assert len(outbox(test_url)) == 3                             # written in the request's transaction...
    rec = Recorder()
    assert drain(test_url, rec) == [3]                            # ...delivered by the worker
    assert {d for d, _ in rec.sent} == scene["donors"]
    assert all(row["processed_at"] for row in outbox(test_url))
    assert drain(test_url, rec) == [0] and len(rec.sent) == 3     # nothing due any more


def test_notifying_twice_reaches_each_donor_once(client, scene, test_url):
    notify(client, scene)
    notify(client, scene)                                          # double click: 6 messages for 3 donors
    rec = Recorder()
    drain(test_url, rec)
    assert len(rec.sent) == 3                                      # idempotent handler: (request, donor) is a key


def test_a_failed_send_is_retried_with_backoff_and_leaves_no_trace(client, scene, test_url):
    notify(client, scene)
    now = datetime.now(timezone.utc)
    drain(test_url, Recorder(fail=True), now=now)
    rows = outbox(test_url)
    assert all(r["attempts"] == 1 and r["processed_at"] is None for r in rows)
    assert all(r["available_at"] >= now + timedelta(seconds=2) for r in rows)        # backoff: not retried at once
    assert "SMS provider down" in rows[0]["last_error"]
    assert asyncio.run(_fetch(test_url, "SELECT count(*) FROM donor_responses"))[0][0] == 0   # rolled back with the send
    assert drain(test_url, Recorder(), now=now) == [0]                                # not due yet
    rec = Recorder()
    drain(test_url, rec, now=now + timedelta(seconds=3))
    assert len(rec.sent) == 3


def test_a_hopeless_message_is_dead_lettered(client, scene, test_url):
    notify(client, scene)
    when = datetime.now(timezone.utc)
    for _ in range(MAX_ATTEMPTS):
        when += timedelta(minutes=10)
        drain(test_url, Recorder(fail=True), now=when)
    assert all(r["dead_at"] is not None for r in outbox(test_url))
    assert drain(test_url, Recorder(), now=when + timedelta(days=1)) == [0]          # a human looks at it now


def test_retries_reach_the_backoff_cap_and_outlast_a_short_outage():
    """B7: with 5 attempts a 35 s SMS outage dead-lettered donor alerts. Now: retries reach the 5-minute cap."""
    waits = [backoff(a) for a in range(1, MAX_ATTEMPTS)]          # the wait after each failed attempt but the last
    assert waits[-1] == timedelta(minutes=5)
    assert timedelta(minutes=20) < sum(waits, timedelta()) < timedelta(minutes=40)


def test_the_worker_rechecks_the_donor_at_send_time(client, scene, test_url):
    """B8: a donor who withdrew consent or opted out AFTER the hospital clicked is skipped, silently and for good."""
    notify(client, scene)
    away = sorted(scene["donors"] - {scene["me"]})[0]
    asyncio.run(_fetch(test_url, "UPDATE donors SET consent_at = NULL WHERE id = $1::uuid", scene["me"]))
    asyncio.run(_fetch(test_url, "UPDATE donors SET is_available = false WHERE id = $1::uuid", away))
    rec = Recorder()
    assert drain(test_url, rec) == [3]
    assert {d for d, _ in rec.sent} == scene["donors"] - {scene["me"], away}
    assert all(r["processed_at"] and r["attempts"] == 1 for r in outbox(test_url))   # skipped = done, not retried
    assert asyncio.run(_fetch(test_url, "SELECT count(*) FROM donor_responses"))[0][0] == 1   # they cannot answer


def test_one_poison_message_does_not_block_the_others(client, scene, test_url):
    asyncio.run(run_sql(test_url, "INSERT INTO outbox (topic, payload, created_at, available_at) "
                                  "VALUES ('no.such.topic', '{}', now(), now())"))
    notify(client, scene)
    rec = Recorder()
    drain(test_url, rec)
    assert len(rec.sent) == 3                                       # SAVEPOINT per message
    assert [r["processed_at"] is None for r in outbox(test_url)] == [True, False, False, False]


def test_two_workers_never_send_the_same_message(client, scene, test_url):
    notify(client, scene)
    rec = Recorder()
    taken = drain(test_url, rec, workers=2)                         # FOR UPDATE SKIP LOCKED
    assert sum(taken) == 3 and len(rec.sent) == 3


def test_a_rolled_back_request_leaves_no_message(app, test_url):
    class BrokenAudit:
        async def add(self, *a, **k):
            raise RuntimeError("audit down")

    with TestClient(app, raise_server_exceptions=False) as c:
        hospital = c.post("/v1/organizations", json=make_organization(kind="hospital", **AIIMS),
                          headers=as_role(c, test_url, "admin")).json()["id"]
        staff = as_role(c, test_url, "hospital", hospital)
        c.post("/v1/donors", json=make_donor(latitude=23.21, longitude=77.46), headers=as_role(c, test_url, "admin"))
        req = c.post("/v1/emergencies", json={"blood_group": "O+", "units_needed": 1}, headers=staff).json()
        app.dependency_overrides[get_audit_repo] = lambda: BrokenAudit()
        assert c.post(f"/v1/emergencies/{req['id']}/notify-donors", headers=staff).status_code == 500
    assert outbox(test_url) == []                                   # same transaction: no message without the request


def test_a_cancelled_request_does_not_bother_donors(client, scene, test_url):
    notify(client, scene)
    client.post(f"/v1/emergencies/{scene['req']}/cancel", json={"collected_from": []}, headers=scene["staff"])
    rec = Recorder()
    drain(test_url, rec)
    assert rec.sent == [] and all(r["processed_at"] for r in outbox(test_url))


def test_a_notified_donor_answers_and_the_hospital_sees_it(client, scene, test_url):
    notify(client, scene)
    drain(test_url, Recorder())
    inbox = client.get("/v1/emergencies/donor-inbox", headers=scene["donor"]).json()
    assert [(i["request_id"], i["my_status"]) for i in inbox] == [(scene["req"], "NOTIFIED")]
    r = client.post(f"/v1/emergencies/{scene['req']}/responses", json={"accept": True}, headers=scene["donor"])
    assert r.status_code == 200 and r.json()["my_status"] == "ACCEPTED"
    answers = client.get(f"/v1/emergencies/{scene['req']}/responses", headers=scene["staff"]).json()
    mine = [a for a in answers if a["donor_id"] == scene["me"]]
    assert mine[0]["status"] == "ACCEPTED" and set(mine[0]) == {"donor_id", "first_name", "blood_group", "status", "responded_at"}


def test_a_donor_who_was_never_asked_cannot_answer(client, scene, test_url):
    stranger = as_role(client, test_url, "donor")
    client.post("/v1/donors", json=make_donor(), headers=stranger)
    r = client.post(f"/v1/emergencies/{scene['req']}/responses", json={"accept": True}, headers=stranger)
    assert r.status_code == 404
    assert client.get("/v1/emergencies/donor-inbox", headers=stranger).json() == []


def test_a_failing_pass_never_ends_the_worker_loop(client, scene, test_url):
    """Postgres restarting = the session cannot even open, and the expiry sweep raises every time. The loop logs, backs
    off and keeps going, so the queued alerts still go out once the database is back."""
    notify(client, scene)

    async def go():
        engine = make_engine(app_url(test_url), 2, 0)
        real, opened, sweeps, stop = make_sessionmaker(engine), [], [], asyncio.Event()

        def flaky():
            opened.append(1)
            if len(opened) == 1:
                raise ConnectionRefusedError("the database system is starting up")
            return real()

        async def broken_expiry(sm, now):
            sweeps.append(now)
            raise RuntimeError("expiry pass blew up")

        class StopAfterThree(Recorder):
            async def send(self, donor_id, request_id):
                await super().send(donor_id, request_id)
                if len(self.sent) == 3:
                    stop.set()

        rec = StopAfterThree()
        try:
            await asyncio.wait_for(run_loop(flaky, HANDLERS, rec, stop, broken_expiry, expiry_every=0,
                                            poll_seconds=0.01), 15)
        finally:
            await engine.dispose()
        return rec, len(opened), len(sweeps)

    rec, opened, sweeps = asyncio.run(go())
    assert {d for d, _ in rec.sent} == scene["donors"]              # delivered after the failed pass
    assert opened >= 2 and sweeps >= 2                              # it came back for more after both kinds of failure


def test_a_donor_answering_an_old_request_gets_its_item_not_a_500(client, scene, test_url):
    """The inbox shows the newest 20; the reply must build its own item, whatever its age."""
    notify(client, scene)
    drain(test_url, Recorder())
    asyncio.run(run_sql(test_url, f"""
        INSERT INTO emergency_requests (id, hospital_id, created_by, blood_group, component, units_needed, urgency,
                                        status, created_at, updated_at, code)
        SELECT gen_random_uuid(), hospital_id, created_by, 'O+', 'red_cells', 1, 'HIGH', 'OPEN', now(), now(),
               upper(substr(md5(random()::text), 1, 8))
        FROM emergency_requests, generate_series(1, 21);
        INSERT INTO donor_responses (request_id, donor_id, status, notified_at)
        SELECT id, '{scene["me"]}', 'NOTIFIED', now() + interval '1 minute' FROM emergency_requests
        WHERE id <> '{scene["req"]}'"""))
    assert scene["req"] not in [i["request_id"] for i in client.get("/v1/emergencies/donor-inbox",
                                                                     headers=scene["donor"]).json()]
    r = client.post(f"/v1/emergencies/{scene['req']}/responses", json={"accept": True}, headers=scene["donor"])
    assert r.status_code == 200 and (r.json()["request_id"], r.json()["my_status"]) == (scene["req"], "ACCEPTED")
