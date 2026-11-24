"""The parallel run (Step 20): compare a day's export of the current system with this one.

The current system's daily export has the format of the golden export. For one market date this
module reads it, reads the ``company_page`` rows of the same date and reports every score that
differs by more than 0.05 and every page field that does not match (counts exactly, ratios within
display rounding). Each difference is classified as one of

* ``carried_forward_input``: our runway or catalyst input was carried (or expired), so the score
  or field built on it differs by design;
* ``known_quirk_fix``: listed in the setting ``parallel.known_quirk_fixes`` (``"TICKER:field"`` or
  ``"*:field"``), a difference the team has decided is a fix;
* ``source_data_timing``: the two systems read their sources at different times (06:00 here, 16:30
  there): catalyst, earnings and trial fields, milestone-driven scores, runway rows of another
  date;
* ``unexplained``: everything else.

``evaluate_pass`` applies the acceptance rule over 14 consecutive trading days.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from apps.platform.calendar import is_trading_day
from apps.platform.models import ParallelDiff
from apps.platform.settings_store import get_setting
from apps.web.models import CompanyPage
from scoring_engine import display_score

SCORE_TOLERANCE = 0.05
WINDOW_DAYS = 14
MIN_GOOD_DAYS = 12
GOOD_DAY_SHARE = 0.99
MAX_UNEXPLAINED_DAYS = 2
MAX_DIFFS_STORED = 6000
TIMING_MILESTONE_DELTA = 0.5  # largest milestone gap the clock difference can explain

CARRIED, QUIRK, TIMING, UNEXPLAINED = (
    "carried_forward_input",
    "known_quirk_fix",
    "source_data_timing",
    "unexplained",
)
CLASSES = (CARRIED, QUIRK, TIMING, UNEXPLAINED)

# The old system's runway rating is 1 to 5; ours has four names. 3 and 4 (12 to 24 months) are
# both "Moderate".
OLD_RATING_NAMES = {"1": "Critical", "2": "Short", "3": "Moderate", "4": "Moderate", "5": "Long"}


# ------------------------------------------------------------------------------------ reading


def parse_cell(raw: str | None) -> Any:
    """Blank and '-' are None; numbers become floats; True and False become booleans."""
    if raw is None:
        return None
    text = raw.strip()
    if text in ("", "-"):
        return None
    if text in ("True", "False"):
        return text == "True"
    try:
        number = float(text)
    except ValueError:
        return text
    return None if math.isnan(number) else number


def read_export(path: Path) -> dict[str, dict[str, Any]]:
    """The export's rows by ticker (upper case); a repeated ticker keeps its first row."""
    rows: dict[str, dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for raw in csv.DictReader(handle):
            ticker = (raw.get("ticker") or "").strip().upper()
            if ticker and ticker not in rows:
                rows[ticker] = {k: parse_cell(v) for k, v in raw.items() if k}
    return rows


# --------------------------------------------------------------------------- what is compared


@dataclass(frozen=True)
class Spec:
    name: str  # the export column
    kind: str  # score, count, ratio or text
    tol: float
    ours: Callable[[CompanyPage], Any]
    group: str  # runway, catalyst, trials, earnings, market or scores
    page_field: str = ""


def _attr(name: str) -> Callable[[CompanyPage], Any]:
    return lambda row: getattr(row, name)


def _quarters(row: CompanyPage) -> Any:
    months = row.runway_months
    return None if months is None else math.floor(months / 3)


def _rating(row: CompanyPage) -> Any:
    if row.runway_months is None:
        return None
    from apps.sec.runway import runway_rating

    return runway_rating(row.runway_months)


def _old_rating(value: Any) -> Any:
    return None if value is None else OLD_RATING_NAMES.get(str(int(value)))


def _spec(
    name: str, kind: str, tol: float, ours: str | Callable[[CompanyPage], Any], group: str
) -> Spec:
    return Spec(name, kind, tol, _attr(ours) if isinstance(ours, str) else ours, group)


def _shown(score: str, column: str) -> Callable[[CompanyPage], Any]:
    """Our score as the page shows it: the old system exports displayed (rounded) scores."""
    return lambda row: display_score(score, getattr(row, column))


SCORE_SPECS = (
    _spec("composite_score", "score", SCORE_TOLERANCE, "composite", "scores"),  # exported unrounded
    _spec("cash_health", "score", SCORE_TOLERANCE, _shown("cash_health", "cash_health"), "scores"),
    _spec(
        "milestone_proximity", "score", SCORE_TOLERANCE, _shown("milestone", "milestone"), "scores"
    ),
    _spec("outlier_score", "score", SCORE_TOLERANCE, _shown("outlier", "outlier"), "scores"),
    _spec("divergence_recency", "count", 0, "recency", "scores"),
)

FIELD_SPECS = (
    _spec("price", "ratio", 0.005, "price", "market"),
    _spec("mcap", "ratio", 0.5, "mcap_musd", "market"),
    _spec("w52h", "ratio", 0.005, "w52h", "market"),
    _spec("sma200", "ratio", 0.005, "sma200", "market"),
    _spec("rsi", "ratio", 0.05, "rsi", "market"),
    _spec("avg_vol", "ratio", 0.5, "avg_vol_k", "market"),
    _spec("rel_vol", "ratio", 0.0005, "rel_vol", "market"),
    _spec("perf_w", "ratio", 0.005, "perf_w", "market"),
    _spec("perf_m", "ratio", 0.005, "perf_m", "market"),
    _spec("perf_q", "ratio", 0.005, "perf_q", "market"),
    _spec("perf_ytd", "ratio", 0.005, "perf_ytd", "market"),
    _spec("cash_sh", "ratio", 0.005, "cash_sh", "market"),
    _spec("quick_ratio", "ratio", 0.005, "quick_ratio", "market"),
    _spec("td_eq", "ratio", 0.005, "td_eq", "market"),
    _spec("short_float", "ratio", 0.005, "short_float", "market"),
    _spec("inst_own", "ratio", 0.005, "inst_own", "market"),
    _spec("indication", "text", 0, "industry", "market"),
    _spec("earnings_date", "text", 0, "earnings_date_text", "earnings"),
    _spec("peer_group", "text", 0, "peer_group_key", "scores"),
    _spec("peer_n", "count", 0, "peer_n", "scores"),
    _spec("runway_months", "ratio", 0.05, "runway_months", "runway"),
    _spec("runway_quarters", "count", 0, _quarters, "runway"),
    Spec("runway_rating", "text", 0, _rating, "runway"),
    _spec("ct_total_studies", "count", 0, "total_studies", "trials"),
    _spec("ct_active_phase_2", "count", 0, "active_phase_2", "trials"),
    _spec("ct_active_phase_3", "count", 0, "active_phase_3", "trials"),
    _spec("ct_latest_trial_nct", "text", 0, "latest_trial_nct", "trials"),
    _spec("ct_latest_trial_status", "text", 0, "latest_trial_status", "trials"),
    _spec(
        "catalyst_clinical_date",
        "text",
        0,
        lambda r: r.clinical_date.isoformat() if r.clinical_date else None,
        "catalyst",
    ),
    _spec("catalyst_clinical_overdue", "count", 0, "clinical_overdue", "catalyst"),
)

RUNWAY_DEPENDENT = {"cash_health", "composite_score"}
CATALYST_DEPENDENT = {"milestone_proximity", "composite_score"}


def different(spec: Spec, old: Any, new: Any) -> bool:
    if old is None and new is None:
        return False
    if old is None or new is None:
        return True
    if spec.name == "runway_rating":
        old = _old_rating(old)
    if spec.kind in ("score", "ratio"):
        return abs(float(old) - float(new)) > spec.tol
    if spec.kind == "count":
        return float(old) != float(new)
    return str(old).strip() != str(new).strip()


# ---------------------------------------------------------------------------- classification


def _state(row: CompanyPage, name: str) -> str:
    return str(((row.score_input_resolution or {}).get(name) or {}).get("state", ""))


def classify(
    spec: Spec,
    row: CompanyPage,
    old_row: Mapping[str, Any],
    known: set[str],
    differing: set[str],
) -> str:
    """The class of one difference. ``differing`` holds the columns that differ for this company."""
    ticker = (row.ticker or "").upper()
    if f"{ticker}:{spec.name}" in known or f"*:{spec.name}" in known:
        return QUIRK
    carried = {"carried", "expired"}
    if _state(row, "runway") in carried and (
        spec.group == "runway" or spec.name in RUNWAY_DEPENDENT
    ):
        return CARRIED
    if _state(row, "catalyst") in carried and (
        spec.group == "catalyst" or spec.name in CATALYST_DEPENDENT
    ):
        return CARRIED
    if spec.group in ("catalyst", "trials", "earnings"):
        return TIMING
    if spec.name == "milestone_proximity":
        # Days to the catalyst and to earnings differ by about 13.5 hours between the two systems,
        # which moves a milestone score a little. A larger gap is not a clock effect.
        limit = float(get_setting("parallel.timing_milestone_max_delta", TIMING_MILESTONE_DELTA))
        was, now = old_row.get(spec.name), spec.ours(row)
        if was is not None and now is not None and abs(float(was) - float(now)) <= limit:
            return TIMING
        return UNEXPLAINED
    if spec.name == "composite_score":
        others = differing & {"cash_health", "outlier_score", "divergence_recency"}
        if not others and "milestone_proximity" in differing:
            milestone_spec = next(x for x in SCORE_SPECS if x.name == "milestone_proximity")
            return classify(milestone_spec, row, old_row, known, differing)
    if spec.group == "runway":
        old_day = str(old_row.get("runway_as_of") or "")[:10]
        ours = row.runway_calc_date.isoformat() if row.runway_calc_date else ""
        if old_day and ours and old_day != ours:
            return TIMING
    if spec.name == "cash_health":
        old_day = str(old_row.get("runway_as_of") or "")[:10]
        ours = row.runway_calc_date.isoformat() if row.runway_calc_date else ""
        if old_day and ours and old_day != ours:
            return TIMING
    return UNEXPLAINED


# ------------------------------------------------------------------------------------ the day


def compare_day(
    day: date, old: Mapping[str, Mapping[str, Any]], rows: Iterable[CompanyPage] | None = None
) -> dict[str, Any]:
    """The report for ``day``: ``old`` is the export by ticker, ``rows`` the read-model rows."""
    pages = list(rows if rows is not None else CompanyPage.objects.filter(as_of_date=day))
    ours = {(p.ticker or "").upper(): p for p in pages if p.ticker}
    known = {str(k) for k in (get_setting("parallel.known_quirk_fixes", []) or [])}
    only_old = sorted(set(old) - set(ours))
    only_new = sorted(set(ours) - set(old))
    score_diffs: list[dict[str, Any]] = []
    field_diffs: list[dict[str, Any]] = []
    by_class = dict.fromkeys(CLASSES, 0)
    within = 0
    compared = 0
    for ticker in sorted(set(old) & set(ours)):
        compared += 1
        row, old_row = ours[ticker], old[ticker]
        differing = {
            s.name
            for s in (*SCORE_SPECS, *FIELD_SPECS)
            if different(s, old_row.get(s.name), s.ours(row))
        }
        if not any(s.name in differing and s.kind == "score" for s in SCORE_SPECS):
            within += 1
        for spec in (*SCORE_SPECS, *FIELD_SPECS):
            if spec.name not in differing:
                continue
            was, now = old_row.get(spec.name), spec.ours(row)
            kind = classify(spec, row, old_row, known, differing)
            by_class[kind] += 1
            entry = {
                "ticker": ticker,
                "field": spec.name,
                "old": was,
                "new": now,
                "class": kind,
            }
            if isinstance(was, float) and isinstance(now, (int, float)) and spec.kind == "score":
                entry["delta"] = round(float(now) - was, 4)
            (score_diffs if spec in SCORE_SPECS else field_diffs).append(entry)
    return {
        "day": day.isoformat(),
        "export_companies": len(old),
        "our_companies": len(ours),
        "compared": compared,
        "only_in_export": only_old[:200],
        "only_here": only_new[:200],
        "only_in_export_n": len(only_old),
        "only_here_n": len(only_new),
        "within_tolerance": within,
        "share_within_tolerance": round(within / len(old), 4) if old else 0.0,
        "by_class": by_class,
        "score_diffs": score_diffs[:MAX_DIFFS_STORED],
        "field_diffs": field_diffs[:MAX_DIFFS_STORED],
        "not_compared": NOT_COMPARED,
    }


NOT_COMPARED = (
    "Short volume, insider activity, margins and the trial lists are not in the export; "
    "compare them by hand."
)


def save_report(day: date, report: dict[str, Any]) -> None:
    ParallelDiff.objects.update_or_create(day=day, defaults={"report": report})


# ------------------------------------------------------------------------- the pass condition


def trading_window(end: date, length: int = WINDOW_DAYS) -> list[date]:
    days: list[date] = []
    day = end
    while len(days) < length:
        if is_trading_day(day):
            days.append(day)
        day -= timedelta(days=1)
    return sorted(days)


def evaluate_pass(
    reports: Mapping[date, Mapping[str, Any]], end: date | None = None
) -> dict[str, Any]:
    """The acceptance rule over the 14 trading days ending at ``end`` (default: the latest)."""
    if not reports:
        return {"passed": False, "reason": "no reports yet", "days_with_reports": 0}
    last = end or max(reports)
    window = trading_window(last)
    have = [d for d in window if d in reports]
    missing = [d.isoformat() for d in window if d not in reports]
    good = [d for d in have if reports[d]["share_within_tolerance"] >= GOOD_DAY_SHARE]
    unexplained_days: dict[tuple[str, str], int] = {}
    for d in have:
        seen = {
            (e["ticker"], e["field"])
            for e in (*reports[d]["score_diffs"], *reports[d]["field_diffs"])
            if e["class"] == UNEXPLAINED
        }
        for key in seen:
            unexplained_days[key] = unexplained_days.get(key, 0) + 1
    too_long = sorted(k for k, n in unexplained_days.items() if n > MAX_UNEXPLAINED_DAYS)
    all_classified = all(
        e["class"] in CLASSES
        for d in have
        for e in (*reports[d]["score_diffs"], *reports[d]["field_diffs"])
    )
    checks = {
        "fourteen_days_reported": not missing,
        "good_days_at_least_12": len(good) >= MIN_GOOD_DAYS,
        "no_difference_unexplained_over_2_days": not too_long,
        "every_difference_classified": all_classified,
    }
    return {
        "passed": all(checks.values()),
        "window": [d.isoformat() for d in window],
        "days_with_reports": len(have),
        "missing_days": missing,
        "good_days": len(good),
        "good_day_share_required": GOOD_DAY_SHARE,
        "unexplained_more_than_2_days": [f"{t}:{f}" for t, f in too_long],
        "checks": checks,
    }


def checklist(result: Mapping[str, Any]) -> str:
    """The sign-off checklist as text, with the result of each check."""
    checks = result.get("checks", {})
    window = result.get("window") or ["-"]
    missing = result.get("missing_days") or []
    open_items = result.get("unexplained_more_than_2_days") or []

    def line(key: str, text: str) -> str:
        return f"{'[x]' if checks.get(key) else '[ ]'} {text}"

    return "\n".join(
        [
            "OxHouse parallel run: sign-off checklist",
            f"Window: {window[0]} to {window[-1]}",
            "",
            line(
                "fourteen_days_reported",
                "The diff report ran on all 14 trading days"
                + (f" (missing: {', '.join(missing)})" if missing else ""),
            ),
            line(
                "good_days_at_least_12",
                "At least 99% of companies are within tolerance on at least 12 days "
                f"(met on {result.get('good_days', 0)})",
            ),
            line(
                "no_difference_unexplained_over_2_days",
                "No difference is unexplained for more than 2 days"
                + (f" (open: {', '.join(open_items[:10])})" if open_items else ""),
            ),
            line("every_difference_classified", "Every remaining difference is classified"),
            "",
            f"Result: {'PASS' if result.get('passed') else 'NOT YET'}",
            "",
            "Product owner: ____________________   Date: __________",
            "Engineer:      ____________________   Date: __________",
        ]
    )
