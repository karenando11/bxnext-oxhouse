"""Where alerts go (Steps 16 and 17).

The batch sends through an ``AlertSink``. ``DedupAlerts`` sits in front of the real sink and
keeps the state of every condition in ``AlertState``: a condition sends once when it is raised,
nothing more while it persists, and sends again only after it has cleared and recurred.

The real sink is ``EmailAlerts`` when ``ALERT_EMAIL_TO`` is set, otherwise ``LogAlerts`` (the
error log). A sink may raise; ``DedupAlerts`` catches it, logs it and leaves the condition unraised
so the next batch tries again. An alert that cannot be delivered never stops the batch.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from django.conf import settings
from django.core.mail import send_mail

from apps.platform.models import AlertState

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


class EmailAlerts:
    """One email per alert to the configured recipients (``ALERT_EMAIL_TO``)."""

    def __init__(self, recipients: list[str], sender: str | None = None) -> None:
        self.recipients = recipients
        self.sender = sender or settings.ALERT_EMAIL_FROM

    def send(self, kind: str, subject: str, body: str) -> None:
        logger.error("ALERT [%s] %s: %s", kind, subject, body)
        send_mail(f"[OxHouse] {subject}", body, self.sender, self.recipients, fail_silently=False)


def default_sink() -> AlertSink:
    recipients = list(settings.ALERT_EMAIL_TO)
    return EmailAlerts(recipients) if recipients else LogAlerts()


class DedupAlerts:
    """Sends a condition once, then stays quiet until it clears and recurs.

    ``send`` is the ``AlertSink`` interface, with the alert's kind as the condition (the batch's
    step alerts). ``raise_condition`` and ``clear`` take an explicit key, for conditions that have
    several instances (one per missing FINRA date, one per blank column).
    """

    def __init__(self, sink: AlertSink) -> None:
        self.sink = sink
        self.raised_this_run: set[str] = set()

    def send(self, kind: str, subject: str, body: str) -> None:
        self.raise_condition(kind, kind, subject, body)

    def raise_condition(self, key: str, kind: str, subject: str, body: str) -> bool:
        """Raise ``key``. Returns True when an alert was sent now."""
        self.raised_this_run.add(key)
        state = AlertState.objects.filter(key=key).first()
        if state is not None and state.active:
            return False
        try:
            self.sink.send(kind, subject, body)
        except Exception:
            logger.exception("Could not deliver the alert %s; it is tried again next time", key)
            return False
        now = datetime.now(UTC)
        if state is None:
            state = AlertState(key=key, raised_at=now)
        state.kind, state.subject, state.body = kind, subject[:200], body
        state.active, state.cleared_at, state.last_sent_at = True, None, now
        if state.pk is not None:
            state.raised_at = now
        state.send_count += 1
        state.save()
        return True

    def raise_silently(self, key: str, kind: str, subject: str, body: str) -> None:
        """Record a condition whose alert was already sent elsewhere (the overrun thread)."""
        self.raised_this_run.add(key)
        now = datetime.now(UTC)
        AlertState.objects.update_or_create(
            key=key,
            defaults={
                "kind": kind,
                "subject": subject[:200],
                "body": body,
                "active": True,
                "raised_at": now,
                "cleared_at": None,
                "last_sent_at": now,
            },
        )

    def clear(self, key: str) -> None:
        AlertState.objects.filter(key=key, active=True).update(
            active=False, cleared_at=datetime.now(UTC)
        )

    def clear_except(self, prefix: str, keep: set[str]) -> None:
        """Clear every active condition whose key starts with ``prefix`` and is not in ``keep``."""
        stale = AlertState.objects.filter(key__startswith=prefix, active=True).exclude(key__in=keep)
        stale.update(active=False, cleared_at=datetime.now(UTC))
