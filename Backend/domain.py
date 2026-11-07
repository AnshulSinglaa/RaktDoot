"""Pure business rules. No FastAPI, no database, no clock calls."""
from datetime import datetime, timedelta
from enum import StrEnum


class BloodGroup(StrEnum):
    A_POS = "A+"
    A_NEG = "A-"
    B_POS = "B+"
    B_NEG = "B-"
    AB_POS = "AB+"
    AB_NEG = "AB-"
    O_POS = "O+"
    O_NEG = "O-"


def _antigens(group: BloodGroup) -> frozenset[str]:
    """A group is just the set of markers on the red cells: A, B, and Rh(D)."""
    abo, rh = group.value[:-1], group.value[-1]
    found = {a for a in ("A", "B") if a in abo}
    if rh == "+":
        found.add("D")
    return frozenset(found)


def can_donate_red_cells(donor: BloodGroup, recipient: BloodGroup) -> bool:
    """Red cells are safe if the donor has no marker the recipient lacks."""
    return _antigens(donor) <= _antigens(recipient)


def compatible_donor_groups(recipient: BloodGroup) -> list[BloodGroup]:
    return [g for g in BloodGroup if can_donate_red_cells(g, recipient)]


def donation_interval_days(sex: str | None, male_days: int, female_days: int) -> int:
    """The gap required since the last whole-blood donation. Anything but 'male' (female, other, unknown) gets the
    LONGER interval: when unsure, protect the donor. The blood centre's own check is still final."""
    return male_days if sex == "male" else female_days


def eligibility_cutoffs(now: datetime, male_days: int, female_days: int) -> tuple[datetime, datetime]:
    """(male_cutoff, other_cutoff): a donor is eligible if they last donated on or before their cutoff."""
    return now - timedelta(days=male_days), now - timedelta(days=female_days)


def indian_ten_digits(v: str) -> str:
    """The 10 national digits of an Indian number written any common way: '98123 45678', '09812345678',
    '+91 98123-45678', '0755 2345678'. Raises ValueError on letters or a wrong length. Callers add their own rule for the
    first digit (mobiles start 6-9; facility numbers may be landlines but never start with 0 after +91)."""
    if any(c not in "0123456789 +-()" for c in v):
        raise ValueError("a phone number has digits, spaces, '+', '-' and brackets only")
    digits = "".join(c for c in v if c.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError("enter a 10-digit Indian number")
    return digits


def is_eligible(last_donated_at: datetime | None, now: datetime, min_interval_days: int = 90) -> bool:
    """`now` is passed IN, never read inside, so tests can pin time."""
    if last_donated_at is None:
        return True
    return now - last_donated_at >= timedelta(days=min_interval_days)

def _abo(group: BloodGroup) -> frozenset[str]:
    return _antigens(group) - {"D"}


def compatible_plasma_donor_groups(recipient: BloodGroup) -> list[BloodGroup]:
    """Plasma runs the OTHER way: donor plasma must carry no antibody against the recipient's A/B markers, i.e. the
    donor must HAVE every ABO marker the recipient has. AB plasma is universal. Rh is ignored for plasma (no red cells)."""
    return [g for g in BloodGroup if _abo(recipient) <= _abo(g)]


def compatible_groups(recipient: BloodGroup, component: str) -> list[BloodGroup]:
    """The system PROPOSES; the blood bank's cross-match decides (medical safety)."""
    if component == "red_cells":
        return compatible_donor_groups(recipient)
    if component == "plasma":
        return compatible_plasma_donor_groups(recipient)
    raise ValueError(f"no compatibility rule for {component}")     # platelets: ABO-identical policy differs per bank


def stewardship_rank(patient: BloodGroup, unit: BloodGroup, component: str) -> int:
    """Which compatible unit a bank should give first (lower = first), so scarce groups stay for the patients who need
    them. Red cells: the exact group, then the same Rh sign, then other compatible groups, O- last for anyone not O-
    (O- is the only red cell an O- patient, or a patient of unknown group, can get). Plasma: exact first, then the rest.
    Only reorders units INSIDE one bank among already-compatible ones, so it never makes blood farther away (ADR 021
    revision 2). Trade-off: this order awaits a medical officer's review; a cross-bank preference needs the same sign-off."""
    if unit == patient:
        return 0
    if component != "red_cells":
        return 1
    if unit == BloodGroup.O_NEG:
        return 3
    return 1 if unit.value[-1] == patient.value[-1] else 2
