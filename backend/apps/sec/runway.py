"""The runway calculator (Step 12). Pure: no database, no network.

Inputs are the allow-listed facts of one company as they stood on a day, and the count of active
trials per phase. The output is everything that is stored on ``RunwayCalc``.

    runway_months = cash / (operating burn + trial burn), capped at 360 months.

Defaults chosen where the requirements leave a choice (see the Step 12 open items):
* Cash is the latest balance-sheet cash fact; short-term investments are added only when they
  carry the same date.
* Operating burn is the annual operating cash outflow divided by 12. A company with no annual
  figure uses its latest year-to-date quarterly figure divided by the months it covers (or, with
  the parity switch ``ocf_ytd_naive_sum`` on, the old naive sum of every quarterly fact of that
  fiscal year, divided by the same months, which counts cumulative figures twice).
* With no operating cash flow at all, burn is annual R&D expense divided by 12.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from apps.sec.parsing import ALLOWED_TAGS

METHOD_VERSION = "runway-v1"
RUNWAY_CAP_MONTHS = 360.0
DAYS_PER_MONTH = Decimal("30.44")

DEFAULT_TRIAL_RATES: dict[str, int] = {
    "phase1": 150_000,
    "phase2": 800_000,
    "phase3": 2_500_000,
    "other": 100_000,
}
PHASE_ORDER = ("phase1", "phase2", "phase3", "other")

# Tags in the order they are preferred when several carry the same date.
CASH_TAGS = {
    "us-gaap": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "Cash",
    ),
    "ifrs-full": ("CashAndCashEquivalents",),
}
INVESTMENT_TAGS = {
    "us-gaap": (
        "ShortTermInvestments",
        "MarketableSecuritiesCurrent",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
    ),
    "ifrs-full": ("OtherCurrentFinancialAssets",),
}
OCF_TAGS = {
    "us-gaap": ("NetCashProvidedByUsedInOperatingActivities",),
    "ifrs-full": ("CashFlowsFromUsedInOperatingActivities",),
}
RD_TAGS = {
    "us-gaap": (
        "ResearchAndDevelopmentExpense",
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
    ),
    "ifrs-full": ("ResearchAndDevelopmentExpense",),
}

ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "20-F", "20-F/A"})
ANNUAL_DAYS = (330, 400)

# Every tag used here must be on the collector's allow-list.
for _group in (CASH_TAGS, INVESTMENT_TAGS, OCF_TAGS, RD_TAGS):
    for _taxonomy, _tags in _group.items():
        assert set(_tags) <= ALLOWED_TAGS[_taxonomy], (_taxonomy, _tags)


@dataclass(frozen=True)
class Fact:
    id: int | None
    taxonomy: str
    tag: str
    period_start: date | None
    period_end: date
    value: Decimal
    form: str
    filed_at: date
    accession: str = ""

    @property
    def days(self) -> int | None:
        return None if self.period_start is None else (self.period_end - self.period_start).days


@dataclass(frozen=True)
class TrialLine:
    phase: str
    active_trial_count: int
    monthly_rate_usd: Decimal
    amount_usd: Decimal

    def as_json(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "active_trial_count": self.active_trial_count,
            "monthly_rate_usd": str(self.monthly_rate_usd),
            "amount_usd": str(self.amount_usd),
        }


@dataclass
class RunwayResult:
    status: str = "unavailable"  # ok, partial, unavailable
    reason_codes: list[str] = field(default_factory=list)
    cash_usd: Decimal | None = None
    cash_tag: str = ""
    cash_end_date: date | None = None
    ocf_usd: Decimal | None = None
    ocf_tag: str = ""
    ocf_period_start: date | None = None
    ocf_period_end: date | None = None
    operating_burn_monthly_usd: Decimal | None = None
    trial_burn_monthly_usd: Decimal | None = None
    burn_total_monthly_usd: Decimal | None = None
    burn_source: str = ""
    runway_months: float | None = None
    runway_capped: bool = False
    cap_reason: str = ""
    financing_date: date | None = None
    source_fact_ids: list[int] = field(default_factory=list)
    trial_burn_lines: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""


def months_between(start: date, end: date) -> int:
    """Whole months a period covers, to the nearest month."""
    return max(1, round((end - start).days / float(DAYS_PER_MONTH)))


def trial_burn(
    counts: dict[str, int], rates: dict[str, int] | None = None
) -> tuple[Decimal, list[TrialLine]]:
    """Monthly trial burn and one line per phase that has active trials."""
    rates = {**DEFAULT_TRIAL_RATES, **(rates or {})}
    lines = []
    for phase in PHASE_ORDER:
        count = counts.get(phase, 0)
        if count > 0:
            rate = Decimal(rates[phase])
            lines.append(TrialLine(phase, count, rate, rate * count))
    return sum((line.amount_usd for line in lines), Decimal(0)), lines


def _latest_instant(facts: list[Fact], tags: tuple[str, ...]) -> Fact | None:
    """The newest balance-sheet fact; ties go to the earlier tag, then the later filing."""
    best: Fact | None = None
    for fact in facts:
        if fact.tag not in tags or fact.period_start is not None:
            continue
        if best is None:
            best = fact
            continue
        key = (fact.period_end, -tags.index(fact.tag), fact.filed_at)
        if key > (best.period_end, -tags.index(best.tag), best.filed_at):
            best = fact
    return best


def _pick_taxonomy(facts: list[Fact]) -> str:
    return "us-gaap" if any(f.taxonomy == "us-gaap" for f in facts) else "ifrs-full"


def _cash(facts: list[Fact], tax: str) -> tuple[Decimal, str, date, list[int]] | None:
    cash = _latest_instant(facts, CASH_TAGS[tax])
    if cash is None:
        return None
    total, tag = cash.value, cash.tag
    ids = [cash.id] if cash.id is not None else []
    same_day = [f for f in facts if f.period_end == cash.period_end]
    investment = _latest_instant(same_day, INVESTMENT_TAGS[tax])
    if investment is not None:
        total += investment.value
        tag = f"{cash.tag}+{investment.tag}"
        if investment.id is not None:
            ids.append(investment.id)
    return total, tag, cash.period_end, ids


def _annual(facts: list[Fact], tags: tuple[str, ...]) -> Fact | None:
    """The newest full-year fact of a filing on an annual form."""
    low, high = ANNUAL_DAYS
    best: Fact | None = None
    for fact in facts:
        if fact.tag not in tags or fact.days is None or not low <= fact.days <= high:
            continue
        if fact.form not in ANNUAL_FORMS:
            continue
        if best is None or (fact.period_end, fact.filed_at) > (best.period_end, best.filed_at):
            best = fact
    return best


def _quarterly_burn(
    facts: list[Fact], tags: tuple[str, ...], *, naive: bool
) -> tuple[Decimal, list[Fact], date, date] | None:
    """Operating cash flow and its span from the quarterly filings of the latest fiscal year."""
    quarterly = [
        f
        for f in facts
        if f.tag in tags
        and f.days is not None
        and f.days < ANNUAL_DAYS[0]
        and f.form.startswith("10-Q")
    ]
    if not quarterly:
        return None
    latest_end = max(f.period_end for f in quarterly)
    # Facts that belong to the latest fiscal year: they end in the same 12 months as the newest.
    year = [f for f in quarterly if (latest_end - f.period_end).days < 300]
    if not year:
        return None
    anchor = max(year, key=lambda f: (f.period_end, f.days or 0, f.filed_at))
    start = min(
        f.period_start
        for f in year
        if f.period_start is not None and f.period_end <= anchor.period_end
    )
    if naive:
        # The old bug: cumulative figures are summed as if they were separate quarters.
        distinct = {(f.period_start, f.period_end): f for f in year}
        return (
            sum((f.value for f in distinct.values()), Decimal(0)),
            list(distinct.values()),
            start,
            anchor.period_end,
        )
    # De-cumulated: the newest year-to-date figure already holds every discrete quarter before it.
    ytd = [f for f in year if f.period_end == anchor.period_end]
    longest = max(ytd, key=lambda f: (f.days or 0, f.filed_at))
    assert longest.period_start is not None
    return longest.value, [longest], longest.period_start, longest.period_end


def calculate_runway(
    facts: list[Fact],
    trial_counts: dict[str, int] | None = None,
    *,
    rates: dict[str, int] | None = None,
    ocf_ytd_naive_sum: bool = True,
    trial_burn_on_profitable: bool = True,
    reason_without_facts: str | None = None,
) -> RunwayResult:
    """One company's runway. ``reason_without_facts`` says why there are no facts, if known."""
    result = RunwayResult()
    reasons: set[str] = set()
    notes: list[str] = []
    trial_total, lines = trial_burn(trial_counts or {}, rates)
    result.trial_burn_lines = [line.as_json() for line in lines]
    if not lines:
        notes.append("no_active_trials")

    if not facts:
        reasons.update({"cash_missing", "operating_cash_flow_missing"})
        if reason_without_facts:
            reasons.add(reason_without_facts)
        if lines:
            reasons.add("phase_burn_only_no_xbrl")
            result.trial_burn_monthly_usd = trial_total
            result.burn_source = "phase_burn_only"
            result.status = "partial"
        return _finish(result, reasons, notes)

    tax = _pick_taxonomy(facts)
    ids: list[int] = []

    cash = _cash(facts, tax)
    if cash is None:
        reasons.add("cash_missing")
    else:
        result.cash_usd, result.cash_tag, result.cash_end_date, cash_ids = cash
        ids += cash_ids

    operating: Decimal | None = None
    annual = _annual(facts, OCF_TAGS[tax])
    ocf_facts: list[Fact] = []
    ocf_value: Decimal | None
    start: date | None
    end: date | None
    if annual is not None:
        ocf_facts = [annual]
        ocf_value, start, end = annual.value, annual.period_start, annual.period_end
        months = 12
    else:
        quarter = _quarterly_burn(facts, OCF_TAGS[tax], naive=ocf_ytd_naive_sum)
        if quarter is None:
            ocf_value = start = end = None
            months = 0
        else:
            ocf_value, ocf_facts, start, end = quarter
            months = months_between(start, end)
    if ocf_value is not None and start is not None and end is not None:
        result.ocf_usd = ocf_value
        result.ocf_tag = ocf_facts[0].tag
        result.ocf_period_start, result.ocf_period_end = start, end
        ids += [f.id for f in ocf_facts if f.id is not None]
        result.burn_source = "operating_cash_flow"
        if ocf_value >= 0:
            operating = Decimal(0)
            reasons.add("company_cash_positive")
        else:
            operating = (-ocf_value / months).quantize(Decimal("0.01"))
    else:
        reasons.add("operating_cash_flow_missing")
        rd = _annual(facts, RD_TAGS[tax])
        if rd is not None:
            operating = (abs(rd.value) / 12).quantize(Decimal("0.01"))
            result.burn_source = "rd_expense"
            reasons.add("fallback_rd_expense")
            if rd.id is not None:
                ids.append(rd.id)

    result.operating_burn_monthly_usd = operating
    positive = "company_cash_positive" in reasons
    if lines and not (positive and not trial_burn_on_profitable):
        result.trial_burn_monthly_usd = trial_total
    elif lines:
        result.trial_burn_monthly_usd = Decimal(0)
    elif operating is not None:
        result.trial_burn_monthly_usd = Decimal(0)
    if operating is not None or result.trial_burn_monthly_usd is not None:
        result.burn_total_monthly_usd = (operating or Decimal(0)) + (
            result.trial_burn_monthly_usd or Decimal(0)
        )
        if operating is None and lines:
            result.burn_source = "phase_burn_only"
    result.source_fact_ids = sorted(set(ids))
    _derive_runway(result)
    return _finish(result, reasons, notes)


def runway_for(cash: Decimal, burn: Decimal) -> tuple[float, bool, str]:
    """Months of runway, whether it was capped at 360, and why (zero_burn or tiny_burn)."""
    if burn <= 0:
        return RUNWAY_CAP_MONTHS, True, "zero_burn"
    months = float(max(cash, Decimal(0)) / burn)
    if months > RUNWAY_CAP_MONTHS:
        return RUNWAY_CAP_MONTHS, True, "tiny_burn"
    return months, False, ""


def _derive_runway(result: RunwayResult) -> None:
    if result.cash_usd is None or result.burn_total_monthly_usd is None:
        return
    months, capped, why = runway_for(result.cash_usd, result.burn_total_monthly_usd)
    result.runway_months = months
    result.runway_capped = capped
    result.cap_reason = why
    if result.cash_end_date is not None:
        result.financing_date = result.cash_end_date + timedelta(
            days=round(Decimal(str(months)) * DAYS_PER_MONTH)
        )


def _finish(result: RunwayResult, reasons: set[str], notes: list[str]) -> RunwayResult:
    result.reason_codes = sorted(reasons)
    result.notes = ";".join(sorted(reasons) + notes)
    if result.runway_months is not None:
        result.status = "ok" if not ({"cash_missing"} & reasons) else "partial"
    elif result.cash_usd is not None or result.burn_total_monthly_usd is not None:
        result.status = "partial"
    return result


# --- Display values: derived at read time, never stored -------------------------------------

CASH_FLOW_POSITIVE_LABEL = "cash-flow positive"


@dataclass(frozen=True)
class RunwayDisplay:
    months: float | None
    months_label: str
    quarters: float | None
    rating: str
    label: str


def runway_rating(months: float) -> str:
    if months < 6:
        return "Critical"
    if months < 12:
        return "Short"
    if months < 24:
        return "Moderate"
    return "Long"


def runway_display(
    months: float | None, *, capped: bool = False, cash_flow_positive: bool = False
) -> RunwayDisplay:
    """Quarters (one decimal, half up), rating, and the cash-flow-positive label."""
    if months is None:
        return RunwayDisplay(None, "", None, "", "")
    if cash_flow_positive:
        return RunwayDisplay(None, "", None, "", CASH_FLOW_POSITIVE_LABEL)
    quarters = float((Decimal(str(months)) / 3).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    label = f"{months:g}+" if capped else f"{months:.1f}"
    return RunwayDisplay(months, label, quarters, runway_rating(months), "")
