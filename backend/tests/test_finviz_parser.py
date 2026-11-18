"""T10.1, T10.2 (unit): parsing a Finviz export by position."""

from __future__ import annotations

import csv
import io
from datetime import date

import pytest

from apps.marketdata.finviz.columns import (
    COLUMNS,
    EXPECTED_COLUMN_COUNT,
    FIELDS,
    NUMERIC_FIELDS,
    header_lookup,
    normalise_header,
)
from apps.marketdata.finviz.parser import (
    ExportFormatError,
    parse_earnings_date,
    parse_export,
    parse_number,
)
from tests.finviz_samples import HEADERS, company_row, export_csv, universe

# ----------------------------------------------------------------------- T10.1 cell parsing


@pytest.mark.parametrize(
    ("raw", "unit", "stored"),
    [
        ("12.4%", "percent", 12.4),
        ("-12.4%", "percent", -12.4),
        ("-", "plain", None),
        ("--", "plain", None),
        ("N/A", "plain", None),
        ("n/a", "plain", None),
        ("", "plain", None),
        ("   ", "plain", None),
        (None, "plain", None),
        ("$1,234.5M", "millions", 1234.5),
        ("1.2B", "millions", 1200.0),
        ("500K", "millions", 0.5),
        ("2T", "millions", 2_000_000.0),
        ("223.23K", "thousands", 223.23),
        ("1.5M", "thousands", 1500.0),
        ("33.41", "millions", 33.41),
        ("1,234.56", "plain", 1234.56),
        ("$5.39", "usd", 5.39),
        ("0", "plain", 0.0),
        ("0.00%", "percent", 0.0),
        ("-0.5", "plain", -0.5),
        ("nan", "plain", None),
        ("inf", "plain", None),
        ("abc", "plain", None),
        ("12.4.5", "plain", None),
    ],
)
def test_cell_parsing(raw: str | None, unit: str, stored: float | None) -> None:
    assert parse_number(raw, unit) == stored


def test_a_blank_is_never_zero() -> None:
    assert parse_number("", "percent") is None
    assert parse_number("-", "millions") is None
    assert parse_number("0", "plain") == 0.0


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("8/13/2026 8:30:00 AM", date(2026, 8, 13)),
        ("09/02/2026", date(2026, 9, 2)),
        ("10/5/2026", date(2026, 10, 5)),
        ("Oct 28 AMC", None),
        ("", None),
        (None, None),
        ("13/45/2026", None),
    ],
)
def test_earnings_date_is_parsed_only_in_the_known_format(
    text: str | None, parsed: date | None
) -> None:
    assert parse_earnings_date(text) == parsed


# ---------------------------------------------------------------------- the column map


def test_the_map_has_50_columns_and_unique_fields() -> None:
    assert len(COLUMNS) == EXPECTED_COLUMN_COUNT == 50
    assert len(set(FIELDS)) == 50
    assert len(NUMERIC_FIELDS) == 46 - 1  # 46 data columns, one of them (the date text) is text


def test_every_alias_belongs_to_one_column() -> None:
    lookup = header_lookup()
    assert len(lookup) >= len(COLUMNS)
    assert lookup[normalise_header("  52-WEEK   high ")].field == "w52h"


def test_every_snapshot_field_in_the_map_exists_on_the_model() -> None:
    from apps.marketdata.models import FinvizSnapshot

    model_fields = {f.name for f in FinvizSnapshot._meta.fields}
    assert set(FIELDS) - {"ticker"} <= model_fields


# ------------------------------------------------------------------- T10.2 a full parse


def test_a_full_export_parses_every_column_for_every_row() -> None:
    parsed = parse_export(export_csv(universe(12)))
    assert parsed.row_count == 12
    assert len(parsed.headers) == len(parsed.mapped) == 50
    assert parsed.missing_fields == []
    assert parsed.unmapped_headers == []
    for row in parsed.rows:
        assert set(row.values) >= set(FIELDS) - {"ticker"}


def test_a_dash_is_null_in_the_parsed_row() -> None:
    row = parse_export(export_csv([company_row("abc", 4)])).rows[0]
    assert (row.values["pe"], row.values["gross_margin"]) == (None, None)


def test_values_land_in_the_right_fields_with_units_applied() -> None:
    parsed = parse_export(export_csv([company_row("abc", 3)]))
    row = parsed.rows[0]
    assert row.ticker == "ABC"
    v = row.values
    assert v["company_name_raw"] == "abc Therapeutics"
    assert v["industry_raw"] == "Biotechnology"
    assert v["price"] == 5.3
    assert v["mcap_musd"] == 103.5
    assert v["shr_out_m"] == 33.0
    assert v["avg_vol_k"] == 203.5
    assert v["perf_w"] == 3.1
    assert v["perf_ytd"] == -12.5
    assert v["w52h"] == -29.61
    assert v["pe"] == 21.4
    assert v["gross_margin"] == 64.0
    assert v["eps_this"] == 13.95
    assert v["earnings_date_text"] == "8/13/2026 8:30:00 AM"
    assert v["earnings_date"] == date(2026, 8, 13)


def test_headers_match_ignoring_case_spaces_and_aliases() -> None:
    headers = [h.upper() if i % 2 else f"  {h}  " for i, h in enumerate(HEADERS)]
    headers[HEADERS.index("Cash/sh")] = "Cash per Share"
    headers[HEADERS.index("Average Volume")] = "avg   volume"
    parsed = parse_export(export_csv([company_row("AAA")], headers=headers))
    assert parsed.missing_fields == []
    assert parsed.rows[0].values["cash_sh"] == 2.57
    assert parsed.rows[0].values["avg_vol_k"] == 200.5


def test_a_byte_order_mark_and_quoted_cells_are_handled() -> None:
    raw = export_csv([company_row("AAA", company_name_raw='Acme, "Bio" Inc')])
    parsed = parse_export(b"\xef\xbb\xbf" + raw)
    assert parsed.rows[0].values["company_name_raw"] == 'Acme, "Bio" Inc'


def test_tickers_are_trimmed_and_upper_cased_and_blank_rows_skipped() -> None:
    rows = [company_row(" abc "), company_row(""), company_row("DEF")]
    parsed = parse_export(export_csv(rows))
    assert [r.ticker for r in parsed.rows] == ["ABC", "DEF"]
    assert parsed.skipped_rows == 1


def test_fully_empty_lines_are_ignored() -> None:
    text = export_csv(universe(2)).decode() + "\n\n,,,\n"
    assert parse_export(text).row_count == 2


def test_duplicate_tickers_keep_the_first_row() -> None:
    rows = [
        company_row("AAA", 1, price="5.00"),
        company_row("BBB"),
        company_row("AAA", 2, price="9.00"),
    ]
    parsed = parse_export(export_csv(rows))
    assert [r.ticker for r in parsed.rows] == ["AAA", "BBB"]
    assert parsed.rows[0].values["price"] == 5.0
    assert parsed.duplicate_tickers == ["AAA"]


# ----------------------------------------------------------- unmapped and missing columns


def test_unmapped_columns_are_kept_as_extras_by_position() -> None:
    parsed = parse_export(export_csv([company_row("AAA")], extra={"Mystery": "42", "No.": "1"}))
    assert parsed.unmapped_headers == ["Mystery", "No."]
    assert parsed.rows[0].extras == {"Mystery#50": "42", "No.#51": "1"}


def test_a_missing_optional_column_is_reported_and_left_out() -> None:
    parsed = parse_export(export_csv([company_row("AAA")], drop=("eps_this", "cash_sh")))
    assert parsed.missing_fields == ["cash_sh", "eps_this"] or set(parsed.missing_fields) == {
        "cash_sh",
        "eps_this",
    }
    assert "eps_this" not in parsed.rows[0].values


@pytest.mark.parametrize(
    "field", ["ticker", "company_name_raw", "industry_raw", "price", "mcap_musd"]
)
def test_a_missing_required_column_is_an_error_naming_it(field: str) -> None:
    with pytest.raises(ExportFormatError, match=field) as raised:
        parse_export(export_csv([company_row("AAA")], drop=(field,)))
    assert field in raised.value.missing


@pytest.mark.parametrize(
    "content",
    [b"", b"   \n", b"<html><body>Please log in</body></html>", "﻿"],
)
def test_unusable_files_are_rejected(content: bytes | str) -> None:
    with pytest.raises(ExportFormatError):
        parse_export(content)


def test_a_header_only_file_parses_to_no_rows() -> None:
    assert parse_export(export_csv([])).row_count == 0


def test_unparseable_cells_become_null_and_are_counted() -> None:
    parsed = parse_export(
        export_csv([company_row("AAA", price="oops"), company_row("BBB", rsi="??")])
    )
    assert parsed.rows[0].values["price"] is None
    assert parsed.bad_cells["price"] == 1
    assert parsed.bad_cells["rsi"] == 1
    assert parsed.bad_cells["rel_vol"] == 0


# ----------------------------------------------------------- duplicate headers by position


def two_52_week_pairs(percent_first: bool) -> bytes:
    """An export whose 52-week high and low headers each appear twice (percent and price)."""
    rows = universe(3)
    base = export_csv(rows).decode()
    table = list(csv.reader(io.StringIO(base)))
    header = table[0]
    hi, lo = header.index("52-Week High"), header.index("52-Week Low")
    prices = {"hi": ["7.65", "8.10", "6.20"], "lo": ["3.10", "3.50", "2.90"]}
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([*header, "52-Week High", "52-Week Low"])
    for n, line in enumerate(table[1:]):
        extra = [prices["hi"][n], prices["lo"][n]]
        if not percent_first:
            line = list(line)
            line[hi], line[lo], extra = extra[0], extra[1], [line[hi], line[lo]]
        writer.writerow([*line, *extra])
    return out.getvalue().encode()


@pytest.mark.parametrize("percent_first", [True, False])
def test_repeated_52_week_headers_are_both_read_and_the_percent_pair_is_used(
    percent_first: bool,
) -> None:
    parsed = parse_export(two_52_week_pairs(percent_first))
    assert len(parsed.headers) == 52
    assert set(parsed.duplicate_headers) == {"52-Week High", "52-Week Low"}
    assert parsed.rows[0].values["w52h"] == -29.61  # the percent distance, not a price
    assert parsed.rows[0].values["w52l"] == 41.2
    if percent_first:
        assert parsed.rows[0].extras == {"52-Week High#50": "7.65", "52-Week Low#51": "3.10"}
    else:
        assert parsed.mapped["w52h"] == 50 and parsed.mapped["w52l"] == 51
        assert set(parsed.rows[0].extras.values()) == {"7.65", "3.10"}
    assert set(parsed.duplicate_choice) == {"w52h", "w52l"}


def test_without_percent_signs_the_first_repeated_column_is_used() -> None:
    rows = [company_row("AAA", w52h="-29.6", w52l="41.2")]
    text = export_csv(rows).decode().splitlines()
    text[0] += ",52-Week High,52-Week Low"
    text[1] += ",7.65,3.10"
    parsed = parse_export("\n".join(text))
    assert parsed.mapped["w52h"] == HEADERS.index("52-Week High")
    assert parsed.rows[0].values["w52h"] == -29.6
