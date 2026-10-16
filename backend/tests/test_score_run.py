"""T7.1 to T7.6 (integration): scoring a stored day through the database."""

from __future__ import annotations

import csv
import importlib
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from django.apps import apps as django_apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, transaction

from apps.platform.golden import load_golden
from apps.scoring.models import FormulaVersion, ScoreResult, ScoreRun
from apps.scoring.service import (
    ScoreRunFailed,
    latest_complete_run,
    load_formula,
    run_scoring,
    stage_a_time,
)
from apps.sec.models import RunwayCalc
from apps.trials.models import CatalystSignal
from apps.web.models import CompanyPage
from scoring_engine import (
    DIMENSIONS,
    PARITY,
    CompanyPeerInput,
    compute_topology,
    display_score,
    round_half_up,
)
from tests import factories as f
from tests.test_golden_loader import GOLDEN_COLUMNS, golden_row, write_csv

DAY = date(2026, 10, 12)


def synthetic_file(tmp_path: Path, count: int = 12) -> Path:
    """A universe of ``count`` companies in one peer group with varied inputs."""
    rows = []
    for i in range(count):
        rows.append(
            golden_row(
                f"T{i:03d}",
                mcap="60",
                price=str(2 + i % 7),
                quick_ratio=str(1 + i / 5),
                cash_sh="1.5",
                td_eq="0.2",
                rel_vol=str(0.5 + i / 10),
                eps_next="25",
                perf_ytd=str(i),
                perf_m=str(i * 2),
                perf_w=str(i % 5),
                rsi=str(40 + i),
                inst_own=str(20 + i * 3),
                short_float=str(5 + i % 4),
                vol_m=str(1 + i) if i != 3 else "",
                avg_vol="100",
                runway_months=str(8 + i),
            )
        )
    return write_csv(tmp_path / "synthetic.csv", GOLDEN_COLUMNS, rows)


@pytest.fixture
def loaded(tmp_path: Path) -> Path:
    path = synthetic_file(tmp_path)
    load_golden(path)
    return path


def result_values(run: ScoreRun) -> list[Any]:
    return list(
        ScoreResult.objects.filter(score_run=run)
        .order_by("company_id")
        .values(
            "company_id", "mcap_bucket", "peer_group_key", "peer_n", "is_sparse", "cash_health",
            "milestone", "outlier", "divergence", "recency", "composite", "carried_inputs",
            "missing_inputs", "inputs", "components",
        )
    )  # fmt: skip


# ---------------------------------------------------------------------------- the basics


@pytest.mark.django_db
def test_a_day_is_scored_and_recorded(loaded: Path) -> None:
    run = run_scoring(DAY)
    assert run.status == "complete"
    assert (run.universe_n, run.scored_n) == (12, 12)
    assert ScoreResult.objects.filter(score_run=run).count() == 12
    assert (run.formula_version.name, run.formula_version.version) == ("parity", 1)
    assert run.finviz_as_of_date == DAY
    assert run.scored_at == datetime(2026, 10, 12, 20, 30, tzinfo=UTC)  # 16:30 US Eastern


def test_stage_a_time_follows_daylight_saving() -> None:
    assert stage_a_time(date(2026, 10, 12)) == datetime(2026, 10, 12, 20, 30, tzinfo=UTC)
    assert stage_a_time(date(2026, 12, 15)) == datetime(2026, 12, 15, 21, 30, tzinfo=UTC)


@pytest.mark.django_db
def test_the_run_links_to_the_loaders_pipeline_run(loaded: Path) -> None:
    run = run_scoring(DAY)
    assert run.pipeline_run is not None
    assert run.pipeline_run.code_ref == "fixture"
    assert CompanyPage.objects.filter(is_fixture=True).count() == 12


# ------------------------------------------------------------------ records what it used


@pytest.mark.django_db
def test_each_result_stores_inputs_components_and_links(loaded: Path) -> None:
    run = run_scoring(DAY)
    result = ScoreResult.objects.filter(score_run=run).order_by("company_id").first()
    assert result is not None
    assert result.inputs["price"] == 2.0
    assert result.inputs["quick_ratio"] == 1.0
    assert result.inputs["peer_metrics"]["perf_ytd"] == 0.0
    assert set(result.components) == {
        "topology", "cash_health", "milestone", "composite", "runway_used",
    }  # fmt: skip
    assert result.components["topology"]["peer_group_key"] == result.peer_group_key
    assert result.components["cash_health"]["terms"]
    assert result.components["milestone"]["terms"]
    runway = RunwayCalc.objects.get(company_id=result.company_id)
    catalyst = CatalystSignal.objects.get(company_id=result.company_id)
    assert (result.runway_calc_id, result.catalyst_signal_id) == (runway.pk, catalyst.pk)


@pytest.mark.django_db
def test_inputs_and_components_are_never_empty(loaded: Path) -> None:
    run = run_scoring(DAY)
    for result in ScoreResult.objects.filter(score_run=run):
        assert result.inputs
        assert result.components


@pytest.mark.django_db
def test_missing_inputs_are_listed(loaded: Path) -> None:
    run = run_scoring(DAY)
    results = {r.company_id: r for r in ScoreResult.objects.filter(score_run=run)}
    by_vol = [r for r in results.values() if "peer.vol_m" in r.missing_inputs]
    assert len(by_vol) == 1  # one company has no monthly volatility
    assert all("eps_this" in r.missing_inputs for r in results.values())  # blank in the export
    assert all(r.carried_inputs == [] for r in results.values())


@pytest.mark.django_db
def test_peer_group_stats_hold_median_deviation_and_count(loaded: Path) -> None:
    run = run_scoring(DAY)
    stats = run.peer_group_stats
    assert {s["dimension"] for s in stats} == set(DIMENSIONS)
    perf = next(s for s in stats if s["dimension"] == "perf_ytd")
    assert (perf["peer_group_key"], perf["n_values"]) == ("Biotechnology|Micro", 12)
    assert perf["median"] == 5.5  # values 0 to 11
    assert perf["mad"] == 3.0
    vol = next(s for s in stats if s["dimension"] == "vol_m")
    assert vol["n_values"] == 11  # one blank


@pytest.mark.django_db
def test_runway_and_catalyst_rows_after_the_day_are_ignored(loaded: Path) -> None:
    company = RunwayCalc.objects.first().company  # type: ignore[union-attr]
    f.runway_calc(company, calc_date=date(2026, 10, 20), runway_months=1.0)
    f.catalyst_signal(company, as_of_date=date(2026, 10, 20), rule_version="later")
    run = run_scoring(DAY)
    result = ScoreResult.objects.get(score_run=run, company=company)
    assert result.runway_calc.calc_date <= DAY  # type: ignore[union-attr]
    assert result.catalyst_signal.rule_version == "fixture"  # type: ignore[union-attr]


# ------------------------------------------------------------------------ deterministic


@pytest.mark.django_db
def test_two_runs_give_identical_results(loaded: Path) -> None:
    first = run_scoring(DAY)
    second = run_scoring(DAY)
    assert first.pk != second.pk
    assert result_values(first) == result_values(second)
    assert first.peer_group_stats == second.peer_group_stats


# ----------------------------------------------------------------------------- atomic


@pytest.mark.django_db(transaction=True)
def test_a_failure_leaves_no_partial_rows_and_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An earlier transactional test may have flushed the migration seeds; restore the formula.
    importlib.import_module("apps.scoring.migrations.0002_seed_parity_formula").seed(
        django_apps, None
    )
    load_golden(synthetic_file(tmp_path, count=1200))
    real = ScoreResult.objects.bulk_create
    calls = {"n": 0}

    def fail_on_the_second_batch(objs: Any, *args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected failure at row 500")
        return real(objs, *args, **kwargs)

    monkeypatch.setattr(ScoreResult.objects, "bulk_create", fail_on_the_second_batch)
    with pytest.raises(ScoreRunFailed) as raised:
        run_scoring(DAY)
    assert calls["n"] == 2
    assert ScoreResult.objects.count() == 0  # the first batch was rolled back
    run = ScoreRun.objects.get()
    assert run.pk == raised.value.run.pk
    assert run.status == "failed"
    assert "injected failure" in run.error_text
    assert latest_complete_run() is None
    assert CompanyPage.objects.count() == 0


@pytest.mark.django_db
def test_a_failed_run_does_not_supersede_a_completed_one(loaded: Path) -> None:
    good = run_scoring(DAY)
    with pytest.raises(ScoreRunFailed):
        run_scoring(DAY, finviz_as_of_date=date(2020, 1, 1))  # no snapshots for that date
    good.refresh_from_db()
    assert good.status == "complete"
    assert latest_complete_run(DAY) == good
    assert ScoreRun.objects.filter(status="failed").count() == 1


@pytest.mark.django_db
def test_an_empty_day_fails_with_a_clear_message() -> None:
    with pytest.raises(ScoreRunFailed, match="No Finviz snapshots"):
        run_scoring(DAY)
    assert ScoreRun.objects.get().error_text.startswith("ValueError")


# ---------------------------------------------------------------------- history is kept


@pytest.mark.django_db
def test_a_second_run_supersedes_the_first_and_readers_use_the_latest(loaded: Path) -> None:
    first = run_scoring(DAY)
    assert latest_complete_run() == first
    second = run_scoring(DAY)
    first.refresh_from_db()
    assert first.status == "superseded"
    assert second.status == "complete"
    assert ScoreResult.objects.filter(score_run=first).count() == 12  # kept
    assert latest_complete_run() == second
    assert latest_complete_run(DAY) == second
    assert set(CompanyPage.objects.values_list("score_run_id", flat=True)) == {second.pk}


@pytest.mark.django_db
def test_a_run_for_another_day_does_not_supersede(loaded: Path) -> None:
    first = run_scoring(DAY)
    other = date(2026, 10, 13)
    from apps.marketdata.models import FinvizSnapshot

    for snap in FinvizSnapshot.objects.all():
        snap.pk = None
        snap.as_of_date = other
        snap.save()
    second = run_scoring(other)
    first.refresh_from_db()
    assert first.status == "complete"
    assert latest_complete_run() == second


# ---------------------------------------------------------------------- formula versions


@pytest.mark.django_db
def test_the_formula_version_is_loaded_and_pinnable(loaded: Path) -> None:
    stored, engine = load_formula()
    assert (stored.name, stored.version) == ("parity", 1)
    assert engine == PARITY
    with pytest.raises(FormulaVersion.DoesNotExist):
        load_formula("nonexistent")
    with pytest.raises(FormulaVersion.DoesNotExist):
        load_formula("parity", 99)


@pytest.mark.django_db
def test_a_new_formula_version_changes_the_composite_and_the_old_one_is_frozen(
    loaded: Path,
) -> None:
    plain = PARITY.to_plain()
    plain["config"]["composite"]["cash_weight"] = 0.25
    FormulaVersion.objects.create(
        name="parity", version=2, effective_from=datetime.now(UTC),
        config=plain["config"], quirks=plain["quirks"],
    )  # fmt: skip
    old = run_scoring(DAY, formula_version=1)
    new = run_scoring(DAY)  # latest is version 2
    assert new.formula_version.version == 2
    a = ScoreResult.objects.filter(score_run=old).order_by("company_id").first()
    b = ScoreResult.objects.filter(score_run=new).order_by("company_id").first()
    assert a is not None and b is not None
    assert (a.cash_health, a.milestone, a.outlier) == (b.cash_health, b.milestone, b.outlier)
    assert b.composite == pytest.approx(a.composite - 0.05 * a.cash_health)
    frozen = FormulaVersion.objects.get(name="parity", version=1)
    frozen.config = {"changed": True}
    with pytest.raises(DatabaseError, match="create a new version"), transaction.atomic():
        frozen.save()


# ----------------------------------------------------------------------------- command


@pytest.mark.django_db
def test_the_score_day_command(loaded: Path, capsys: pytest.CaptureFixture[str]) -> None:
    call_command("score_day", "2026-10-12")
    assert "12 of 12 companies scored under parity v1" in capsys.readouterr().out
    with pytest.raises(CommandError, match="Not a date"):
        call_command("score_day", "yesterday")
    with pytest.raises(CommandError, match="failed"):
        call_command("score_day", "2026-01-01")


# ------------------------------------------------------------------------- golden data


def _golden(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture
def golden_run(golden_path: Path, runway_path: Path) -> tuple[ScoreRun, float]:
    load_golden(golden_path, runway_path)
    started = time.perf_counter()
    run = run_scoring(DAY)
    return run, time.perf_counter() - started


@pytest.mark.golden
@pytest.mark.django_db
def test_the_golden_day_reproduces_the_stored_scores(
    golden_path: Path, golden_run: tuple[ScoreRun, float]
) -> None:
    run, _ = golden_run
    by_ticker = {
        r.company.tickers.get().ticker: r
        for r in ScoreResult.objects.filter(score_run=run)
        .select_related("company")
        .prefetch_related("company__tickers")
    }
    rows = _golden(golden_path)
    assert len(by_ticker) == len(rows) == 1053
    wrong: dict[str, list[str]] = {
        "cash_health": [],
        "outlier": [],
        "divergence": [],
        "recency": [],
    }
    milestone_matches = composite_ok = 0
    for row in rows:
        r = by_ticker[row["ticker"]]
        if display_score("cash_health", r.cash_health) != float(row["cash_health"]):
            wrong["cash_health"].append(row["ticker"])
        if round_half_up(r.outlier) != float(row["outlier_score"]):
            wrong["outlier"].append(row["ticker"])
        if round_half_up(r.divergence) != float(row["divergence_direction"]):
            wrong["divergence"].append(row["ticker"])
        if r.recency != float(row["divergence_recency"]):
            wrong["recency"].append(row["ticker"])
        if display_score("milestone", r.milestone) == float(row["milestone_proximity"]):
            milestone_matches += 1  # the others depend on eps_this, which the export lacks
            if abs(r.composite - float(row["composite_score"])) <= 0.045:
                composite_ok += 1
    assert wrong == {"cash_health": [], "outlier": [], "divergence": [], "recency": []}
    assert milestone_matches >= 621
    assert composite_ok == milestone_matches


@pytest.mark.golden
@pytest.mark.django_db
def test_peer_group_stats_equal_the_engines_intermediate_values(
    golden_path: Path, golden_run: tuple[ScoreRun, float]
) -> None:
    run, _ = golden_run
    output = compute_topology(
        [
            CompanyPeerInput(
                row["ticker"],
                row["indication"] or None,
                float(row["mcap"]) if row["mcap"] else None,
                {d: float(row[d]) if row[d] else None for d in DIMENSIONS},
            )
            for row in _golden(golden_path)
        ]
    )
    expected = [
        {
            "peer_group_key": s.peer_group_key,
            "dimension": s.dimension,
            "n_values": s.n_values,
            "median": s.median,
            "mad": s.mad,
        }
        for s in output.group_stats
    ]
    assert run.peer_group_stats == expected


@pytest.mark.golden
@pytest.mark.django_db
def test_a_full_universe_run_takes_under_ten_seconds(
    golden_run: tuple[ScoreRun, float],
) -> None:
    _, seconds = golden_run
    assert seconds < 10


@pytest.mark.golden
@pytest.mark.django_db
def test_two_golden_runs_are_identical(golden_run: tuple[ScoreRun, float]) -> None:
    first, _ = golden_run
    second = run_scoring(DAY)
    assert result_values(first) == result_values(second)
