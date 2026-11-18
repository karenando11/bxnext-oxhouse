"""Read editable settings (the ``Setting`` table) with a default for a missing key."""

from __future__ import annotations

from typing import Any

from apps.platform.models import Setting


def get_setting(key: str, default: Any = None) -> Any:
    row = Setting.objects.filter(key=key).values_list("value", flat=True).first()
    return default if row is None else row
