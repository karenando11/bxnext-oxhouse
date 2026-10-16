"""Reason codes: why a value is missing, carried or approximate.

Every null that the pipeline produces stores one of these codes beside it. The list lives in
code, not in a table. Database check constraints are generated from it, so adding a code
needs a migration (``makemigrations`` detects the change).
"""

from __future__ import annotations

from django.db import models

REASON_CODES: tuple[str, ...] = (
    # SEC and runway
    "no_cik",
    "no_companyfacts",
    "ifrs_incomplete",
    "non_usd_only",
    "operating_cash_flow_missing",
    "cash_missing",
    "fallback_rd_expense",
    "phase_burn_only_no_xbrl",
    "company_cash_positive",
    # Fetching and parsing
    "fetch_error",
    "http_4xx",
    "http_5xx",
    "timeout",
    "rate_limited",
    "parse_error",
    # Carry-forward
    "stale_carried",
    "stale_expired",
    # Trials and catalysts
    "sponsor_no_match",
    "sponsor_ambiguous",
    "no_catalyst",
    "no_trials",
    # Short volume
    "insufficient_history",
    "no_file_for_date",
    "not_in_finra_file",
    # Fixtures
    "fixture_not_available",
)


def reason_code_check(field: str, name: str) -> models.CheckConstraint:
    """A single reason code column holds a known code or NULL."""
    return models.CheckConstraint(
        condition=models.Q(**{f"{field}__in": list(REASON_CODES)})
        | models.Q(**{f"{field}__isnull": True}),
        name=name,
    )


def reason_codes_check(field: str, name: str) -> models.CheckConstraint:
    """An array of reason codes holds only known codes."""
    return models.CheckConstraint(
        condition=models.Q(**{f"{field}__contained_by": list(REASON_CODES)}),
        name=name,
    )
