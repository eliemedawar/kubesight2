from datetime import datetime, timezone

from api.datetime_utils import serialize_utc_datetime


def test_serialize_utc_datetime_appends_z_for_naive_db_values():
    naive = datetime(2026, 6, 9, 12, 10, 52, 112956)
    assert serialize_utc_datetime(naive) == "2026-06-09T12:10:52.112956Z"


def test_serialize_utc_datetime_normalizes_aware_values():
    aware = datetime(2026, 6, 9, 15, 10, 52, tzinfo=timezone.utc)
    assert serialize_utc_datetime(aware) == "2026-06-09T15:10:52Z"
