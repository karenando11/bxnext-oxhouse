"""Fixtures for the private (licensed) export files. They are never committed."""

from __future__ import annotations

import os
from pathlib import Path

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
