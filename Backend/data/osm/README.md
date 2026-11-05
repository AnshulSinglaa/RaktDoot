# Real facility snapshots for Madhya Pradesh (OpenStreetMap)

| File | What | Rows | Snapshot |
|---|---|---|---|
| `mp_blood_banks.json` | blood banks / blood centres (`healthcare=blood_donation`, `amenity=blood_bank`, or a name containing "blood bank/centre") | 119 (118 with a phone, 47 districts) | OSM base 2026-10-19T18:53Z |
| `mp_hospitals.json` | `amenity=hospital` | 2,104 (2,064 named, 437 with a phone) | OSM base 2026-10-19T18:57Z |

Most rows are tagged `source=OpenGovernmentData`: the Ministry of Health's National Health Portal directories
(published 2017) imported into OpenStreetMap (https://wiki.openstreetmap.org/wiki/India_Health_Facilities_Import).
So the names, addresses and phones are REAL but can be years out of date, and some coordinates are wrong
(the import notes warn about this). The importer must validate every row (inside MP's bounding box, phone format)
and report what it dropped.

**What is real and what is not:** facility names, locations and public phone numbers are real. Blood stock, holds and
donors in the demo are SYNTHETIC and labelled as such. The app must never call, SMS or email a real facility.

**Licence:** © OpenStreetMap contributors, Open Database License (ODbL 1.0), https://www.openstreetmap.org/copyright.
Attribution is required wherever this data is shown (the map/footer says "© OpenStreetMap contributors"), and a
database derived from it is shared under the same licence.

**Re-download** (Overpass API, needs a User-Agent header):
```
[out:json][timeout:90];area["ISO3166-2"="IN-MP"]->.a;
(nwr["healthcare"="blood_donation"](area.a);nwr["amenity"="blood_bank"](area.a);
 nwr["name"~"blood bank|blood centre|blood center",i](area.a););out center tags;
```
and `nwr["amenity"="hospital"](area.a);out center tags;` for hospitals.
