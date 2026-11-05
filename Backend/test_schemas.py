import pytest
from pydantic import ValidationError

from schemas import DonorCreate, DonorRead, DonorUpdate

GOOD = {"name": "  Asha  ", "email": "ASHA@Example.com", "age": 30, "blood_group": "O+", "sex": "female", "consent": True}


def test_create_normalises_name_and_email():
    d = DonorCreate(**GOOD)
    assert d.name == "Asha" and d.email == "asha@example.com"


def test_create_rejects_unknown_field():
    with pytest.raises(ValidationError):
        DonorCreate(**GOOD, is_admin=True)


def test_create_rejects_naive_datetime():
    with pytest.raises(ValidationError):
        DonorCreate(**GOOD, last_donated_at="2026-09-01T10:00:00")      # no timezone


def test_update_only_reports_fields_that_were_sent():
    u = DonorUpdate(is_available=False)
    assert u.model_dump(exclude_unset=True) == {"is_available": False}


def test_update_explicit_null_for_required_field_rejected():
    with pytest.raises(ValidationError):
        DonorUpdate(name=None)


def test_update_empty_rejected():
    with pytest.raises(ValidationError):
        DonorUpdate()


def test_update_cannot_touch_last_donated_at():
    # threat model #1: a donor resetting his own eligibility clock
    with pytest.raises(ValidationError):
        DonorUpdate(last_donated_at=None)


def test_read_from_attributes():
    class Row:
        id = "00000000-0000-0000-0000-000000000001"
        name = "Asha"
        blood_group = "O+"
        is_available = True
        eligible_now = True

    assert DonorRead.model_validate(Row()).name == "Asha"

@pytest.mark.parametrize("consent", [False, None, "yes"])
def test_create_requires_explicit_consent(consent):
    with pytest.raises(ValidationError):
        DonorCreate(**{**GOOD, "consent": consent})
    with pytest.raises(ValidationError):
        DonorCreate(**{k: v for k, v in GOOD.items() if k != "consent"})


@pytest.mark.parametrize("raw", ["98123 45678", "09812345678", "+91 98123-45678", "919812345678"])
def test_indian_mobiles_are_normalised_to_e164(raw):
    assert DonorCreate(**GOOD, phone=raw).phone == "+919812345678"
    assert DonorUpdate(phone=raw).phone == "+919812345678"


@pytest.mark.parametrize("raw", ["5812345678", "981234567", "98123456789", "98123abcde", "98123x45678"])
def test_bad_phones_are_rejected(raw):
    # starts with 5 (not a mobile), 9 digits, 11 digits without a leading 0, letters (even around 10 digits)
    with pytest.raises(ValidationError):
        DonorCreate(**GOOD, phone=raw)


def test_sex_is_required_and_closed():
    without = {k: v for k, v in GOOD.items() if k != "sex"}
    with pytest.raises(ValidationError):
        DonorCreate(**without)
    with pytest.raises(ValidationError):
        DonorCreate(**{**GOOD, "sex": "x"})
