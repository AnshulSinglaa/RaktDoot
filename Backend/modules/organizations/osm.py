"""OpenStreetMap snapshot -> organization rows. Pure functions: no database, no clock (ADR 022).

Only what a family needs to call a facility is kept: name, place, coordinates, one public phone, ownership.
Email, website, fax and personnel:* tags are never read (minimisation)."""
import math
import re
from collections import Counter

# ponytail: Madhya Pradesh's bounding box, not its polygon. A few rows just across the border pass (accepted); the
# upgrade is a point-in-polygon check against the state boundary (the OSM relation tagged ISO3166-2=IN-MP).
MP_BOX = {"lat": (21.0, 26.9), "lng": (74.0, 82.9)}
DUPLICATE_WITHIN_M = 200
PHONE_TAGS = ("contact:phone", "phone", "contact:mobile")

OWNERSHIP_TAGS = {"government": "government", "public": "government", "private": "private",
                  "ngo": "charitable", "charitable": "charitable", "religious": "charitable",
                  "community": "charitable", "non_profit": "charitable"}
# In India these names denote a government facility (CHC/PHC are NHM tiers). A bare "Medical College" is NOT here:
# RKDF, Chirayu, Index, Amaltas and RD Gardi are private medical colleges in this very snapshot (ADR 022).
GOVERNMENT_NAME = re.compile(
    r"\b(govt|government|goverment|district hospital|civil hospital|gmc|esic|railway|military|chc|phc"
    r"|community health cent(re|er)|primary health cent(re|er))\b", re.IGNORECASE)


def infer_ownership(operator_type: str | None, name: str) -> tuple[str, bool]:
    """(ownership, inferred). A recognised operator:type tag always wins; else a government name pattern; else unknown."""
    tagged = OWNERSHIP_TAGS.get((operator_type or "").strip().lower())
    if tagged:
        return tagged, False
    if GOVERNMENT_NAME.search(name):
        return "government", True
    return "unknown", False


def normalise_phone(raw: str | None) -> str | None:
    """First valid Indian number in a tag value ('a; b', 'a, b', 'a / b') as +91XXXXXXXXXX, else None.
    Valid = 10 national digits, first 1-9, after dropping a trunk 0 (11 digits) or a 91 / +91 prefix (12 digits)."""
    for piece in re.split(r"[;,/]", raw or ""):
        digits = re.sub(r"\D", "", piece)
        if len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        elif len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        if len(digits) == 10 and digits[0] != "0":
            return "+91" + digits
    return None


def _metres(a: dict, b: dict) -> float:
    """Haversine. Fine here: an import-time check on a handful of same-name rows, never a query (that is PostGIS)."""
    p1, p2 = math.radians(a["latitude"]), math.radians(b["latitude"])
    dp, dl = p2 - p1, math.radians(b["longitude"] - a["longitude"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def map_snapshot(elements: list[dict], kind: str) -> tuple[list[dict], Counter]:
    """Overpass `elements` -> (rows to upsert, counts). Dropped rows are counted by reason; kept rows with an
    unusable phone are kept with phone None and counted `phone_invalid`."""
    counts: Counter = Counter()
    candidates = []
    for e in elements:
        tags = e.get("tags", {})
        where = e.get("center", e)              # node: lat/lon on the element; way/relation: Overpass's center
        lat, lng = where.get("lat"), where.get("lon")
        name = (tags.get("name") or tags.get("name:en") or "").strip()
        city = (tags.get("addr:city") or tags.get("addr:district") or "").strip()
        if not name:
            reason = "no_name"
        elif lat is None or not (MP_BOX["lat"][0] <= lat <= MP_BOX["lat"][1]
                                 and MP_BOX["lng"][0] <= lng <= MP_BOX["lng"][1]):
            reason = "outside_mp"
        elif not city:
            reason = "no_place"
        elif len(name) > 200:                   # the CHECKs of migration 0001
            reason = "name_too_long"
        elif len(city) > 100:
            reason = "city_too_long"
        else:
            reason = None
        if reason:
            counts[f"dropped:{reason}"] += 1
            continue
        raw_phones = [tags[t] for t in PHONE_TAGS if tags.get(t)]
        phone = next((p for p in map(normalise_phone, raw_phones) if p), None)
        if raw_phones and phone is None:
            counts["phone_invalid"] += 1
        operator_type = tags.get("operator:type")
        if operator_type and operator_type.strip().lower() not in OWNERSHIP_TAGS:
            counts["operator_type_unrecognised"] += 1
        ownership, inferred = infer_ownership(operator_type, name)
        candidates.append({"osm_id": f"{e['type']}/{e['id']}", "kind": kind, "name": name, "city": city,
                           "latitude": lat, "longitude": lng, "phone": phone,
                           "ownership": ownership, "ownership_inferred": inferred, "_order": (phone is None, e["id"])})

    # duplicates: same name (case-folded, whitespace collapsed) AND within 200 m. Never by name alone: "Government
    # Hospital" alone appears ~70 times in MP. The keeper is the one with a phone, else the lower OSM id.
    kept, by_name = [], {}
    for row in sorted(candidates, key=lambda r: r["_order"]):
        key = " ".join(row["name"].casefold().split())
        if any(_metres(row, other) <= DUPLICATE_WITHIN_M for other in by_name.get(key, [])):
            counts["dropped:duplicate"] += 1
            continue
        by_name.setdefault(key, []).append(row)
        kept.append({k: v for k, v in row.items() if k != "_order"})
    for row in kept:
        counts[f"ownership:{row['ownership']}" + (":inferred" if row["ownership_inferred"] else "")] += 1
    return kept, counts


def _joined(r: dict, prefix: str) -> str:
    return "; ".join(f"{k[len(prefix):]} {n}" for k, n in r.items() if k.startswith(prefix)) or "none"


def format_report(report: dict) -> str:
    """One line per kind, e.g. `[import-osm] blood_bank: 119 read, 118 kept (117 inserted, 1 updated), dropped: ...`."""
    return "\n".join(
        f"[import-osm] {kind}: {r['read']} read, {r['kept']} kept ({r['inserted']} inserted, {r['updated']} updated)"
        f", dropped: {_joined(r, 'dropped:')}; phone_invalid {r.get('phone_invalid', 0)}; "
        f"operator_type_unrecognised {r.get('operator_type_unrecognised', 0)}; kind_conflict {r['kind_conflict']}; "
        f"missing_from_snapshot {r['missing_from_snapshot']}; ownership: {_joined(r, 'ownership:')}"
        for kind, r in report.items())
