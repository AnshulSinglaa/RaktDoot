from itertools import count

_n = count(1)


def make_donor(**overrides) -> dict:
    """A valid POST /v1/donors body with a unique email. Override only what the test is about."""
    n = next(_n)
    return {"name": f"Donor {n}", "email": f"donor{n}@example.com", "age": 30, "blood_group": "O+", **overrides}
