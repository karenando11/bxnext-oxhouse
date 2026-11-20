"""The real Finviz export (contract tests).

These need a real, unprocessed download saved in ``tests/fixtures/private/finviz_raw.csv`` (see
``manage.py finviz_contract_check --save``); they are skipped when it is absent. The file is
licensed data and is never committed. The tests check structure and plausibility, not particular
values, because the numbers change every day.
"""

from __future__ import annotations

import statistics
from datetime import date
from pathlib import Path

import pytest

from apps.marketdata.finviz.columns import COLUMNS, FIELDS
from apps.marketdata.finviz.ingest import ingest_finviz
from apps.marketdata.finviz.parser import ParsedExport, parse_export
from apps.marketdata.models import FinvizSnapshot
from apps.marketdata.storage import LocalRawStorage

pytestmark = pytest.mark.golden

DAY = date(2026, 11, 18)


@pytest.fixture(scope="module")
def parsed(finviz_raw_path: Path) -> ParsedExport:
    return parse_export(finviz_raw_path.read_bytes())


def values(parsed: ParsedExport, field: str) -> list[float]:
    return [r.values[field] for r in parsed.rows if r.values.get(field) is not None]


def test_every_column_of_the_map_is_found_in_the_real_file(parsed: ParsedExport) -> None:
    assert parsed.missing_fields == []
    assert len(parsed.mapped) == len(COLUMNS) == 50


def test_the_universe_is_about_a_thousand_healthcare_companies(parsed: ParsedExport) -> None:
    assert 900 <= parsed.row_count <= 1300
    assert parsed.duplicate_tickers == []
    assert parsed.skipped_rows == 0
    assert {r.values["sector_raw"] for r in parsed.rows} == {"Healthcare"}
    assert len({r.values["industry_raw"] for r in parsed.rows}) >= 8


def test_eps_this_is_populated(parsed: ParsedExport) -> None:
    assert len(values(parsed, "eps_this")) / parsed.row_count > 0.7


def test_no_mapped_column_is_blank_for_the_whole_universe(parsed: ParsedExport) -> None:
    blank = [f for f in FIELDS if f != "ticker" and not any(r.values.get(f) for r in parsed.rows)]
    assert blank == []


def test_every_numeric_cell_parses(parsed: ParsedExport) -> None:
    assert dict(parsed.bad_cells) == {}


def test_percent_distance_columns_have_the_expected_signs(parsed: ParsedExport) -> None:
    # The range lags the latest trading, so a few percent of names sit just beyond it.
    highs, lows = values(parsed, "w52h"), values(parsed, "w52l")
    assert sum(v > 0 for v in highs) / len(highs) < 0.10
    assert sum(v < 0 for v in lows) / len(lows) < 0.10
    assert min(highs) < -20  # a percent distance, so many names are far below their high
    assert max(lows) > 20


def test_price_to_cash_is_consistent_with_price_and_cash_per_share(parsed: ParsedExport) -> None:
    close = total = 0
    for row in parsed.rows:
        price, cash, pcash = (row.values.get(k) for k in ("price", "cash_sh", "pcash"))
        if price and cash and pcash and cash > 0:
            total += 1
            close += abs(price / cash - pcash) / pcash < 0.05
    assert total > 500
    assert close / total > 0.95


def test_units_are_plausible(parsed: ParsedExport) -> None:
    # market cap in USD millions: small caps in the tens to hundreds, the largest far above
    mcap = values(parsed, "mcap_musd")
    assert 10 < statistics.median(mcap) < 5000
    assert 10_000 < max(mcap) < 3_000_000
    # shares in millions, average volume in thousands of shares
    assert 5 < statistics.median(values(parsed, "shr_out_m")) < 500
    assert 10 < statistics.median(values(parsed, "avg_vol_k")) < 10_000
    # percent columns are plain numbers (12.4 means 12.4%), not fractions
    assert max(abs(v) for v in values(parsed, "perf_ytd")) > 5


def test_earnings_dates_are_kept_as_text_and_parsed_when_possible(parsed: ParsedExport) -> None:
    dated = [r for r in parsed.rows if r.values.get("earnings_date_text")]
    assert len(dated) / parsed.row_count > 0.8
    assert sum(r.values.get("earnings_date") is not None for r in dated) / len(dated) > 0.95


@pytest.mark.django_db
def test_the_real_file_loads_with_every_check_passing(
    finviz_raw_path: Path, parsed: ParsedExport, tmp_path: Path
) -> None:
    result = ingest_finviz(
        DAY, content=finviz_raw_path.read_bytes(), storage=LocalRawStorage(tmp_path / "raw")
    )
    assert result.rows == parsed.row_count
    assert result.new_companies == parsed.row_count
    assert FinvizSnapshot.objects.filter(as_of_date=DAY).count() == parsed.row_count
    assert [(c.check_name, c.scope) for c in result.failed_checks] == []
    assert result.run.status == "ok"
    again = ingest_finviz(
        DAY, content=finviz_raw_path.read_bytes(), storage=LocalRawStorage(tmp_path / "raw")
    )
    assert again.unchanged is True
    assert FinvizSnapshot.objects.count() == parsed.row_count
