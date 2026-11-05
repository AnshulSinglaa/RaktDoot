"""Anonymous search for a patient's family: nearest banks with stock, and how many donors are near (counts only)."""
import asyncio
import re

from conftest import run_sql
from factories import make_blood_unit, make_donor, make_organization

SEHORE = {"latitude": 23.2032, "longitude": 77.0844}
BHOPAL_BANK = {"latitude": 23.2599, "longitude": 77.3920}      # ~32 km from Sehore
INDORE_BANK = {"latitude": 22.7196, "longitude": 75.8577}      # ~135 km


def find(client, path, **params):
    # anonymous: no Authorization header at all
    return client.get(f"/v1/public/{path}", params={**SEHORE, **params}, headers={"Authorization": ""})


def test_nearest_banks_with_compatible_stock_nearest_first(client):
    near = client.post("/v1/organizations", json=make_organization(kind="blood_bank", **BHOPAL_BANK)).json()["id"]
    far = client.post("/v1/organizations", json=make_organization(kind="blood_bank", **INDORE_BANK)).json()["id"]
    empty_bank = make_organization(kind="blood_bank", latitude=23.21, longitude=77.10)
    empty = client.post("/v1/organizations", json=empty_bank).json()["id"]
    for bank, group in ((near, "O-"), (near, "O+"), (far, "O-"), (empty, "AB+")):     # empty: only incompatible stock
        client.post("/v1/blood-units", json=make_blood_unit(bank, blood_group=group))
    got = find(client, "nearest-banks", recipient="O+").json()
    assert [(b["blood_bank_id"], b["units"], b["blood_groups"]) for b in got] == [(near, 2, ["O+", "O-"])]
    assert 25 < got[0]["distance_km"] < 40                         # real distance Sehore -> Bhopal
    assert len(find(client, "nearest-banks", recipient="O+", radius_km=100).json()) == 1      # Indore is still too far
    assert find(client, "nearest-banks", recipient="O+", radius_km=1001).status_code == 422    # radius capped


def test_donors_are_only_counted_per_band_and_small_counts_are_hidden(client):
    for _ in range(6):
        client.post("/v1/donors", json=make_donor(blood_group="O-", **BHOPAL_BANK))      # 25-50 km band
    for _ in range(2):
        client.post("/v1/donors", json=make_donor(blood_group="O+", latitude=23.21, longitude=77.10))   # under 2 km
    client.post("/v1/donors", json=make_donor(blood_group="AB+", latitude=23.21, longitude=77.10))     # incompatible
    client.post("/v1/donors", json=make_donor(blood_group="O+", latitude=23.21, longitude=77.10,
                                              last_donated_at="2026-10-01T00:00:00Z"))                # not eligible yet
    body = find(client, "donors-nearby", recipient="O+").json()
    bands = {b["band"]: b["donors"] for b in body["bands"]}
    assert bands["25-50 km"] == 6
    assert bands["under 2 km"] is None          # 2 real donors: too few to show, could identify a neighbour
    assert bands["5-10 km"] is None             # 0 is hidden like 1-4: "none" vs "a few" is a signal too
    assert body["total"] is None and "hospital" in body["how_to_reach_them"]
    # B1: total - visible bands must never reveal a hidden band, so any hidden band hides the total
    assert None not in bands.values() or body["total"] is None
    text = str(body)
    assert set(body) == {"total", "bands", "how_to_reach_them"} and all(set(b) == {"band", "donors"} for b in body["bands"])
    assert "@example.com" not in text and not re.search(r"Donor d", text)                 # no names, no emails


def test_a_tiny_total_is_hidden_too(client):
    client.post("/v1/donors", json=make_donor(blood_group="O-", latitude=23.21, longitude=77.10))
    assert find(client, "donors-nearby", recipient="O-").json()["total"] is None


def test_the_callers_location_is_coarsened_so_streets_cannot_be_probed(client):
    for _ in range(5):
        client.post("/v1/donors", json=make_donor(blood_group="O-", latitude=23.25, longitude=77.10))
    a = find(client, "donors-nearby", recipient="O-", latitude=23.2111, longitude=77.0811).json()
    b = find(client, "donors-nearby", recipient="O-", latitude=23.2399, longitude=77.1199).json()
    assert a == b                                # both snap to 23.2, 77.1: moving ~3 km reveals nothing


def test_public_search_is_a_token_bucket_burst_20_then_60_per_minute(client, monkeypatch):
    """QA 2r. Break-it (run): put the old fixed 30/min window back and the 21st search gets 200, not 429; the
    sustained test below then counts ~60 instead of ~79 (the window ignores the burst and refills all at once)."""
    clock = [5000.0]
    monkeypatch.setattr("deps.time.monotonic", lambda: clock[0])
    search = lambda: find(client, "nearest-banks", recipient="O+")             # noqa: E731
    assert [search().status_code for _ in range(20)] == [200] * 20             # a family (or a ward) clicking fast
    blocked = search()
    assert blocked.status_code == 429 and blocked.headers["Retry-After"] == "1"   # 1 token refills in 1 s at 60/min
    assert blocked.json()["type"].endswith("too-many-requests")
    clock[0] += 0.5
    assert search().status_code == 429                                         # half a token is not a search
    clock[0] += 0.5
    assert search().status_code == 200 and search().status_code == 429         # refill: one per second, not a reset
    clock[0] += 3600
    assert [search().status_code for _ in range(21)] == [200] * 20 + [429]     # idle refills only up to the burst
    assert client.post("/v1/auth/login", json={"email": "x@example.com", "password": "y"}).status_code == 401  # own bucket


def test_public_rate_limit_sustains_60_per_minute_with_no_rollover_burst(client, monkeypatch):
    clock = [7000.0]
    monkeypatch.setattr("deps.time.monotonic", lambda: clock[0])
    ok = 0
    for _ in range(120):                                       # two minutes, one search every 0.5 s (120/min asked)
        ok += find(client, "nearest-banks", recipient="O+").status_code == 200
        clock[0] += 0.5
    assert 20 + 119 * 0.5 - 1 <= ok <= 20 + 119 * 0.5 + 1     # burst + 60/min: ~79, never the fixed window's 2x


def test_bad_input_is_rejected(client):
    assert find(client, "donors-nearby", recipient="Z+").status_code == 422
    assert find(client, "donors-nearby", recipient="O+", latitude=95).status_code == 422


# ---------- 2r: ownership filter, coordinates, demo-stock flag (ADR 022) ----------
def bank(client, test_url, lat, lng, ownership="unknown", inferred=False, source="manual", osm_id=None):
    bid = client.post("/v1/organizations", json=make_organization(kind="blood_bank", latitude=lat, longitude=lng)).json()["id"]
    client.post("/v1/blood-units", json=make_blood_unit(bid, blood_group="O+"))
    asyncio.run(run_sql(test_url, f"UPDATE organizations SET ownership = '{ownership}', ownership_inferred = {inferred}, "
                                  f"data_source = '{source}', osm_id = {f"'{osm_id}'" if osm_id else 'NULL'} "
                                  f"WHERE id = '{bid}'"))
    return bid


def test_the_ownership_filter_runs_before_the_limit_so_far_private_banks_are_still_found(client, test_url):
    """AC 9: 21 government banks are nearer than the one private bank. Filtering the 20 nearest afterwards would
    return nothing for ownership=private; the filter is inside the SQL, so the private bank comes back."""
    govt = [bank(client, test_url, 23.21 + i * 0.001, 77.10, "government", inferred=i % 2 == 0) for i in range(21)]
    private = bank(client, test_url, 23.40, 77.10, "private")
    charitable = bank(client, test_url, 23.41, 77.10, "charitable")
    unknown = bank(client, test_url, 23.42, 77.10, source="osm", osm_id="node/7")
    ids = lambda **p: [b["blood_bank_id"] for b in find(client, "nearest-banks", recipient="O+", **p).json()]  # noqa: E731
    assert ids(ownership="private") == [private]
    got = find(client, "nearest-banks", recipient="O+", ownership="government").json()
    assert [b["blood_bank_id"] for b in got] == govt[:20] and {b["ownership"] for b in got} == {"government"}
    assert {b["ownership_inferred"] for b in got} == {True, False}               # inferred included, and flagged
    assert ids() == govt[:20]                                                   # all: nearest 20, at most 20
    assert {charitable, unknown} <= set(ids(radius_km=1000, latitude=23.42))   # all includes charitable and unknown
    assert not {charitable, unknown} & set(ids(ownership="government", latitude=23.42) + ids(ownership="private",
                                                                                            latitude=23.42))
    for bad in ("state", "charitable", "unknown", "GOVERNMENT"):
        r = find(client, "nearest-banks", recipient="O+", ownership=bad)
        assert r.status_code == 422 and r.json()["type"].startswith("urn:lifelink:problem:")


def test_nearby_bank_carries_its_own_coordinates_ownership_and_the_demo_stock_flag(client, test_url):
    """AC 10-11: radius up to 1000 km; unrounded bank coordinates; stock_is_demo for osm/synthetic rows only."""
    manual = bank(client, test_url, 23.212345, 77.106789, "private")
    osm = bank(client, test_url, 23.3, 77.1, "government", inferred=True, source="osm", osm_id="node/1")
    synthetic = bank(client, test_url, 22.7196, 75.8577, source="synthetic")             # Indore, ~150 km
    got = {b["blood_bank_id"]: b for b in find(client, "nearest-banks", recipient="O+", radius_km=1000).json()}
    assert list(got) == [manual, osm, synthetic]                                         # nearest first
    assert (got[manual]["latitude"], got[manual]["longitude"]) == (23.212345, 77.106789)
    assert {k: got[osm][k] for k in ("ownership", "ownership_inferred", "stock_is_demo")} == {
        "ownership": "government", "ownership_inferred": True, "stock_is_demo": True}
    assert (got[manual]["stock_is_demo"], got[synthetic]["stock_is_demo"]) == (False, True)
    assert set(got[manual]) == {"blood_bank_id", "name", "city", "distance_km", "units", "blood_groups", "phone",
                                "stock_confirmed_at", "latitude", "longitude", "ownership", "ownership_inferred",
                                "stock_is_demo", "units_same_group"}


# ---------- 2c R1: stock and freshness filter BEFORE the LIMIT 20; units_same_group ----------
def test_the_stock_filter_runs_before_the_limit_so_far_rare_blood_is_still_found(client, test_url):
    """AC 1: 21 banks nearer than bank X hold only O+ (useless for an O- patient); X holds O-. Filtering the 20
    nearest afterwards returned nothing at any radius. Break-it (run): put the old filter-after-LIMIT back -> red."""
    for i in range(21):
        bank(client, test_url, 23.21 + i * 0.001, 77.10)                    # bank() stocks one O+ unit
    x = client.post("/v1/organizations", json=make_organization(kind="blood_bank", **INDORE_BANK)).json()["id"]
    client.post("/v1/blood-units", json=make_blood_unit(x, blood_group="O-"))
    got = find(client, "nearest-banks", recipient="O-", radius_km=1000).json()
    assert [b["blood_bank_id"] for b in got] == [x] and got[0]["units"] == 1
    assert find(client, "nearest-banks", recipient="O-", radius_km=50).json() == []    # 150 km: outside 50


def test_confirmed_within_h_keeps_only_freshly_counted_banks_and_filters_before_the_limit(client, test_url):
    """AC 3: 21 nearer banks were never counted; the one counted 2 h ago is found. 0 and 169 are 422; omitted = all."""
    stale = [bank(client, test_url, 23.21 + i * 0.001, 77.10) for i in range(21)]
    fresh = bank(client, test_url, 23.40, 77.10)
    old = bank(client, test_url, 23.41, 77.10)
    asyncio.run(run_sql(test_url, f"UPDATE organizations SET stock_confirmed_at = now() - interval '2 hours' "
                                  f"WHERE id = '{fresh}'; UPDATE organizations SET stock_confirmed_at = now() - "
                                  f"interval '25 hours' WHERE id = '{old}'"))
    ids = lambda **p: [b["blood_bank_id"] for b in find(client, "nearest-banks", recipient="O+", **p).json()]  # noqa: E731
    assert ids(confirmed_within_h=24) == [fresh]
    assert ids(confirmed_within_h=26) == [fresh, old]
    assert ids() == stale[:20]                                              # omitted: today's behaviour
    for bad in (0, 169, "x"):
        assert find(client, "nearest-banks", recipient="O+", confirmed_within_h=bad).status_code == 422


def test_units_same_group_counts_exactly_the_patients_group(client):
    """AC 16: B+ patient, 2 B+ and 4 O+ -> 2 of 6; a bank with only O+ -> 0 of its total."""
    both = client.post("/v1/organizations", json=make_organization(kind="blood_bank", **BHOPAL_BANK)).json()["id"]
    only_o = client.post("/v1/organizations", json=make_organization(kind="blood_bank", latitude=23.21,
                                                                      longitude=77.10)).json()["id"]
    for bank_id, group, n in ((both, "B+", 2), (both, "O+", 4), (only_o, "O+", 3)):
        for _ in range(n):
            client.post("/v1/blood-units", json=make_blood_unit(bank_id, blood_group=group))
    got = {b["blood_bank_id"]: (b["units_same_group"], b["units"]) for b in find(client, "nearest-banks",
                                                                                 recipient="B+").json()}
    assert got == {both: (2, 6), only_o: (0, 3)}
    assert [b["blood_bank_id"] for b in find(client, "nearest-banks", recipient="B+").json()] == [only_o, both]
