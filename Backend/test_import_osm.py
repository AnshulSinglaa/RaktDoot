"""import-osm (ADR 022): real MP facilities from the OpenStreetMap snapshots, validated, idempotent, never contacted."""
import asyncio
import json
from pathlib import Path

import pytest

from conftest import _fetch, app_url, audit_rows, run_sql
from factories import make_blood_unit
from modules.organizations.osm import format_report, infer_ownership, map_snapshot, normalise_phone
from modules.organizations.service import import_osm_dir

REAL = Path(__file__).parent / "data" / "osm"
BASE = {"version": 0.6, "osm3s": {"timestamp_osm_base": "2026-10-19T18:53:36Z"}}


# ---------- AC 5: ownership ----------
@pytest.mark.parametrize("tag, name, expected", [
    ("government", "Kasturba Hospital", ("government", False)),
    ("Government", "X", ("government", False)),                        # case-insensitive
    ("public", "X", ("government", False)),
    ("private", "Civil Hospital Road Clinic", ("private", False)),     # a tag always wins over a name pattern
    ("ngo", "X", ("charitable", False)),
    ("charitable", "X", ("charitable", False)),
    ("religious", "X", ("charitable", False)),
    ("community", "X", ("charitable", False)),
    ("non_profit", "X", ("charitable", False)),
    ("doctor", "Dr Sharma Clinic", ("unknown", False)),               # unrecognised tag = untagged
    ("doctor", "Govt Hospital Sehore", ("government", True)),
    (None, "Govt. Hospital Sehore", ("government", True)),
    (None, "Government Hospital", ("government", True)),
    (None, "Goverment Hospital Rewa", ("government", True)),          # the misspelling in the snapshot
    (None, "District Hospital Blood Bank - Bhopal", ("government", True)),
    (None, "Civil Hospital Itarsi", ("government", True)),
    (None, "GMC Bhopal", ("government", True)),
    (None, "ESIC Hospital Indore", ("government", True)),
    (None, "Railway Hospital Itarsi", ("government", True)),
    (None, "Military Hospital Mhow", ("government", True)),
    (None, "CHC Budhni", ("government", True)),
    (None, "PHC Bilkisganj", ("government", True)),
    (None, "Community Health Centre Ashta", ("government", True)),
    (None, "Community Health Center Ashta", ("government", True)),
    (None, "Primary Health Centre Doraha", ("government", True)),
    (None, "Government Medical College Shahdol", ("government", True)),
    (None, "RKDF Medical College Hospital", ("unknown", False)),       # a bare "Medical College" is often private
    (None, "Chirayu Medical College", ("unknown", False)),
    (None, "Phcare Hospital", ("unknown", False)),                    # word boundaries: not PHC
    (None, "Archcare Clinic", ("unknown", False)),                    # not CHC
    ("", "Bansal Hospital", ("unknown", False)),
])
def test_infer_ownership(tag, name, expected):
    assert infer_ownership(tag, name) == expected


# ---------- AC 6: phones ----------
@pytest.mark.parametrize("raw, expected", [
    ("9827333573", "+919827333573"),
    ("07312590737", "+917312590737"),
    ("0755 277 4851", "+917552774851"),
    ("+91 98270 89853", "+919827089853"),
    ("07312343122; 07312760222", "+917312343122"),     # the first valid one
    ("07312343122, 07312760222", "+917312343122"),
    ("0731278517274", None),                           # 13 digits: malformed
    ("0731278517274; 07312760222", "+917312760222"),    # first VALID one
    ("0000000000", None),
    ("12345", None),
    ("", None),
    (None, None),
])
def test_normalise_phone(raw, expected):
    assert normalise_phone(raw) == expected


def node(i, name, lat=23.25, lon=77.41, **tags):
    return {"type": "node", "id": i, "lat": lat, "lon": lon,
            "tags": {**({"name": name} if name else {}), "addr:city": "Bhopal", **tags}}


# ---------- AC 7 + validation ----------
def test_same_name_within_200_m_is_one_facility_but_far_apart_is_two():
    rows, counts = map_snapshot([
        node(10, "Arpan Blood Bank"),                                             # no phone, lower id
        node(11, "arpan  blood BANK", lat=23.2504, phone="7554090142"),         # ~45 m away, has a phone: kept
        node(20, "Government Hospital", lat=23.0),
        node(21, "Government Hospital", lat=23.045),                             # ~5 km away: a different hospital
    ], "blood_bank")
    assert sorted(r["osm_id"] for r in rows) == ["node/11", "node/20", "node/21"]
    assert counts["dropped:duplicate"] == 1


def test_bad_rows_are_dropped_by_reason_and_a_bad_phone_keeps_the_row():
    rows, counts = map_snapshot([
        node(1, None),
        node(2, None, **{"name:en": "English Name Hospital"}),                  # name:en when there is no name
        node(3, "Lucknow Hospital", lat=26.95),                                  # outside the MP box
        node(4, "Nowhere Hospital", **{"addr:city": ""}),
        node(5, "District Only", **{"addr:city": "", "addr:district": "Sehore"}),
        node(6, "N" * 201),
        node(7, "Long City", **{"addr:city": "C" * 101}),
        node(8, "Bad Phone Hospital", phone="0731278517274", email="x@y.in", website="https://x", fax="0755",
             **{"personnel:count": "4", "operator:type": "doctor"}),
        {"type": "way", "id": 9, "center": {"lat": 22.7, "lon": 75.8}, "tags": {"name": "Way Hospital", "addr:city": "Indore"}},
    ], "hospital")
    got = {r["osm_id"]: r for r in rows}
    assert sorted(got) == ["node/2", "node/5", "node/8", "way/9"]
    assert got["node/5"]["city"] == "Sehore" and got["node/2"]["name"] == "English Name Hospital"
    assert (got["way/9"]["latitude"], got["way/9"]["longitude"]) == (22.7, 75.8)            # Overpass's center
    assert got["node/8"]["phone"] is None
    assert {k: counts[k] for k in counts if k.startswith("dropped:")} == {
        "dropped:no_name": 1, "dropped:outside_mp": 1, "dropped:no_place": 1, "dropped:name_too_long": 1,
        "dropped:city_too_long": 1}
    assert (counts["phone_invalid"], counts["operator_type_unrecognised"]) == (1, 1)
    # AC 8: minimisation. Only these keys ever reach the database.
    assert set(got["node/8"]) == {"osm_id", "kind", "name", "city", "latitude", "longitude", "phone", "ownership",
                                  "ownership_inferred"}


def test_the_importer_never_reads_contact_tags_it_does_not_need():
    source = (Path(__file__).parent / "modules" / "organizations" / "osm.py").read_text(encoding="utf-8")
    for tag in ('"email"', '"website"', '"fax"', '"contact:email"', '"personnel'):
        assert tag not in source


# ---------- AC 2, 3, 14: the real snapshots, twice ----------
def count(test_url, sql):
    return asyncio.run(_fetch(test_url, sql))[0][0]


def test_real_snapshots_import_validated_and_a_second_run_changes_nothing_the_bank_owns(test_url, fresh_db):
    first = asyncio.run(import_osm_dir(app_url(test_url), REAL))
    assert {k: (r["read"], r["kept"], r["inserted"], r["updated"]) for k, r in first.items()} == {
        "blood_bank": (119, 118, 118, 0), "hospital": (2104, 1929, 1929, 0)}
    assert first["blood_bank"]["dropped:no_place"] == 1 and first["hospital"]["dropped:duplicate"] == 4
    assert "[import-osm] blood_bank: 119 read, 118 kept (118 inserted, 0 updated)" in format_report(first)
    assert count(test_url, "SELECT count(*) FROM organizations WHERE data_source = 'osm' AND (name IS NULL OR "
                           "latitude NOT BETWEEN 21.0 AND 26.9 OR longitude NOT BETWEEN 74.0 AND 82.9 OR "
                           "phone !~ '^[+]91[1-9][0-9]{9}$' OR source_updated_at IS NULL)") == 0
    ids = asyncio.run(_fetch(test_url, "SELECT osm_id, id FROM organizations ORDER BY osm_id"))
    bank = ids[0]["id"]
    asyncio.run(run_sql(test_url, f"UPDATE organizations SET stock_confirmed_at = '2026-10-20Z', auto_confirm_holds = true "
                                  f"WHERE id = '{bank}'"))

    second = asyncio.run(import_osm_dir(app_url(test_url), REAL))
    assert {k: (r["inserted"], r["updated"], r["missing_from_snapshot"]) for k, r in second.items()} == {
        "blood_bank": (0, 118, 0), "hospital": (0, 1929, 0)}
    assert asyncio.run(_fetch(test_url, "SELECT osm_id, id FROM organizations ORDER BY osm_id")) == ids
    assert asyncio.run(_fetch(test_url, f"SELECT stock_confirmed_at, auto_confirm_holds FROM organizations "
                                        f"WHERE id = '{bank}'"))[0][1] is True
    assert count(test_url, f"SELECT count(*) FROM organizations WHERE id = '{bank}' AND stock_confirmed_at IS NULL") == 0

    events = [e for e in audit_rows(test_url) if e["event"] == "organizations.imported"]
    assert len(events) == 2                                                     # one per run
    details = json.loads(events[0]["details"])
    assert details == {"source": "osm", "inserted": 2047, "updated": 0,
                       "dropped": {"duplicate": 4, "no_name": 39, "no_place": 133}}
    text = json.dumps([dict(e) for e in events], default=str)
    assert "Blood Bank" not in text and "+91" not in text                     # counts only: no names, no phones


# ---------- AC 4: a newer snapshot renames a bank and moves a hospital, in place ----------
def write(folder: Path, banks: list, hospitals: list) -> Path:
    folder.mkdir(exist_ok=True)
    (folder / "mp_blood_banks.json").write_text(json.dumps({**BASE, "elements": banks}), encoding="utf-8")
    (folder / "mp_hospitals.json").write_text(json.dumps({**BASE, "elements": hospitals}), encoding="utf-8")
    return folder


def test_a_newer_snapshot_updates_in_place_and_holds_stay_attached(test_url, fresh_db, tmp_path, client):
    v1 = write(tmp_path / "v1", [node(1, "Old Name Blood Bank", phone="7554090142")],
               [node(2, "Moving Hospital"), node(3, "Gone Hospital", lat=23.1)])
    asyncio.run(import_osm_dir(app_url(test_url), v1))
    before = {r["osm_id"]: r["id"] for r in asyncio.run(_fetch(test_url, "SELECT osm_id, id FROM organizations"))}
    unit = client.post("/v1/blood-units", json=make_blood_unit(str(before["node/1"])))
    assert unit.status_code == 201

    v2 = write(tmp_path / "v2", [node(1, "New Name Blood Bank", phone="7554090142", **{"operator:type": "ngo"})],
               [node(2, "Moving Hospital", lat=23.30, lon=77.45)])
    report = asyncio.run(import_osm_dir(app_url(test_url), v2))
    assert report["hospital"]["missing_from_snapshot"] == 1                    # counted, never deleted
    rows = {r["osm_id"]: r for r in asyncio.run(_fetch(
        test_url, "SELECT osm_id, id, name, latitude, longitude, ownership FROM organizations"))}
    assert (rows["node/1"]["id"], rows["node/1"]["name"], rows["node/1"]["ownership"]) == (
        before["node/1"], "New Name Blood Bank", "charitable")
    assert (rows["node/2"]["id"], rows["node/2"]["latitude"], rows["node/2"]["longitude"]) == (before["node/2"], 23.30, 77.45)
    assert "node/3" in rows
    assert count(test_url, f"SELECT count(*) FROM blood_units WHERE blood_bank_id = '{before['node/1']}'") == 1


def test_an_osm_id_never_changes_kind(test_url, fresh_db, tmp_path):
    """A bank with holds must stay a bank: the same OSM element arriving as a hospital is refused and counted."""
    asyncio.run(import_osm_dir(app_url(test_url), write(tmp_path / "a", [node(1, "Blood Bank X")], [])))
    report = asyncio.run(import_osm_dir(app_url(test_url), write(tmp_path / "b", [], [node(1, "Blood Bank X")])))
    assert (report["hospital"]["kind_conflict"], report["hospital"]["updated"]) == (1, 0)
    assert count(test_url, "SELECT kind FROM organizations WHERE osm_id = 'node/1'") == "blood_bank"


# ---------- contact safety ----------
def test_organizations_have_no_email_column_so_nothing_can_email_a_facility(test_url):
    cols = {r[0] for r in asyncio.run(_fetch(
        test_url, "SELECT column_name FROM information_schema.columns WHERE table_name = 'organizations'"))}
    assert not {"email", "website", "fax"} & cols and "phone" in cols
