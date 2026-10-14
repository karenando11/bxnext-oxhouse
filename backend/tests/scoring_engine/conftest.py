"""Shared fixtures for the scoring engine tests.

The golden export contains licensed Finviz-derived data, so it is never committed. Put
``golden_export_2026-10-12.csv`` in ``tests/fixtures/private/`` (git-ignored) or point the
``OXHOUSE_GOLDEN_EXPORT`` environment variable at it. Tests that need it are marked ``golden``
and are skipped, with a clear reason, when the file is absent.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest

GOLDEN_ENV_VAR = "OXHOUSE_GOLDEN_EXPORT"
DEFAULT_GOLDEN_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "private" / "golden_export_2026-10-12.csv"
)


@pytest.fixture(scope="session")
def golden_rows() -> list[dict[str, str]]:
    path = Path(os.environ.get(GOLDEN_ENV_VAR, DEFAULT_GOLDEN_PATH))
    if not path.exists():
        pytest.skip(
            f"Golden export not found at {path}. Place it in tests/fixtures/private/ "
            f"or set {GOLDEN_ENV_VAR}."
        )
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))
