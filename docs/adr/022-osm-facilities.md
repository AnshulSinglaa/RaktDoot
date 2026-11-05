# ADR 022: Real facilities from OpenStreetMap

Date: 2026-10-20. Status: accepted. Item 2r (backend). Migration 0016.

## Context
The demo ran on synthetic "(demo)" hospitals and blood banks. A reviewer cannot tell whether the geo search, the
distances and the blood-desert analysis (item 2s) mean anything on fake points. The government directories
(data.gov.in / AIKOSH) had no working download and e-RaktKosh has no public API (D80). OpenStreetMap carries the
Ministry of Health's 2017 facility directory for Madhya Pradesh in a downloadable, licensed form: 119 blood banks and
2,104 hospitals, snapshotted in `Backend/data/osm/` (Overpass JSON, OSM base 2026-10-19T18:53Z / 18:57Z).

The rows are real but old (2017), some coordinates are wrong, phone fields hold several numbers or malformed ones,
names repeat ("Government Hospital" ~70 times), and most hospitals carry no ownership tag.

## Options
1. Keep synthetic facilities. Honest but unconvincing; the 2s analysis would be meaningless.
2. Load the JSON straight into the table. Fast, but imports bad rows silently and duplicates on every re-run.
3. **A validating, idempotent importer (`python manage.py import-osm`) keyed on the OSM id, with a printed report.**

## Decision
Option 3.
- **Licence:** ODbL 1.0, "© OpenStreetMap contributors". Attribution wherever facilities are shown (the frontend note
  and footer, item 2r-FE); a database derived from it is shared under the same licence (share-alike). Stock, holds,
  donors and accounts are not derived from OSM and stay synthetic.
- **Columns (0016):** `osm_id` (`node/123`, partial UNIQUE where not null: the idempotency key), `data_source`
  (`osm | synthetic | manual`, default manual; an `osm` row must have an `osm_id`), `source_updated_at` (the snapshot's
  `osm3s.timestamp_osm_base`: OSM gives no per-row timestamp in this query), `ownership`
  (`government | private | charitable | unknown`), `ownership_inferred` (never true for `unknown`). Backfill: names
  ending " (demo)" become `synthetic`.
- **Mapping:** coordinates from `lat/lon` (nodes) or `center` (ways, relations); name = `name`, else `name:en`; city =
  `addr:city`, else `addr:district`; phone = the first valid number of `contact:phone`, `phone`, `contact:mobile`
  (split on `; , /`). Never read: email, website, fax, personnel:* (minimisation; `organizations` has no email column,
  pinned by a test).
- **Validation** (row dropped, counted by reason): `no_name`, `outside_mp` (bounding box lat 21.0-26.9, lng 74.0-82.9;
  `ponytail:` the state polygon is the upgrade), `no_place`, `name_too_long` (200), `city_too_long` (100), `duplicate`
  (same kind, same case-folded whitespace-collapsed name, within 200 m of a kept row; the keeper is the one with a
  phone, else the lower OSM id). Never de-duplicate by name alone.
- **Phones** (row KEPT, phone NULL, counted `phone_invalid`): digits only; drop a trunk `0` (11 digits) or a `91`
  prefix (12 digits, i.e. `+91`); then exactly 10 digits, first 1-9, stored `+91XXXXXXXXXX`.
- **Ownership:** `operator:type`, case-insensitive: government/public -> government; private -> private;
  ngo/charitable/religious/community/non_profit -> charitable; anything else (e.g. `doctor`) is untagged and counted
  `operator_type_unrecognised`. Untagged: government with `ownership_inferred = true` only when the name matches (word
  boundaries) `govt`, `government`, `goverment`, `district hospital`, `civil hospital`, `gmc`, `esic`, `railway`,
  `military`, `chc`, `phc`, `community health centre/center`, `primary health centre/center`. A tag always wins.
  Supervisor decision: CHC/PHC and the health-centre names are added (in India they are NHM government tiers by
  definition); a bare "Medical College" is NOT a pattern, because this snapshot has private RKDF, Chirayu, Index,
  Amaltas and RD Gardi medical colleges. Everything else stays `unknown`: a wrong "government" label can send a poor
  family somewhere they cannot afford.
- **Idempotent upsert:** one `INSERT ... SELECT FROM unnest(...) ON CONFLICT (osm_id) WHERE osm_id IS NOT NULL DO UPDATE`
  of name, city, latitude, longitude, phone, ownership, ownership_inferred, source_updated_at only. Never touched:
  `id`, `created_at` (holds and audit point at them), and what the bank owns: `stock_confirmed_at`,
  `auto_confirm_holds`, `has_blood_centre`. The update is skipped when the kind differs (a bank with holds must stay a
  bank; counted `kind_conflict`). Rows missing from a newer snapshot are never deleted (counted
  `missing_from_snapshot`). Both files in ONE transaction, with ONE audit event `organizations.imported {source,
  inserted, updated, dropped: {reason: n}}`: counts only. The importer runs as the app role, like `seed-demo`.
- **Never contact a facility:** the notifier targets donors only (comment at `Notifier`); facility phones are shown to
  people, never dialled or messaged by the system.
- **`stock_is_demo` in the API:** `NearbyBank.stock_is_demo = data_source in ('osm', 'synthetic')`. Until a bank is
  onboarded (item 5) its stock in this build is seed data. A real bank's name next to a synthetic count must be marked
  in the contract, not only in one page, so any other client gets the warning too. `seed-demo` sets
  `stock_confirmed_at` only on its 3 demo banks: we never claim a real bank confirmed anything.
- **Public search:** `ownership=all|government|private` filters INSIDE `organizations.nearby` (SQL, before the LIMIT
  20); `radius_km` up to 1000 (MP is ~900 km across). `NearbyBank` carries the bank's own unrounded coordinates: a
  facility is a public building. Donor endpoints are unchanged and never return coordinates.

## Real numbers (snapshot of 2026-10-19, first import into an empty database)
- Blood banks: 119 read, 118 kept (118 inserted), dropped `no_place` 1; `phone_invalid` 1. Ownership: government 48
  tagged + 5 inferred, private 21, charitable 2, unknown 42.
- Hospitals: 2,104 read, 1,929 kept (1,929 inserted), dropped `no_name` 39, `no_place` 132, `duplicate` 4;
  `phone_invalid` 10; `operator_type_unrecognised` 1. Ownership: government 104 tagged + 479 inferred, private 133,
  unknown 1,213.
- Second run: 0 inserted, 118 + 1,929 updated, ids unchanged.
- Demo set chosen by `seed-demo` (nearest with a phone, ties by OSM id): hospital1 Sultania Zanana Hospital, hospital2
  Masterlal Singh Hospital (the two nearest the Bhopal centre); bank1 Indira Gandhi Hospital's Blood Bank
  (auto-confirms), bank2 Tatpar Pathology and Diagnostic Centre Blood Bank, bank3 City Blood Bank of Bhojpal
  Charitable Trust (the three nearest hospital1, so its requests land on banks that can log in).

## Rollback (revised 2026-10-20 after QA 2r, D90)
`alembic downgrade 0015` drops only the label columns (`data_source`, `ownership`, `ownership_inferred`,
`source_updated_at`) and their two table CHECKs. It KEEPS `osm_id`, its format CHECK and the partial unique index
`organizations_osm_id_uq`: they are the import's match key. The first version dropped `osm_id` too, so the documented
"re-run import-osm after the next upgrade" inserted all 2,047 facilities a second time (QA: 2,047 -> 4,094 rows).
Keeping the column is sound: it is nullable with no default, and 0015's code never reads or writes it, so the old code
runs unchanged. The upgrade uses `ADD COLUMN IF NOT EXISTS` / `CREATE UNIQUE INDEX IF NOT EXISTS`, and the upsert
now also sets `data_source = 'osm'` on conflict, so a re-import after down/up inserts 0 rows, keeps every id and
restores the labels. `test_0016_rollback_then_reimport_matches_existing_facilities_instead_of_duplicating` proves it;
restoring the old `DROP COLUMN osm_id` turns it red. Still lossy, on purpose: between the downgrade and the re-import
the labels are defaults (`manual`, `unknown`); nothing about blood or holds depends on them.

## Consequences
- The demo, the distances and 2s run on real places; every stock figure is flagged demo.
- 2,000 hospitals: every organisation list is keyset-paginated already (no unbounded list).
- Gap closed 2026-10-20 (D88): allocation holds only at banks with at least one ACTIVE `blood_bank` staff account
  (`auth.service.orgs_with_active_staff`, passed to `organizations.service.nearby(..., among=...)` and filtered inside
  the SQL, before the LIMIT). Staff-less real banks still appear in public search (phone, freshness, `stock_is_demo`)
  but never have units held: nobody there could confirm. Item 5's onboarding grows that set. hospital2 now holds at
  the onboarded demo banks 2-3 km away instead of the staff-less ones 1 km away.
- 2c (R1): public search now also filters by compatible stock (`inventory.service.banks_with_stock` -> `among=`) and
  by freshness (`confirmed_within_h` -> `confirmed_since`) inside the same nearby SQL, before the LIMIT 20. Before,
  Bhopal O- returned 5 banks at 50, 100 and 1000 km alike (see decision D92 for the after numbers).
- Re-running on a newer snapshot updates in place; deleting a facility is a manual, audited decision (never automatic).
- To start a dev database over on real facilities: drop and recreate it (or `docker compose down -v` on a throwaway
  machine), `manage.py migrate`, `import-osm`, `seed-demo`. Synthetic organisations already in a database are not
  deleted by the importer.
