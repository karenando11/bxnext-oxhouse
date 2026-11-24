"""The US equity market calendar, for the daily batch (Step 16).

``market_date_for(run_date)`` is the last completed trading day before the run date: the day the
Finviz snapshot, the score run and the screen are stored under.

Holidays are the NYSE full-day closures computed by rule (New Year's Day, Martin Luther King Jr.
Day, Presidents' Day, Good Friday, Memorial Day, Juneteenth, Independence Day, Labor Day,
Thanksgiving, Christmas). A holiday on a Saturday is observed on the Friday before and one on a
Sunday on the Monday after, except New Year's Day on a Saturday, which closes nothing. Early
closes do not matter here. Extra closures (an unscheduled one such as a national day of mourning)
are added with the ``market_holidays`` setting, a list of ISO dates.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Western Easter Sunday (the anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * ell) // 433
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(day: date) -> date | None:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def nyse_holidays(year: int) -> set[date]:
    holidays: set[date] = {
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Presidents' Day
        _easter(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
    }
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:  # a Saturday New Year's Day closes nothing
        holidays.add(_observed(new_year) or new_year)
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)) or date(year, 6, 19))
    for fixed in (date(year, 7, 4), date(year, 12, 25)):
        holidays.add(_observed(fixed) or fixed)
    return holidays


def is_trading_day(day: date, extra_holidays: Iterable[date] = ()) -> bool:
    if day.weekday() >= 5:
        return False
    return day not in nyse_holidays(day.year) and day not in set(extra_holidays)


def previous_trading_day(day: date, extra_holidays: Iterable[date] = ()) -> date:
    """The last trading day strictly before ``day``."""
    extra = set(extra_holidays)
    candidate = day - timedelta(days=1)
    while not is_trading_day(candidate, extra):
        candidate -= timedelta(days=1)
    return candidate


def market_date_for(run_date: date, extra_holidays: Iterable[date] = ()) -> date:
    """The market date a batch started on ``run_date`` describes: the last completed trading day."""
    return previous_trading_day(run_date, extra_holidays)


def trading_days_between(start: date, end: date, extra_holidays: Iterable[date] = ()) -> int:
    """Trading days after ``start`` up to and including ``end`` (0 when ``end`` is not later).

    This is the age that stale banners use: a long weekend or a market holiday does not make
    data look old.
    """
    extra = set(extra_holidays)
    count, day = 0, start + timedelta(days=1)
    while day <= end:
        if is_trading_day(day, extra):
            count += 1
        day += timedelta(days=1)
    return count
