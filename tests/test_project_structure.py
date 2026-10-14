"""The apps named in the architecture document exist and are installed."""

from __future__ import annotations

import pytest
from django.apps import apps

EXPECTED_APPS = [
    "platform",
    "companies",
    "marketdata",
    "sec",
    "trials",
    "scoring",
    "screener",
    "web",
]


@pytest.mark.parametrize("label", EXPECTED_APPS)
def test_app_is_installed(label: str) -> None:
    assert apps.get_app_config(label).name == f"apps.{label}"


def test_project_apps_do_not_shadow_standard_library_modules() -> None:
    # apps.platform must stay namespaced; importing the stdlib module must still work.
    import platform

    assert hasattr(platform, "system")
