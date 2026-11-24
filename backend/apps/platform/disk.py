"""Disk usage alert (Step 19): usage above the limit on any watched path raises one alert."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from apps.platform.alerts import AlertSink, DedupAlerts, default_sink

DEFAULT_LIMIT_PERCENT = 80.0


@dataclass(frozen=True)
class DiskReading:
    path: str
    percent_used: float


def read_usage(path: str) -> DiskReading:
    usage = shutil.disk_usage(path)
    return DiskReading(path, round(100.0 * usage.used / usage.total, 1))


def check_disk(
    paths: Sequence[str],
    *,
    limit: float = DEFAULT_LIMIT_PERCENT,
    sink: AlertSink | None = None,
    reader: Callable[[str], DiskReading] = read_usage,
) -> list[DiskReading]:
    """Raise ``disk_usage:<path>`` for every path above ``limit`` percent; clear the others.

    Returns the readings. The alert is sent once while the disk stays full and again if it
    fills up again after dropping below the limit.
    """
    alerts = DedupAlerts(sink or default_sink())
    readings = [reader(path) for path in paths]
    for reading in readings:
        key = f"disk_usage:{reading.path}"
        if reading.percent_used > limit:
            alerts.raise_condition(
                key,
                "disk_usage",
                f"Disk {reading.path} is {reading.percent_used:g}% full",
                f"{reading.path} is above the limit of {limit:g}% "
                f"({reading.percent_used:g}% used). Free space or enlarge the volume.",
            )
        else:
            alerts.clear(key)
    return readings
