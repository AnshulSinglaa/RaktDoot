from datetime import datetime, timedelta, timezone
from itertools import count

_n = count(1)


def make_donor(**overrides) -> dict:
    """A valid POST /v1/donors body with a unique email and phone. Override only what the test is about.
    When staff (the default admin `client`) post it, the phone counts as verified in person, so the donor is matchable."""
    n = next(_n)
    return {"name": f"Donor {n}", "email": f"donor{n}@example.com", "age": 30, "blood_group": "O+", "sex": "male",
            "phone": f"+91900{n:07d}", "consent": True, **overrides}


def make_organization(**overrides) -> dict:
    """A valid POST /v1/organizations body."""
    n = next(_n)
    return {"kind": "hospital", "name": f"Org {n}", "city": "Bhopal", **overrides}


def make_blood_unit(blood_bank_id: str, **overrides) -> dict:
    """A valid POST /v1/blood-units body: collected yesterday, expiring in 20 days (red cells)."""
    now = datetime.now(timezone.utc)
    return {
        "blood_bank_id": blood_bank_id,
        "blood_group": "O+",
        "component": "red_cells",
        "collected_at": (now - timedelta(days=1)).isoformat(),
        "expires_at": (now + timedelta(days=20)).isoformat(),
        **overrides,
    }
