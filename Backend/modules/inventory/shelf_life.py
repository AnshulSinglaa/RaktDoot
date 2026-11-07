from datetime import datetime, timedelta

# Trade-off: upper bounds for how long a unit may be stored, NOT clinical advice. Blood banks print the real expiry on the
# bag; we only refuse impossible values. Confirm every number against blood-bank / NBTC rules before real use.
MAX_SHELF_LIFE_DAYS = {"red_cells": 42, "platelets": 7, "plasma": 365}


def shelf_life_error(component: str, collected_at: datetime, expires_at: datetime, now: datetime) -> str | None:
    """Why this unit cannot be registered, or None if it can. Pure function: no database, no clock of its own."""
    if expires_at <= collected_at:
        return "expires_at must be after collected_at"
    if collected_at > now:
        return "collected_at cannot be in the future"
    if expires_at <= now:
        return "this unit has already expired"
    longest = MAX_SHELF_LIFE_DAYS[component]
    if expires_at - collected_at > timedelta(days=longest):
        return f"{component} cannot be stored longer than {longest} days"
    return None
