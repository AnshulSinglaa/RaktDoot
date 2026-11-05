# ADR 015: Geo search with PostGIS (nearest banks, nearest donors)

Status: accepted (2026-10-15). Drafted by Claude on Ansh's behalf: read it, own it.

## Context
"Nearest blood bank with stock" and "eligible donors near this hospital" are the two core questions. Answering them by
loading every row into Python and computing Haversine distances is O(all rows) per request and gets worse with every donor.

## Options
1. Haversine in Python. Simple; scans everything, every time.
2. A lat/lng bounding box on two B-tree indexes. Better, but a box is not a circle, and two 1-D indexes combine poorly.
3. PostGIS `geography` + a GiST index + `ST_DWithin`. The index answers "what is inside this circle" directly.
4. Geohash / H3 cells. Great for pre-bucketing at very large scale (CLAUDE.md 6.5); unnecessary here.

## Decision
Option 3.
- Image `postgis/postgis:16-3.4` (compose + testcontainers fallback). `CREATE EXTENSION postgis` in migration 0009.
- The app writes plain `latitude`/`longitude`; Postgres derives `location geography(Point,4326) GENERATED ALWAYS AS
  (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography) STORED`, with a GiST index. No ORM plugin
  (geoalchemy2) and no chance of the two drifting apart.
- Banks: `organizations.service.nearby(kind, lat, lng, radius)`. Donors: `repository.nearby_eligible_donors(...)`.
- Privacy: donor coordinates are rounded to 2 decimals (~1.1 km) by the API schema before storage; hospitals see a
  distance BAND, first name and blood group only; every donor lookup writes an audit event.

## Evidence (2026-10-15, 200,000 synthetic donors spread over Madhya Pradesh, 10 km radius, O+ patient)
| | Execution time | Shared buffers |
|---|---|---|
| With GiST index | **9.3 ms** | 586 |
| Without (index dropped in a rolled-back transaction) | 234 ms | 5,389 |
The planner combined the GiST index with the existing `blood_group` index (BitmapAnd), so only ~400 nearby rows were
visited. Without it, every O+/O- donor in the state (~50k rows) was distance-checked.

## Consequences
- Distance is straight-line over the earth's surface, not road distance or travel time. Good enough to RANK candidates;
  ETA needs a routing engine (self-hosted OSRM, CLAUDE.md 5.12).
- The blood-group index scan (~2 ms, 50k rows) is now the largest part of the query. Upgrade if it grows:
  a partial index for available donors, or a composite GiST including the group (btree_gist).
- Nominatim (geocoding) is NOT called at request time: coordinates are geocoded once, at onboarding.
