import pytest
from pydantic import ValidationError

from schemas import DonorCreate, DonorRead, DonorUpdate

GOOD = {"name": "  Asha  ", "email": "ASHA@Example.com", "age": 30, "blood_group": "O+"}


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


def test_update_can_clear_last_donated_at_with_explicit_null():
    assert DonorUpdate(last_donated_at=None).model_dump(exclude_unset=True) == {"last_donated_at": None}


def test_read_from_attributes():
    class Row:
        id = "00000000-0000-0000-0000-000000000001"
        name = "Asha"
        blood_group = "O+"
        is_available = True
        eligible_now = True

    assert DonorRead.model_validate(Row()).name == "Asha"