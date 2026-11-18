"""Synthetic Finviz exports for tests. They are invented, never real Finviz data."""

from __future__ import annotations

import csv
import io
from typing import Any

from apps.marketdata.finviz.columns import COLUMNS

HEADERS = [c.headers[0] for c in COLUMNS]
ALL_FIELDS = [c.field for c in COLUMNS]


def company_row(ticker: str, i: int = 0, **overrides: str) -> dict[str, str]:
    """One company, keyed by snapshot field, with every column filled."""
    row = {
        "ticker": ticker,
        "company_name_raw": f"{ticker} Therapeutics",
        "sector_raw": "Healthcare",
        "industry_raw": "Biotechnology",
        "price": f"{5 + i / 10:.2f}",
        "mcap_musd": f"{100 + i}.5M",
        "shr_out_m": f"{30 + i}.0M",
        "shr_float_m": f"{25 + i}.0M",
        "avg_vol_k": f"{200 + i}.5K",
        "rel_vol": "1.25",
        "perf_w": f"{i}.10%",
        "perf_m": "-3.40%",
        "perf_q": "5.00%",
        "perf_h": "8.00%",
        "perf_ytd": "-12.50%",
        "perf_y": "20.00%",
        "vol_w": "4.20%",
        "vol_m": "5.10%",
        "beta": "0.90",
        "atr": "0.31",
        "sma20": "1.10%",
        "sma50": "-2.20%",
        "sma200": "-8.30%",
        "w52h": "-29.61%",
        "w52l": "41.20%",
        "rsi": "48.30",
        "pe": "-",
        "fwd_pe": "-",
        "ps": "12.40",
        "pb": "3.10",
        "pcash": "2.10",
        "quick_ratio": f"{2 + i / 10:.1f}",
        "cur_ratio": "2.50",
        "ltd_eq": "0.05",
        "td_eq": "0.10",
        "gross_margin": "-",
        "op_margin": "-",
        "profit_margin": "-",
        "roa": "-25.00%",
        "roe": "-30.00%",
        "cash_sh": "2.57",
        "insider_own": "4.10%",
        "inst_own": "69.11%",
        "inst_trans": "1.20%",
        "insider_trans": "0.00%",
        "short_float": "9.92%",
        "short_ratio": "3.40",
        "eps_this": f"{10 + i}.95%",
        "eps_next": "22.65%",
        "earnings_date_text": "8/13/2026 8:30:00 AM",
    }
    if i % 3 == 0:  # profitable companies have these; loss-making biotechs show a dash
        row.update(pe="21.40", fwd_pe="18.20", gross_margin="64.00%", op_margin="12.50%",
                   profit_margin="9.80%")  # fmt: skip
    row.update(overrides)
    return row


def export_csv(
    rows: list[dict[str, str]],
    *,
    headers: list[str] | None = None,
    drop: tuple[str, ...] = (),
    extra: dict[str, str] | None = None,
) -> bytes:
    """A CSV export: one column per mapped field (in map order), in the position the header
    list gives. ``drop`` removes fields' columns; ``extra`` appends columns by header text."""
    fields = [f for f in ALL_FIELDS if f not in drop]
    header_text = headers or [c.headers[0] for c in COLUMNS if c.field not in drop]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([*header_text, *(extra or {})])
    for row in rows:
        writer.writerow([row.get(f, "") for f in fields] + list((extra or {}).values()))
    return out.getvalue().encode()


def universe(count: int = 10, **overrides: Any) -> list[dict[str, str]]:
    return [company_row(f"T{i:03d}", i, **overrides) for i in range(count)]
