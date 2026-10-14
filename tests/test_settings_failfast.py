"""T0.4 (unit): the real settings module refuses to load without its variables."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from config.envconfig import REQUIRED_VARIABLES

REPO_ROOT = Path(__file__).resolve().parent.parent

LOAD_SETTINGS = (
    "from django.conf import settings; settings.SECRET_KEY"  # triggers the lazy settings import
)


@pytest.mark.parametrize("missing", REQUIRED_VARIABLES)
def test_settings_fail_fast_naming_the_missing_variable(missing: str) -> None:
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "DJANGO_SECRET_KEY": "x" * 20,
        "DATABASE_URL": "postgres://u:p@localhost:5432/db",
    }
    env.pop(missing)
    result = subprocess.run(
        [sys.executable, "-c", LOAD_SETTINGS],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert missing in result.stderr
