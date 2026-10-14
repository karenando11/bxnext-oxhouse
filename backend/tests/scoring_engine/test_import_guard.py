"""T1.8 (unit): the scoring package must stay free of database, network and Django code."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "scoring_engine"

FORBIDDEN_ROOTS = {
    "django",
    "psycopg",
    "psycopg2",
    "sqlalchemy",
    "sqlite3",
    "requests",
    "httpx",
    "urllib3",
    "aiohttp",
    "socket",
    "http",
    "apps",
    "config",
}
FORBIDDEN_MODULES = {"urllib.request", "datetime.now"}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


@pytest.mark.parametrize("path", sorted(PACKAGE_DIR.glob("*.py")), ids=lambda p: p.name)
def test_module_imports_no_infrastructure(path: Path) -> None:
    offending = {
        name
        for name in _imported_modules(path)
        if name.split(".")[0] in FORBIDDEN_ROOTS or name in FORBIDDEN_MODULES
    }
    assert offending == set(), f"{path.name} imports infrastructure code: {sorted(offending)}"


def test_package_has_modules_to_check() -> None:
    assert len(list(PACKAGE_DIR.glob("*.py"))) >= 3
