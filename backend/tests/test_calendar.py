"""The market calendar behind the batch's market date."""

from __future__ import annotations

from datetime import date

import pytest

from apps.platform.calendar import (
    is_trading_day,
    market_date_for,
    nyse_holidays,
    previous_trading_day,
)

HOLIDAYS = """
2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07
2026-11-26 2026-12-25 2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18
2027-07-05 2027-09-06 2027-11-25 2027-12-24
""".split()


@pytest.mark.parametrize("holiday", [date.fromisoformat(d) for d in HOLIDAYS])
def test_nyse_holidays(holiday: date) -> None:
    assert holiday in nyse_holidays(holiday.year)
    assert not is_trading_day(holiday)


def test_a_saturday_new_years_day_closes_nothing() -> None:
    assert date(2028, 1, 1) not in nyse_holidays(2028)
    assert is_trading_day(date(2027, 12, 31))  # the Friday before stays open


def test_ordinary_days_are_trading_days() -> None:
    assert is_trading_day(date(2026, 10, 12))  # Columbus Day is not a market holiday
    assert not is_trading_day(date(2026, 10, 10))  # Saturday
    assert not is_trading_day(date(2026, 10, 11))  # Sunday


@pytest.mark.parametrize(
    ("run", "market"),
    [
        (date(2026, 10, 13), date(2026, 10, 12)),  # Tuesday: Monday's close
        (date(2026, 10, 12), date(2026, 10, 9)),  # Monday: Friday's close
        (date(2026, 10, 10), date(2026, 10, 9)),  # a Saturday run: still Friday
        (date(2027, 1, 19), date(2027, 1, 15)),  # Tuesday after Monday's holiday
        (date(2027, 1, 18), date(2027, 1, 15)),  # the holiday Monday itself
        (date(2026, 11, 27), date(2026, 11, 25)),  # the day after Thanksgiving
        (date(2026, 4, 6), date(2026, 4, 2)),  # Monday after Good Friday
    ],
)
def test_market_date_is_the_last_completed_trading_day(run: date, market: date) -> None:
    assert market_date_for(run) == market


def test_extra_closures_come_from_settings() -> None:
    assert market_date_for(date(2026, 10, 13), [date(2026, 10, 12)]) == date(2026, 10, 9)
    assert previous_trading_day(date(2026, 10, 13), {date(2026, 10, 12)}) == date(2026, 10, 9)
