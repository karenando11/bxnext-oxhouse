"""Plain factory functions that build one valid object of each model (T5.6)."""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from apps.companies.models import Company, CompanySponsorAlias, TickerHistory
from apps.marketdata.models import FinvizSnapshot, ShortVolumeDaily
from apps.platform.models import DataQualityCheck, PipelineRun, Setting, SourceCall
from apps.scoring.models import FormulaVersion, ScoreResult, ScoreRun
from apps.screener.models import FunnelEvent, ScreenDefinition, ScreenMember, ScreenRun
from apps.sec.models import (
    RunwayCalc,
    SecFact,
    SecFiling,
    SecForm4Filing,
    SecForm4Transaction,
)
from apps.trials.models import CatalystSignal, CtStudyVersion

DAY = date(2026, 10, 12)
NOW = datetime(2026, 10, 12, 20, 30, tzinfo=UTC)
_counter = itertools.count(1)


def _n() -> int:
    return next(_counter)


def pipeline_run(**kw: Any) -> PipelineRun:
    return PipelineRun.objects.create(
        **{"stage": "market", "as_of_date": DAY, "trigger": "manual", "started_at": NOW, **kw}
    )


def source_call(**kw: Any) -> SourceCall:
    return SourceCall.objects.create(
        **{"feed": "finviz_elite", "requested_at": NOW, "status": "ok", **kw}
    )


def data_quality_check(**kw: Any) -> DataQualityCheck:
    run = kw.pop("run", None) or pipeline_run()
    return DataQualityCheck.objects.create(
        **{"run": run, "check_name": "blank_column", "status": "pass", **kw}
    )


def setting(**kw: Any) -> Setting:
    return Setting.objects.create(**{"key": f"test.key.{_n()}", "value": 1, **kw})


def company(**kw: Any) -> Company:
    n = _n()
    return Company.objects.create(
        **{
            "name": f"Company {n}",
            "industry": "Biotechnology",
            "cik": f"{n:010d}",
            "first_seen_date": DAY,
            "last_seen_date": DAY,
            **kw,
        }
    )


def ticker(company_: Company | None = None, **kw: Any) -> TickerHistory:
    return TickerHistory.objects.create(
        **{
            "company": company_ or company(),
            "ticker": f"T{_n()}",
            "valid_from": DAY,
            **kw,
        }
    )


def sponsor_alias(company_: Company | None = None, **kw: Any) -> CompanySponsorAlias:
    return CompanySponsorAlias.objects.create(
        **{
            "company": company_ or company(),
            "sponsor_name": "Example Therapeutics",
            "source": "derived",
            **kw,
        }
    )


def snapshot(company_: Company | None = None, **kw: Any) -> FinvizSnapshot:
    return FinvizSnapshot.objects.create(
        **{"company": company_ or company(), "as_of_date": DAY, "price": 5.39, **kw}
    )


def short_volume(company_: Company | None, trade_date: date, short: int, total: int, **kw: Any):
    return ShortVolumeDaily.objects.create(
        **{
            "ticker_raw": f"S{company_.pk if company_ else 'X'}",
            "company": company_,
            "trade_date": trade_date,
            "short_volume": short,
            "total_volume": total,
            "market": "CNMS",
            **kw,
        }
    )


def sec_filing(company_: Company | None = None, **kw: Any) -> SecFiling:
    return SecFiling.objects.create(
        **{
            "company": company_ or company(),
            "accession": f"0000000000-26-{_n():06d}",
            "form": "10-Q",
            "filed_at": DAY,
            **kw,
        }
    )


def sec_fact(company_: Company | None = None, **kw: Any) -> SecFact:
    return SecFact.objects.create(
        **{
            "company": company_ or company(),
            "taxonomy": "us-gaap",
            "tag": "CashAndCashEquivalentsAtCarryingValue",
            "unit": "USD",
            "period_end": DAY,
            "value": Decimal("1000000"),
            "accession": "0000000000-26-000001",
            "filed_at": DAY,
            **kw,
        }
    )


def runway_calc(company_: Company | None = None, **kw: Any) -> RunwayCalc:
    return RunwayCalc.objects.create(
        **{
            "company": company_ or company(),
            "calc_date": DAY,
            "method_version": "v1",
            "status": "ok",
            "runway_months": 20.0,
            **kw,
        }
    )


def form4_filing(company_: Company | None = None, **kw: Any) -> SecForm4Filing:
    return SecForm4Filing.objects.create(
        **{
            "company": company_ or company(),
            "accession": f"0000000001-26-{_n():06d}",
            "form": "4",
            "filed_at": DAY,
            "parse_status": "ok",
            **kw,
        }
    )


def form4_transaction(filing: SecForm4Filing | None = None, **kw: Any) -> SecForm4Transaction:
    shares = kw.pop("shares", Decimal("100"))
    price = kw.pop("price_per_share", Decimal("5"))
    return SecForm4Transaction.objects.create(
        **{
            "filing": filing or form4_filing(),
            "line_no": 1,
            "reporting_owner_cik": "0000000099",
            "transaction_date": DAY,
            "transaction_code": "P",
            "shares": shares,
            "price_per_share": price,
            "amount_usd": None if shares is None or price is None else shares * price,
            **kw,
        }
    )


def ct_study(company_: Company | None = None, **kw: Any) -> CtStudyVersion:
    return CtStudyVersion.objects.create(
        **{
            "nct_id": f"NCT{_n():08d}",
            "company": company_ or company(),
            "observed_on": DAY,
            "content_hash": f"hash{_n()}",
            **kw,
        }
    )


def catalyst_signal(company_: Company | None = None, **kw: Any) -> CatalystSignal:
    return CatalystSignal.objects.create(
        **{"company": company_ or company(), "as_of_date": DAY, "rule_version": "v0", **kw}
    )


def formula_version(**kw: Any) -> FormulaVersion:
    return FormulaVersion.objects.create(
        **{
            "name": f"test{_n()}",
            "version": 1,
            "effective_from": NOW,
            "config": {"a": 1},
            "quirks": {"b": True},
            **kw,
        }
    )


def score_run(**kw: Any) -> ScoreRun:
    return ScoreRun.objects.create(
        **{
            "as_of_date": DAY,
            "scored_at": NOW,
            "formula_version": kw.pop("formula_version", None) or formula_version(),
            "finviz_as_of_date": DAY,
            "status": "complete",
            **kw,
        }
    )


def score_result(run: ScoreRun | None = None, company_: Company | None = None, **kw: Any):
    return ScoreResult.objects.create(
        **{
            "score_run": run or score_run(),
            "company": company_ or company(),
            "mcap_bucket": "Micro",
            "peer_group_key": "Biotechnology|Micro",
            "peer_n": 20,
            "is_sparse": False,
            "cash_health": 5.0,
            "milestone": 5.0,
            "outlier": 5.0,
            "divergence": 0.0,
            "recency": 0.5,
            "composite": 5.0,
            "inputs": {},
            "components": {},
            **kw,
        }
    )


def screen_definition(**kw: Any) -> ScreenDefinition:
    return ScreenDefinition.objects.create(
        **{"name": f"Screen {_n()}", "config": {"composite_floor": 5}, **kw}
    )


def screen_run(**kw: Any) -> ScreenRun:
    return ScreenRun.objects.create(
        **{
            "score_run": kw.pop("score_run", None) or score_run(),
            "definition": kw.pop("definition", None) or screen_definition(),
            "filter_config": {},
            "universe_n": 1,
            "passed_n": 1,
            "executed_at": NOW,
            **kw,
        }
    )


def screen_member(**kw: Any) -> ScreenMember:
    return ScreenMember.objects.create(
        **{
            "screen_run": kw.pop("screen_run", None) or screen_run(),
            "company": kw.pop("company", None) or company(),
            "passed": True,
            **kw,
        }
    )


def funnel_event(**kw: Any) -> FunnelEvent:
    return FunnelEvent.objects.create(
        **{
            "company": kw.pop("company", None) or company(),
            "as_of_date": DAY,
            "from_state": "excluded",
            "to_state": "passed",
            **kw,
        }
    )


def days_ago(days: int, base: date = DAY) -> date:
    return base - timedelta(days=days)
