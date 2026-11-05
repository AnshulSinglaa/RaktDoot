import asyncio

import asyncpg
import pytest
from alembic import command
from alembic.script import ScriptDirectory

from conftest import alembic_config, run_sql


async def fetch(url: str, sql: str):
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetch(sql)
    finally:
        await conn.close()


def donor_columns(url: str) -> set[str]:
    rows = asyncio.run(fetch(url, "SELECT column_name FROM information_schema.columns WHERE table_name = 'donors'"))
    return {r["column_name"] for r in rows}


def test_db_is_at_the_newest_revision(test_url):
    cfg = alembic_config(test_url)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    assert [r["version_num"] for r in asyncio.run(fetch(test_url, "SELECT version_num FROM alembic_version"))] == [head]


def test_every_migration_can_go_down_and_back_up(test_url, fresh_db):     # empty tables: see the 0012 test
    cfg = alembic_config(test_url)
    command.downgrade(cfg, "0001")                  # back to before city: undoes 0004, 0003 and 0002
    assert "city" not in donor_columns(test_url)
    command.upgrade(cfg, "head")
    assert "city" in donor_columns(test_url)
    command.downgrade(cfg, "base")                  # undo everything: proves 0001's downgrade too
    assert donor_columns(test_url) == set()
    command.upgrade(cfg, "head")                    # leave the DB at head for every other test


def test_0011_goes_down_to_0010_and_back(test_url, fresh_db):
    """Lossy on purpose: sex, phone and phone_verified_at are dropped on the way down (ADR 020)."""
    cfg = alembic_config(test_url)
    command.downgrade(cfg, "0010")
    assert not {"sex", "phone", "phone_verified_at"} & donor_columns(test_url)
    command.upgrade(cfg, "head")
    assert {"sex", "phone", "phone_verified_at"} <= donor_columns(test_url)


def phone_index(url: str) -> str:
    return asyncio.run(fetch(url, "SELECT indexdef FROM pg_indexes WHERE indexname = 'donors_phone_uq'"))[0]["indexdef"]


def test_0012_makes_phones_unique_among_verified_donors_only_and_goes_back(test_url, fresh_db):
    cfg = alembic_config(test_url)
    assert phone_index(test_url).endswith("WHERE (phone_verified_at IS NOT NULL)")
    # a verified owner and an unverified squatter share a number: legal since 0012, so the old index cannot come back
    asyncio.run(fetch(test_url, "INSERT INTO donors (name, email, age, blood_group, phone, phone_verified_at) VALUES "
                                "('A', 'a@x.in', 30, 'O+', '+919800000000', now()), "
                                "('B', 'b@x.in', 30, 'O+', '+919800000000', NULL)"))
    with pytest.raises(Exception, match="donors_phone_uq"):
        command.downgrade(cfg, "0011")                  # fails loudly instead of deleting a donor to make it fit
    asyncio.run(fetch(test_url, "DELETE FROM donors"))
    command.downgrade(cfg, "0011")
    assert phone_index(test_url).endswith("WHERE (phone IS NOT NULL)")
    command.upgrade(cfg, "head")
    assert phone_index(test_url).endswith("WHERE (phone_verified_at IS NOT NULL)")


def test_0013_backfills_live_allocations_as_confirmed_and_refuses_to_forget_discarded_units(test_url, fresh_db):
    cfg = alembic_config(test_url)
    command.downgrade(cfg, "0012")
    asyncio.run(run_sql(test_url, """
        INSERT INTO organizations (id, kind, name, city) VALUES
          ('00000000-0000-0000-0000-00000000000b', 'blood_bank', 'B', 'X'),
          ('00000000-0000-0000-0000-0000000000a1', 'hospital', 'H', 'X');
        INSERT INTO blood_units (id, blood_bank_id, blood_group, collected_at, expires_at, status) VALUES
          ('00000000-0000-0000-0000-0000000000a2', '00000000-0000-0000-0000-00000000000b', 'O+', now(),
           now() + interval '9 days', 'RESERVED');
        INSERT INTO users (id, email, password_hash, role, organization_id) VALUES
          ('00000000-0000-0000-0000-0000000000a3', 'm@x.in', 'x', 'hospital', '00000000-0000-0000-0000-0000000000a1');
        INSERT INTO emergency_requests (id, hospital_id, created_by, blood_group, component, units_needed, urgency, status,
                                        created_at, updated_at) VALUES
          ('00000000-0000-0000-0000-0000000000a4', '00000000-0000-0000-0000-0000000000a1',
           '00000000-0000-0000-0000-0000000000a3', 'O+', 'red_cells', 1, 'HIGH', 'ALLOCATED', now(), now());
        INSERT INTO allocations (id, request_id, blood_unit_id, blood_bank_id, blood_group, allocated_at) VALUES
          (gen_random_uuid(), '00000000-0000-0000-0000-0000000000a4', '00000000-0000-0000-0000-0000000000a2',
           '00000000-0000-0000-0000-00000000000b', 'O+', now() - interval '1 hour')"""))
    command.upgrade(cfg, "0013")
    (row,) = asyncio.run(fetch(test_url, "SELECT confirmed_at = allocated_at AS confirmed, "
                                         "expires_at > now() + interval '5 hours' AS pickup_window FROM allocations"))
    assert row["confirmed"] and row["pickup_window"]                        # legacy live holds: CONFIRMED, 6 h
    asyncio.run(fetch(test_url, "UPDATE blood_units SET status = 'DISCARDED'"))
    with pytest.raises(Exception, match="blood_units_status_check"):
        command.downgrade(cfg, "0012")                  # a discarded unit must not silently become stock again
    asyncio.run(fetch(test_url, "UPDATE blood_units SET status = 'RESERVED'"))
    command.downgrade(cfg, "0012")
    assert "confirmed_at" not in {r["column_name"] for r in asyncio.run(fetch(
        test_url, "SELECT column_name FROM information_schema.columns WHERE table_name = 'allocations'"))}
    command.upgrade(cfg, "head")


def test_0014_requires_a_reason_on_every_released_hold_and_refuses_to_forget_a_bank_release(test_url, fresh_db):
    cfg = alembic_config(test_url)
    asyncio.run(run_sql(test_url, """
        INSERT INTO organizations (id, kind, name, city) VALUES
          ('00000000-0000-0000-0000-00000000000b', 'blood_bank', 'B', 'X'),
          ('00000000-0000-0000-0000-0000000000a1', 'hospital', 'H', 'X');
        INSERT INTO blood_units (id, blood_bank_id, blood_group, collected_at, expires_at, status) VALUES
          ('00000000-0000-0000-0000-0000000000a2', '00000000-0000-0000-0000-00000000000b', 'O+', now(),
           now() + interval '9 days', 'AVAILABLE');
        INSERT INTO users (id, email, password_hash, role, organization_id) VALUES
          ('00000000-0000-0000-0000-0000000000a3', 'm@x.in', 'x', 'hospital', '00000000-0000-0000-0000-0000000000a1');
        INSERT INTO emergency_requests (id, hospital_id, created_by, blood_group, component, units_needed, urgency, status,
                                        created_at, updated_at, code) VALUES
          ('00000000-0000-0000-0000-0000000000a4', '00000000-0000-0000-0000-0000000000a1',
           '00000000-0000-0000-0000-0000000000a3', 'O+', 'red_cells', 1, 'HIGH', 'OPEN', now(), now(), 'T0014AAA')"""))
    insert = ("INSERT INTO allocations (id, request_id, blood_unit_id, blood_bank_id, blood_group, allocated_at, "
              "expires_at, released_at, release_reason) VALUES (gen_random_uuid(), '00000000-0000-0000-0000-0000000000a4', "
              "'00000000-0000-0000-0000-0000000000a2', '00000000-0000-0000-0000-00000000000b', 'O+', now(), now(), {})")
    for bad in ("now(), NULL", "NULL, 'expired'"):
        with pytest.raises(Exception, match="allocations_released_has_reason"):
            asyncio.run(run_sql(test_url, insert.format(bad)))
    asyncio.run(run_sql(test_url, insert.format("now(), 'released'")))
    with pytest.raises(Exception, match="allocations_release_reason_check"):
        command.downgrade(cfg, "0013")                  # a bank's explicit release has no name before 0014
    asyncio.run(run_sql(test_url, "DELETE FROM allocations"))
    command.downgrade(cfg, "0013")
    asyncio.run(run_sql(test_url, insert.format("now(), NULL")))           # legal again before 0014...
    with pytest.raises(Exception, match="allocations_released_has_reason"):
        command.upgrade(cfg, "head")                    # ...so the upgrade validates existing rows and refuses
    asyncio.run(run_sql(test_url, "DELETE FROM allocations"))
    command.upgrade(cfg, "head")


def test_0015_backfills_handed_over_units_as_collected_and_the_check_holds(test_url, fresh_db):
    """Backfill: live holds of FULFILLED requests are collected at the close; released ones and open requests are not.
    The CHECK refuses a hold both collected and released (the F1 phantom-stock bug, at the database)."""
    cfg = alembic_config(test_url)
    command.downgrade(cfg, "0014")
    asyncio.run(run_sql(test_url, """
        INSERT INTO organizations (id, kind, name, city) VALUES
          ('00000000-0000-0000-0000-00000000000b', 'blood_bank', 'B', 'X'),
          ('00000000-0000-0000-0000-0000000000a1', 'hospital', 'H', 'X');
        INSERT INTO blood_units (id, blood_bank_id, blood_group, collected_at, expires_at, status)
          SELECT ('00000000-0000-0000-0000-0000000000c' || i)::uuid, '00000000-0000-0000-0000-00000000000b', 'O+', now(),
                 now() + interval '9 days', 'ISSUED' FROM generate_series(1, 3) i;
        INSERT INTO users (id, email, password_hash, role, organization_id) VALUES
          ('00000000-0000-0000-0000-0000000000a3', 'm@x.in', 'x', 'hospital', '00000000-0000-0000-0000-0000000000a1');
        INSERT INTO emergency_requests (id, hospital_id, created_by, blood_group, component, units_needed, urgency, status,
                                        created_at, updated_at) VALUES
          ('00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-0000000000a1',
           '00000000-0000-0000-0000-0000000000a3', 'O+', 'red_cells', 2, 'HIGH', 'FULFILLED', now(), '2026-10-01Z'),
          ('00000000-0000-0000-0000-0000000000f2', '00000000-0000-0000-0000-0000000000a1',
           '00000000-0000-0000-0000-0000000000a3', 'O+', 'red_cells', 1, 'HIGH', 'ALLOCATED', now(), now());
        INSERT INTO allocations (id, request_id, blood_unit_id, blood_bank_id, blood_group, allocated_at, expires_at,
                                 confirmed_at, released_at, release_reason) VALUES
          (gen_random_uuid(), '00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-0000000000c1',
           '00000000-0000-0000-0000-00000000000b', 'O+', now(), now(), NULL, NULL, NULL),
          (gen_random_uuid(), '00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-0000000000c2',
           '00000000-0000-0000-0000-00000000000b', 'O+', now(), now(), NULL, now(), 'expired'),
          (gen_random_uuid(), '00000000-0000-0000-0000-0000000000f2', '00000000-0000-0000-0000-0000000000c3',
           '00000000-0000-0000-0000-00000000000b', 'O+', now(), now(), now(), NULL, NULL)"""))
    command.upgrade(cfg, "0015")
    rows = asyncio.run(fetch(test_url, "SELECT blood_unit_id::text AS u, collected_at, confirmed_at FROM allocations"))
    got = {r["u"][-2:]: (r["collected_at"], r["confirmed_at"]) for r in rows}
    assert got["c1"][0] is not None and got["c1"][0] == got["c1"][1] and got["c1"][0].isoformat().startswith("2026-10-01")
    assert got["c2"][0] is None and got["c2"][1] is None                  # released: never marked collected
    assert got["c3"][0] is None                                           # open request: still a normal hold
    with pytest.raises(Exception, match="allocations_collected_is_live"):
        asyncio.run(run_sql(test_url, "UPDATE allocations SET collected_at = now(), confirmed_at = now() "
                                      "WHERE released_at IS NOT NULL"))
    with pytest.raises(Exception, match="allocations_collected_is_live"):
        asyncio.run(run_sql(test_url, "UPDATE allocations SET released_at = now(), release_reason = 'cancelled' "
                                      "WHERE collected_at IS NOT NULL"))
    command.downgrade(cfg, "0014")
    assert "collected_at" not in {r["column_name"] for r in asyncio.run(fetch(
        test_url, "SELECT column_name FROM information_schema.columns WHERE table_name = 'allocations'"))}
    command.upgrade(cfg, "head")


def test_0016_backfills_synthetic_rows_and_its_checks_refuse_bad_provenance(test_url, fresh_db):
    """AC 1: down and up on a DB with data; the CHECKs refuse an unknown ownership, an OSM row without an id, and
    'inferred' on an unknown owner; osm_id is unique where present."""
    cfg = alembic_config(test_url)
    command.downgrade(cfg, "0015")
    asyncio.run(run_sql(test_url, "INSERT INTO organizations (kind, name, city) VALUES "
                                  "('blood_bank', 'Bhopal Blood Centre 1 (demo)', 'Bhopal'), ('hospital', 'H', 'X')"))
    command.upgrade(cfg, "0016")
    rows = asyncio.run(fetch(test_url, "SELECT name, data_source, ownership, ownership_inferred FROM organizations"))
    assert {r["name"]: (r["data_source"], r["ownership"], r["ownership_inferred"]) for r in rows} == {
        "Bhopal Blood Centre 1 (demo)": ("synthetic", "unknown", False), "H": ("manual", "unknown", False)}
    insert = "INSERT INTO organizations (kind, name, city, {}) VALUES ('hospital', 'Y', 'Z', {})"
    for cols, values, check in (("ownership", "'state'", "ownership_check"),
                                ("data_source", "'osm'", "organizations_osm_has_id"),
                                ("ownership_inferred", "true", "organizations_inferred_is_known"),
                                ("osm_id", "'node/x'", "osm_id_check")):
        with pytest.raises(asyncpg.exceptions.CheckViolationError, match=check):
            asyncio.run(run_sql(test_url, insert.format(cols, values)))
    asyncio.run(run_sql(test_url, insert.format("osm_id, data_source", "'node/1', 'osm'")))
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        asyncio.run(run_sql(test_url, insert.format("osm_id, data_source", "'node/1', 'osm'")))
    command.downgrade(cfg, "0015")
    assert "ownership" not in {r["column_name"] for r in asyncio.run(fetch(
        test_url, "SELECT column_name FROM information_schema.columns WHERE table_name = 'organizations'"))}
    command.upgrade(cfg, "head")


def test_0016_rollback_then_reimport_matches_existing_facilities_instead_of_duplicating(test_url, fresh_db):
    """QA 2r: the documented rollback (downgrade 0015, upgrade head, import-osm) must not double every facility."""
    from conftest import app_url
    from modules.organizations.service import import_osm_dir
    from test_import_osm import REAL
    snapshot = "SELECT id, osm_id, data_source FROM organizations ORDER BY osm_id"
    asyncio.run(import_osm_dir(app_url(test_url), REAL))
    before = asyncio.run(fetch(test_url, snapshot))
    assert len(before) == 2047
    cfg = alembic_config(test_url)
    command.downgrade(cfg, "0015")
    cols = {r["column_name"] for r in asyncio.run(fetch(
        test_url, "SELECT column_name FROM information_schema.columns WHERE table_name = 'organizations'"))}
    assert "osm_id" in cols and not {"data_source", "ownership", "ownership_inferred", "source_updated_at"} & cols
    command.upgrade(cfg, "head")
    assert len(asyncio.run(fetch(test_url, "SELECT 1 FROM pg_constraint WHERE conname LIKE 'organizations_osm_id_check%'"
                                           ))) == 1      # IF NOT EXISTS did not stack a second CHECK
    report = asyncio.run(import_osm_dir(app_url(test_url), REAL))
    assert sum(r["inserted"] for r in report.values()) == 0
    assert asyncio.run(fetch(test_url, snapshot)) == before            # same rows, same ids, labels re-derived to 'osm'


def test_0017_backfills_unique_codes_on_existing_requests_and_goes_back(test_url, fresh_db):
    """AC 13: upgrade / downgrade / upgrade on a DB with requests; old requests get distinct 8-character codes."""
    cfg = alembic_config(test_url)
    command.downgrade(cfg, "0016")
    asyncio.run(run_sql(test_url, """
        INSERT INTO organizations (id, kind, name, city) VALUES ('00000000-0000-0000-0000-0000000000a1', 'hospital', 'H', 'X');
        INSERT INTO users (id, email, password_hash, role, organization_id) VALUES
          ('00000000-0000-0000-0000-0000000000a3', 'm@x.in', 'x', 'hospital', '00000000-0000-0000-0000-0000000000a1');
        INSERT INTO emergency_requests (id, hospital_id, created_by, blood_group, component, units_needed, urgency,
                                        status, created_at, updated_at)
        SELECT gen_random_uuid(), '00000000-0000-0000-0000-0000000000a1', '00000000-0000-0000-0000-0000000000a3',
               'O+', 'red_cells', 1, 'HIGH', 'OPEN', now(), now() FROM generate_series(1, 50)"""))
    command.upgrade(cfg, "0017")
    codes = [r["code"] for r in asyncio.run(fetch(test_url, "SELECT code FROM emergency_requests"))]
    assert len(codes) == 50 == len(set(codes)) and all(len(c) == 8 and c == c.upper() for c in codes)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        asyncio.run(run_sql(test_url, f"UPDATE emergency_requests SET hospital_ref = '{'x' * 31}'"))
    with pytest.raises(asyncpg.exceptions.NotNullViolationError):
        asyncio.run(run_sql(test_url, "UPDATE emergency_requests SET code = NULL"))
    command.downgrade(cfg, "0016")
    cols = {r["column_name"] for r in asyncio.run(fetch(
        test_url, "SELECT column_name FROM information_schema.columns WHERE table_name = 'emergency_requests'"))}
    assert not {"code", "hospital_ref"} & cols
    command.upgrade(cfg, "head")
