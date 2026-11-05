"""The request lifecycle as data. Pure: no database, so every rule is a one-line test.

    OPEN ──(all units reserved)──> ALLOCATED ──(hospital received units)──> FULFILLED
      │  <──(a hold was rejected or expired)──┘                                 ^
      ├──────────(received SOME units: partly fulfilled, ADR 021 rev 2)─────────┘
      └──────────────(cancel)──────────────> CANCELLED <──(cancel)── ALLOCATED
Cancel is refused (in the router) once any unit was collected: that blood is in a patient, not back on a shelf.
"""
TRANSITIONS: dict[str, set[str]] = {
    "OPEN": {"ALLOCATED", "CANCELLED", "FULFILLED"},      # FULFILLED: partly fulfilled, the rest released
    "ALLOCATED": {"FULFILLED", "CANCELLED", "OPEN"},       # OPEN: a bank rejected a hold, or it expired (ADR 021)
    "FULFILLED": set(),         # terminal: the blood is in a patient
    "CANCELLED": set(),         # terminal: units went back to stock
}


def can_move(current: str, target: str) -> bool:
    return target in TRANSITIONS[current]


def status_after_allocation(units_needed: int, units_allocated: int) -> str:
    return "ALLOCATED" if units_allocated >= units_needed else "OPEN"


if __name__ == "__main__":
    assert can_move("OPEN", "ALLOCATED") and can_move("ALLOCATED", "FULFILLED")
    assert not can_move("FULFILLED", "CANCELLED") and can_move("OPEN", "FULFILLED")
    assert can_move("ALLOCATED", "OPEN") and not can_move("FULFILLED", "OPEN")
    assert status_after_allocation(3, 2) == "OPEN" and status_after_allocation(3, 3) == "ALLOCATED"
    print("state self-check ok")
