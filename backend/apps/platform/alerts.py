"""Where alerts go. The batch sends through an ``AlertSink``; Step 17 plugs email in here.

Until then the default sink writes the alert to the error log, which is where a failed batch is
visible today. A sink must never raise: an alert that cannot be delivered must not stop the batch.
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger("oxhouse.alerts")


class AlertSink(Protocol):
    def send(self, kind: str, subject: str, body: str) -> None: ...


class LogAlerts:
    def send(self, kind: str, subject: str, body: str) -> None:
        logger.error("ALERT [%s] %s: %s", kind, subject, body)


class RecordingAlerts:
    """Keeps every alert in memory (tests, and a batch run that only reports at the end)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, kind: str, subject: str, body: str) -> None:
        self.sent.append((kind, subject, body))

    def kinds(self) -> list[str]:
        return [kind for kind, _, _ in self.sent]
