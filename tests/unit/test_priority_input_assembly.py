from datetime import date

import pytest

from services.priority.input_assembly import parse_persisted_date


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("2026-09-02", date(2026, 9, 2)),
        ("2026-09-02T10:30:00", date(2026, 9, 2)),
        ("2026-09-03T00:30:00+02:00", date(2026, 9, 2)),
        ("2026-09-01T23:30:00-02:00", date(2026, 9, 2)),
    ],
)
def test_strict_persisted_date_parsing(raw, expected):
    assert parse_persisted_date(raw) == expected


@pytest.mark.parametrize("raw", ["", " ", "yesterday", "unknown", "2026-02-30"])
def test_invalid_persisted_dates_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_persisted_date(raw)
