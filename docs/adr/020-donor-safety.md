# ADR 020: Donor safety: interval by sex, and only in-person verified phones are matched

Status: accepted (2026-10-19), amended the same day after QA (migration 0012).

## Context
Two gaps made the matching engine unsafe in a real crisis:
1. **One interval for everyone.** Eligibility used 90 days since the last whole-blood donation for every donor. Indian
   guidance (NBTC, as we understand it; confirm with a blood centre) is 3 months for men and 4 months for women. Alerting a
   woman at day 100 asks her to do something she should not, and wastes a slot in a 20-donor alert.
2. **Unreachable donors were matched.** A self-signup with a typo'd or fake number counted in public donor counts and took a
   notification slot. In an emergency an unreachable donor is worse than no donor: the hospital waits on them.

## Options
- **Interval:** (a) keep 90 for all (unsafe for women); (b) sex-based interval, unknown sex gets the LONGER one (chosen);
  (c) store a per-donor "next eligible" date computed by the blood bank (most precise, but needs blood-bank workflow we
  do not have yet).
- **Reachability:** (a) trust self-declared phones (status quo, unsafe); (b) **SMS OTP at signup** (fast, self-service,
  but proves a SIM, not a person, costs money per message, needs a provider and its own abuse protection: OTP flooding,
  SIM farms). Deferred: a later, deliberate decision; (c) **verified in person** by blood-bank / NGO / admin staff,
  or automatically when a blood bank records a donation (chosen).

## Decision
- Migration 0011: `donors.sex` (CHECK male/female/other), `donors.phone` (CHECK `^\+[0-9]{8,15}$`, E.164),
  `donors.phone_verified_at`, partial unique index `donors_phone_uq` (one donor per number). No backfill: rows from
  before 0011 keep NULLs, so they get the 120-day interval and are not matchable until verified. No extra index for the
  new filter: it is a cheap row filter after the GiST + blood-group bitmap (EXPLAIN on 200k donors, below).
- `domain.donation_interval_days(sex)`: 90 for `male`, 120 for anything else (female, other, NULL). Configurable
  (`LIFELINK_DONATION_INTERVAL_MALE_DAYS` / `..._FEMALE_DAYS`). SQL uses two cutoffs (`eligibility_cutoffs`) and a CASE.
- Matching (`nearby_eligible_donors`) and public counts (`eligible_donor_counts_by_band`) require
  `phone_verified_at IS NOT NULL`.
- The API accepts Indian mobiles only (10 digits starting 6-9, optional `+91`/`0`, spaces, dashes) and stores `+91XXXXXXXXXX`.
- Who verifies: staff registering a walk-in with a phone (verified at once: they see the person); `POST
  /v1/donors/{id}/verify-phone` (blood_bank, ngo, admin; audited on every call; keeps the first timestamp); a blood bank
  recording a donation. A donor's own signup is never verified. Changing the phone (PATCH) resets verification.
- **Verification requires presenting the number** (QA finding 1). verify-phone takes `{"phone": ...}` and runs ONE
  statement: `UPDATE donors SET phone_verified_at = coalesce(phone_verified_at, :now) WHERE id = :id AND phone = :phone
  RETURNING id`. Only if no row matched does it read the row to explain: missing -> 404 donor-not-found, no phone -> 422
  no-phone, another number -> 409 phone-mismatch (the stored number is never echoed). A donation takes an optional
  `phone` and verifies only when it is sent and matches (same condition, inside one UPDATE that also keeps the latest
  donation date with `GREATEST`). Why not read-then-write: the old code read the row, then wrote `phone_verified_at`.
  A concurrent PATCH that swapped the number in between got a number nobody ever saw marked "verified in person". In one
  conditional UPDATE, Postgres re-checks the WHERE on the row as it is after waiting for the lock (READ COMMITTED), so a
  changed number simply does not match.
- **A phone number is unique only among VERIFIED donors** (QA finding 2, migration 0012: `donors_phone_uq` is now
  `UNIQUE (phone) WHERE phone_verified_at IS NOT NULL`). With a full unique index, anyone could self-register a real
  donor's number first (squatting: the real owner got a 409 at the camp), and the 409 answered "is this number
  registered?" for any stranger (an oracle). Now self-signup and PATCH never return a phone 409 (they only ever write
  unverified rows). Staff registration with a phone, verify-phone and a verifying donation return `409 phone-taken` only
  if ANOTHER verified donor has the number, enforced by the index itself (IntegrityError in the repository, no pre-check
  query, so no race). An unverified duplicate stays as it is: never matched, and it can never be verified with that
  number. Purging squatted numbers is a planned follow-up.

## Consequences
- Self-signups are invisible to matching until they visit a blood bank or camp. Slower onboarding, accepted: fewer,
  reachable donors beat many unreachable ones.
- **Admin can verify remotely** (e.g. after a phone call), so "in person" is policy, not enforced by the code. Accepted:
  every verification is audited with the actor id.
- **PATCH with the same number also resets verification** (we compare "field sent", not "value changed"). Accepted: the
  frontend has no phone edit yet, and a reset is the safe direction.
- **The downgrade is lossy:** 0011 down drops sex, phone and verification dates. Re-upgrading gives NULLs everywhere.
- **0012's downgrade can fail:** restoring the all-donors unique index fails if a verified and an unverified donor share a
  number. On purpose: deleting a donor to make an index fit would be data loss. Resolve those rows by hand first.
- Staff can no longer verify a number by clicking: they must type the number the donor shows. Slightly slower, and the point.
- **Seed phones are `+910XXXXXXXXX`** (donors and, since ADR 021, blood banks): the DB CHECK accepts them, the API never
  does, and no Indian number has a 0 right after +91, so a demo number can never collide with a real signup and never
  rings anyone. The first version used `+915...`, which can be a real landline (STD codes start 1-8): moved 2026-10-19. ~10% of seeded donors are unverified on purpose.
- The existing dev database has 2,001 pre-0011 donors with no sex and no phone: none is matchable until verified or
  re-seeded. Re-seeding means `docker compose down -v`, which is the operator's call.
- SMS OTP and a pre-donation screening questionnaire are deliberately NOT built. The blood centre's own screening stays the
  medical check.

## Evidence (2026-10-19)
- 252 tests pass, 93% coverage (`pytest --cov`); `test_donor_safety.py` covers each rule and QA's two attack tests
  (`test_qa_donor_safety_attacks.py`) pass without xfail. Mutating the conditional UPDATEs to ignore the presented
  number makes 4 tests fail, including both concurrent-change tests (verify-phone and donation).
- 200,000 synthetic donors, 10 km, O+ patient, the new filter in place: `Bitmap Index Scan on donors_location_gist` inside a
  BitmapAnd with the blood-group index, then `Filter: (is_available AND (phone_verified_at IS NOT NULL) AND ...)`.
  Execution 18.9 ms on a first (cold-plan) run, 555 shared buffers.
- Two seeds on emptied databases, in two separate processes (2026-10-19), gave IDENTICAL numbers: 2,001 donors
  (985 male, 953 female, 63 other), 190 unverified, 328 units. Matchable donors (available, verified, eligible by sex,
  located) per group: O+ 416, B+ 361, A+ 295, AB+ 113, B- 33, A- 28, AB- 23, O- 19. Bhopal General Hospital 1 (demo)
  posting A- x 3: 2 units reserved; notify with the page's defaults (10 km, 20 donors) queues 4, the demo donor among them
  (was 9 before 0011). Determinism needed `sorted(...)` over the set of cities: a set's order changes per process.
