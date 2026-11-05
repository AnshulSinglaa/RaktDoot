"""Live status over SSE, fed by Postgres LISTEN/NOTIFY."""
import asyncio
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import jwt
import pytest
import uvicorn

from conftest import TEST_SECRET, _fetch, app_url, as_role, make_user
from db import make_engine, make_sessionmaker
from events import CHANNEL, EventHub, publish
from factories import make_blood_unit, make_organization
from security import make_access_token

AIIMS = {"latitude": 23.2080, "longitude": 77.4600}
HAMIDIA = {"latitude": 23.2599, "longitude": 77.3920}


def setup_request(client, test_url, stock=1):
    hospital = client.post("/v1/organizations", json=make_organization(kind="hospital", **AIIMS)).json()["id"]
    bank = client.post("/v1/organizations", json=make_organization(kind="blood_bank", **HAMIDIA)).json()["id"]
    make_user(test_url, "blood_bank", bank)                    # onboarded bank: only those are held (D88)
    for _ in range(stock):
        client.post("/v1/blood-units", json=make_blood_unit(bank))
    staff = as_role(client, test_url, "hospital", hospital)
    req = client.post("/v1/emergencies", json={"blood_group": "O+", "units_needed": 1}, headers=staff).json()
    return staff, req["id"], bank


def snapshots(response):
    """Yield the decoded data of each `event: snapshot` frame."""
    event = None
    for line in response.iter_lines():
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event == "snapshot":
            yield json.loads(line[6:])


@pytest.fixture
def live_server(app):
    """A REAL uvicorn on a free port. TestClient buffers a streaming body until the app finishes, so it can never
    watch an open SSE stream; a real server and a real HTTP client can."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as c:
        yield c
    server.should_exit = True
    thread.join(10)


def test_the_stream_starts_with_the_full_state_and_follows_every_change(app, live_server, test_url):
    admin = as_role(live_server, test_url, "admin")
    live_server.headers.update(admin)
    staff, rid, _ = setup_request(live_server, test_url)
    seen, first_seen = [], threading.Event()

    def watch():
        with live_server.stream("GET", f"/v1/emergencies/{rid}/events", headers=staff) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            for snap in snapshots(r):
                seen.append(snap["request"]["status"])
                first_seen.set()
            # the server ended the stream by itself: a CANCELLED request has nothing more to say

    with ThreadPoolExecutor(1) as pool:
        watching = pool.submit(watch)
        assert first_seen.wait(10)
        assert app.state.engine.pool.checkedout() == 0             # an open stream holds NO pooled connection
        assert app.state.events.viewers() == 1
        live_server.post(f"/v1/emergencies/{rid}/cancel", json={"collected_from": []}, headers=staff)
        watching.result(timeout=10)
    assert seen == ["ALLOCATED", "CANCELLED"]
    assert app.state.events.viewers() == 0                         # unsubscribed when the stream ended


def test_the_stream_shows_each_banks_hold_and_its_confirmation_live(app, live_server, test_url):
    """ADR 021: the snapshot carries per-bank holds; a bank's confirm reaches the hospital's screen without a reload."""
    live_server.headers.update(as_role(live_server, test_url, "admin"))
    staff, rid, bank = setup_request(live_server, test_url)
    bank_staff = as_role(live_server, test_url, "blood_bank", bank)
    seen, changed = [], threading.Event()

    def watch():
        with live_server.stream("GET", f"/v1/emergencies/{rid}/events", headers=staff) as r:
            for snap in snapshots(r):
                seen.append([(h["status"], h["units"], h["bank_name"] != "") for h in snap["request"]["holds"]])
                changed.set()

    with ThreadPoolExecutor(1) as pool:
        watching = pool.submit(watch)
        assert changed.wait(10)
        changed.clear()
        assert live_server.post(f"/v1/emergencies/{rid}/holds/confirm", headers=bank_staff).status_code == 200
        assert changed.wait(10)
        live_server.post(f"/v1/emergencies/{rid}/cancel", json={"collected_from": []}, headers=staff)
        watching.result(timeout=10)
    assert seen == [[("PENDING", 1, True)], [("CONFIRMED", 1, True)], [("CANCELLED", 1, True)]]


def test_the_stream_shows_a_collected_hold_as_issued_live(app, live_server, test_url):
    """ADR 021 rev 2: "collected" reaches the hospital's screen as ISSUED, with units_collected, without a reload."""
    live_server.headers.update(as_role(live_server, test_url, "admin"))
    staff, rid, bank = setup_request(live_server, test_url)
    seen, changed = [], threading.Event()

    def watch():
        with live_server.stream("GET", f"/v1/emergencies/{rid}/events", headers=staff) as r:
            for snap in snapshots(r):
                seen.append((snap["request"]["units_collected"], [h["status"] for h in snap["request"]["holds"]]))
                changed.set()

    with ThreadPoolExecutor(1) as pool:
        watching = pool.submit(watch)
        assert changed.wait(10)
        changed.clear()
        assert live_server.post(f"/v1/emergencies/{rid}/holds/{bank}/collected", headers=staff).status_code == 200
        assert changed.wait(10)
        live_server.post(f"/v1/emergencies/{rid}/fulfil", json={"collected_from": []}, headers=staff)
        watching.result(timeout=10)
    assert seen == [(0, ["PENDING"]), (1, ["ISSUED"]), (1, ["ISSUED"])]


def test_only_the_owning_hospital_may_watch(client, test_url):
    _, rid, _ = setup_request(client, test_url)
    assert client.get(f"/v1/emergencies/{rid}/events", headers=as_role(client, test_url, "hospital")).status_code == 404
    assert client.get(f"/v1/emergencies/{rid}/events", headers=as_role(client, test_url, "donor")).status_code == 403
    assert client.get(f"/v1/emergencies/{rid}/events", headers={"Authorization": ""}).status_code == 401


def test_a_closed_request_streams_one_snapshot_and_ends(client, test_url):
    staff, rid, bank = setup_request(client, test_url)
    client.post(f"/v1/emergencies/{rid}/holds/confirm", headers=as_role(client, test_url, "blood_bank", bank))
    assert client.post(f"/v1/emergencies/{rid}/fulfil", json={"collected_from": [bank]}, headers=staff).status_code == 200
    with client.stream("GET", f"/v1/emergencies/{rid}/events", headers=staff) as r:
        assert [s["request"]["status"] for s in snapshots(r)] == ["FULFILLED"]


def test_a_stream_ends_when_its_token_expires(client, test_url):
    """B11: authorised at connect only, so the stream ends at the token's exp (here ~2 s away) even though the request
    is still live; the client reconnects with a fresh token. TestClient returns once the stream has ended."""
    staff, rid, _ = setup_request(client, test_url)             # ALLOCATED: nothing but the token can end this stream
    claims = jwt.decode(staff["Authorization"][7:], options={"verify_signature": False})
    issued = datetime.now(timezone.utc) - timedelta(minutes=15) + timedelta(seconds=2)
    short = make_access_token(UUID(claims["sub"]), "hospital", UUID(claims["org"]), issued, 15, TEST_SECRET)
    started = time.monotonic()
    with client.stream("GET", f"/v1/emergencies/{rid}/events", headers={"Authorization": f"Bearer {short}"}) as r:
        assert [s["request"]["status"] for s in snapshots(r)] == ["ALLOCATED"]
    assert time.monotonic() - started < 10                      # ended at exp, not at a 15 s heartbeat or never


def test_events_are_broadcast_only_when_the_transaction_commits(test_url, fresh_db):
    """pg_notify is transactional: a rolled-back change must never reach a screen."""
    async def go():
        hub, engine = EventHub(), make_engine(app_url(test_url), 2, 0)
        await hub.start(app_url(test_url))
        try:
            with hub.subscribe("r-1") as queue:
                async with make_sessionmaker(engine)() as s:
                    async with s.begin():
                        await publish(s, "r-1", "ghost")
                        await s.rollback()
                    async with s.begin():
                        await publish(s, "r-1", "real")
                event = await asyncio.wait_for(queue.get(), 5)
                await asyncio.sleep(0.2)
                return event, queue.qsize()
        finally:
            await hub.stop()
            await engine.dispose()

    event, left = asyncio.run(go())
    assert event == {"request_id": "r-1", "kind": "real"} and left == 0


def test_live_updates_survive_a_killed_listen_connection(test_url, fresh_db):
    """A Postgres restart (simulated: pg_terminate_backend on the LISTEN connection) must not silence the hub.
    It reconnects by itself, nudges every viewer once (events sent while it was down are gone), and later
    publishes arrive again."""
    async def go():
        hub, engine = EventHub(retry_seconds=0.05), make_engine(app_url(test_url), 2, 0)
        await hub.start(app_url(test_url))
        try:
            with hub.subscribe("r-3") as queue:
                old_pid = hub._conn.get_server_pid()
                assert (await _fetch(test_url, "SELECT pg_terminate_backend($1)", old_pid))[0][0] is True
                nudge = await asyncio.wait_for(queue.get(), 10)
                assert hub._conn.get_server_pid() != old_pid
                async with make_sessionmaker(engine)() as s, s.begin():
                    await publish(s, "r-3", "after-restart")
                return nudge, await asyncio.wait_for(queue.get(), 5)
        finally:
            await hub.stop()
            await engine.dispose()

    nudge, event = asyncio.run(go())
    assert nudge == {"request_id": "r-3", "kind": "reconnected"}
    assert event == {"request_id": "r-3", "kind": "after-restart"}


def test_a_slow_viewer_drops_nudges_instead_of_blocking_the_listener():
    hub = EventHub()
    with hub.subscribe("r-2") as queue:
        for _ in range(100):                                       # far more than the queue holds
            hub._on_notify(None, 0, CHANNEL, json.dumps({"request_id": "r-2", "kind": "x"}))
        assert queue.full() and hub.viewers() == 1
    assert hub.viewers() == 0                                      # unsubscribed on exit
