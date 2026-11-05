from datetime import datetime, timedelta, timezone

import pytest

from modules.inventory.shelf_life import MAX_SHELF_LIFE_DAYS, shelf_life_error

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def error(component="red_cells", collected=NOW - DAY, expires=NOW + 10 * DAY):
    return shelf_life_error(component, collected, expires, NOW)


def test_a_normal_unit_is_accepted():
    assert error() is None


def test_expiry_must_come_after_collection():
    assert "after collected_at" in error(expires=NOW - 2 * DAY)


def test_collection_cannot_be_in_the_future():
    assert "future" in error(collected=NOW + DAY, expires=NOW + 5 * DAY)


def test_an_already_expired_unit_is_refused():
    assert "already expired" in error(collected=NOW - 10 * DAY, expires=NOW - DAY)


@pytest.mark.parametrize("component", list(MAX_SHELF_LIFE_DAYS))
def test_the_longest_allowed_storage_is_accepted_and_one_second_more_is_not(component):
    longest = timedelta(days=MAX_SHELF_LIFE_DAYS[component])
    collected = NOW - DAY
    assert shelf_life_error(component, collected, collected + longest, NOW) is None
    too_long = shelf_life_error(component, collected, collected + longest + timedelta(seconds=1), NOW)
    assert too_long is not None and "cannot be stored longer" in too_long
