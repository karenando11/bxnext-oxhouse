"""Fixtures for the private (licensed) export files. They are never committed."""

from __future__ import annotations

import csv
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest

PRIVATE = Path(__file__).resolve().parent / "fixtures" / "private"


def _private_file(env_var: str, default_name: str) -> Path:
    path = Path(os.environ.get(env_var, PRIVATE / default_name))
    if not path.exists():
        pytest.skip(f"{path} not found. Place it in tests/fixtures/private/ or set {env_var}.")
    return path


@pytest.fixture(scope="session")
def golden_path() -> Path:
    return _private_file("OXHOUSE_GOLDEN_EXPORT", "golden_export_2026-10-12.csv")


@pytest.fixture(scope="session")
def runway_path() -> Path:
    return _private_file("OXHOUSE_RUNWAY_EXPORT", "edgar_runway_export_2026-10-14.csv")


GOLDEN_DAY = date(2026, 10, 12)


@pytest.fixture
def golden_day(golden_path: Path, runway_path: Path) -> tuple[Any, list[dict[str, str]]]:
    """The golden day loaded and scored, with the stored composite put back into each result.

    The export has ``eps_this`` blank, so our own composite differs slightly from the stored one
    for some rows. Tests that check counts use the stored composite so the filter logic is checked
    against the numbers the existing system produced.
    """
    from apps.platform.golden import load_golden
    from apps.scoring.models import ScoreResult
    from apps.scoring.service import run_scoring

    load_golden(golden_path, runway_path)
    run = run_scoring(GOLDEN_DAY)
    with golden_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    stored = {r["ticker"]: float(r["composite_score"]) for r in rows}
    results = list(ScoreResult.objects.filter(score_run=run).prefetch_related("company__tickers"))
    for result in results:
        result.composite = stored[result.company.tickers.get().ticker]
    ScoreResult.objects.bulk_update(results, ["composite"])
    return run, rows
