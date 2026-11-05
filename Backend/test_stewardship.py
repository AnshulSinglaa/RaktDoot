"""stewardship_rank, the pure rule behind the order of units inside one bank (ADR 021 revision 2)."""
import pytest

from domain import BloodGroup as G
from domain import compatible_groups, stewardship_rank


def order(patient: str, component: str = "red_cells") -> list[list[str]]:
    """The patient's compatible groups, bucketed by rank: [[rank 0 groups], [rank 1 groups], ...]."""
    ranks: dict[int, list[str]] = {}
    for g in compatible_groups(G(patient), component):
        ranks.setdefault(stewardship_rank(G(patient), g, component), []).append(g.value)
    return [sorted(ranks[k]) for k in sorted(ranks)]


@pytest.mark.parametrize("patient, expected", [
    ("B+", [["B+"], ["O+"], ["B-"], ["O-"]]),
    ("AB+", [["AB+"], ["A+", "B+", "O+"], ["A-", "AB-", "B-"], ["O-"]]),
    ("O-", [["O-"]]),
    ("A-", [["A-"], ["O-"]]),
    ("O+", [["O+"], ["O-"]]),
])
def test_red_cells_exact_then_same_rh_then_others_and_o_negative_last(patient, expected):
    assert order(patient) == expected


def test_plasma_is_exact_first_then_the_rest_unchanged():
    assert order("A+", "plasma") == [["A+"], ["A-", "AB+", "AB-"]]
