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


def is_eligible(last_donated_at: datetime | None, now: datetime, min_interval_days: int = 90) -> bool:
    """`now` is passed IN, never read inside, so tests can pin time."""
    if last_donated_at is None:
        return True
    return now - last_donated_at >= timedelta(days=min_interval_days)