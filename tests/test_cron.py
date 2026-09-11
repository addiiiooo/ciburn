from __future__ import annotations

import pytest

from ciburn.cron import parse_cron, runs_per_day


@pytest.mark.parametrize(
    ("expr", "per_day"),
    [
        ("0 0 * * *", 1.0),
        ("*/15 * * * *", 96.0),
        ("0 */6 * * *", 4.0),
        ("30 3 * * 1-5", 5 / 7),
        ("0 0 1 * *", 1 / 28),
        ("0 9,17 * * mon,wed,fri", 6 / 7),
        ("0 0 * jan *", 0.0),
        ("0 12 1 * 0", 4 / 28),  # POSIX: day-of-month OR weekday; Feb 1 2026 is a Sunday
    ],
)
def test_runs_per_day(expr: str, per_day: float) -> None:
    got = runs_per_day(expr)
    assert got is not None
    assert got == pytest.approx(per_day, rel=1e-6)


@pytest.mark.parametrize(
    "expr", ["", "* * * *", "61 * * * *", "a b c d e", "*/0 * * * *", "5-1 * * * *", "0 0 * * 8"]
)
def test_invalid(expr: str) -> None:
    assert parse_cron(expr) is None
    assert runs_per_day(expr) is None


def test_weekday_seven_is_sunday() -> None:
    spec = parse_cron("0 0 * * 7")
    assert spec is not None
    assert spec.weekdays == frozenset({0})
    assert parse_cron("0 0 * * 1/2") is not None
