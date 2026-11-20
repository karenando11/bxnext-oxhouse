"""The Finviz column map: which export header feeds which ``FinvizSnapshot`` field.

The export is requested with 50 columns: four identity columns and 46 data columns. Each entry
lists the header texts Finviz may use for it (matched ignoring case and repeated spaces), so a
cosmetic rename on their side does not silently drop a column.

Verify against the reference workbook's "Finviz Column Map" sheet (fixture F10) and against the
first real download: ``manage.py finviz_contract_check`` prints every header that matched, every
header that did not, and sample values.

Units are the units the database stores. ``unit`` says how a value with a K, M, B or T suffix is
scaled into the stored unit; a value without a suffix is taken as already in the stored unit.
"""

from __future__ import annotations

from dataclasses import dataclass

TEXT = "text"
NUMBER = "number"

PLAIN = "plain"  # a number or ratio, no scaling
PERCENT = "percent"  # 12.4% is stored as 12.4
USD = "usd"
MILLIONS = "millions"  # market cap in USD millions, shares in millions
THOUSANDS = "thousands"  # average volume in thousands of shares


@dataclass(frozen=True)
class Column:
    field: str  # FinvizSnapshot field (or "ticker")
    headers: tuple[str, ...]  # accepted header texts, most likely first
    kind: str = NUMBER
    unit: str = PLAIN
    required: bool = False  # a missing header stops the load


def _pct(field: str, *headers: str) -> Column:
    return Column(field, headers, unit=PERCENT)


COLUMNS: tuple[Column, ...] = (
    # Identity (4)
    Column("ticker", ("Ticker",), TEXT, required=True),
    Column("company_name_raw", ("Company",), TEXT, required=True),
    Column("sector_raw", ("Sector",), TEXT),
    Column("industry_raw", ("Industry",), TEXT, required=True),
    # Price and size (6)
    Column("price", ("Price",), unit=USD, required=True),
    Column("mcap_musd", ("Market Cap",), unit=MILLIONS, required=True),
    Column("shr_out_m", ("Shares Outstanding", "Outstanding"), unit=MILLIONS),
    Column("shr_float_m", ("Shares Float", "Float"), unit=MILLIONS),
    Column("avg_vol_k", ("Average Volume", "Avg Volume"), unit=THOUSANDS),
    Column("rel_vol", ("Relative Volume", "Rel Volume")),
    # Performance (6)
    _pct("perf_w", "Performance (Week)", "Perf Week"),
    _pct("perf_m", "Performance (Month)", "Perf Month"),
    _pct("perf_q", "Performance (Quarter)", "Perf Quarter"),
    _pct("perf_h", "Performance (Half Year)", "Perf Half Y"),
    _pct("perf_ytd", "Performance (YTD)", "Perf YTD"),
    _pct("perf_y", "Performance (Year)", "Perf Year"),
    # Volatility and technicals (10)
    _pct("vol_w", "Volatility (Week)", "Volatility W"),
    _pct("vol_m", "Volatility (Month)", "Volatility M"),
    Column("beta", ("Beta",)),
    Column("atr", ("Average True Range", "ATR (14)", "ATR"), unit=USD),
    _pct("sma20", "20-Day Simple Moving Average", "SMA20"),
    _pct("sma50", "50-Day Simple Moving Average", "SMA50"),
    _pct("sma200", "200-Day Simple Moving Average", "SMA200"),
    _pct("w52h", "52-Week High", "52W High"),
    _pct("w52l", "52-Week Low", "52W Low"),
    Column("rsi", ("Relative Strength Index (14)", "RSI (14)", "RSI")),
    # Valuation (5)
    Column("pe", ("P/E",)),
    Column("fwd_pe", ("Forward P/E",)),
    Column("ps", ("P/S", "Price/Sales")),
    Column("pb", ("P/B", "Price/Book")),
    Column("pcash", ("P/Cash", "P/C", "Price/Cash")),
    # Balance sheet and margins (10)
    Column("quick_ratio", ("Quick Ratio",)),
    Column("cur_ratio", ("Current Ratio",)),
    Column("ltd_eq", ("LT Debt/Equity",)),
    Column("td_eq", ("Total Debt/Equity",)),
    _pct("gross_margin", "Gross Margin"),
    _pct("op_margin", "Operating Margin"),
    _pct("profit_margin", "Profit Margin"),
    _pct("roa", "Return on Assets"),
    _pct("roe", "Return on Equity"),
    Column("cash_sh", ("Cash/sh", "Cash/Share", "Cash per Share"), unit=USD),
    # Ownership and trading (6)
    _pct("insider_own", "Insider Ownership", "Insider Own"),
    _pct("inst_own", "Institutional Ownership", "Inst Own"),
    _pct("inst_trans", "Institutional Transactions", "Inst Trans"),
    _pct("insider_trans", "Insider Transactions", "Insider Trans"),
    _pct("short_float", "Short Float", "Float Short"),
    Column("short_ratio", ("Short Ratio",)),
    # Earnings (3: two growth estimates and the date, kept as delivered)
    _pct("eps_this", "EPS growth this year", "EPS This Y"),
    _pct("eps_next", "EPS growth next year", "EPS Next Y"),
    Column("earnings_date_text", ("Earnings Date", "Earnings"), TEXT),
)

FIELDS: tuple[str, ...] = tuple(c.field for c in COLUMNS)
REQUIRED_FIELDS: tuple[str, ...] = tuple(c.field for c in COLUMNS if c.required)
# Fields stored on the snapshot as numbers (what the blank-column check watches).
NUMERIC_FIELDS: tuple[str, ...] = tuple(c.field for c in COLUMNS if c.kind == NUMBER)
EXPECTED_COLUMN_COUNT = 50


def normalise_header(text: str) -> str:
    """Header text compared ignoring case, surrounding and repeated spaces."""
    return " ".join(text.split()).casefold()


def header_lookup() -> dict[str, Column]:
    """Normalised header text to its column. Each alias belongs to exactly one column."""
    lookup: dict[str, Column] = {}
    for column in COLUMNS:
        for header in column.headers:
            key = normalise_header(header)
            if key in lookup and lookup[key] is not column:
                raise ValueError(f"Header {header!r} is mapped to two columns")
            lookup[key] = column
    return lookup
