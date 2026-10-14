"""T0.3 (integration): /healthz reflects database connectivity."""

from __future__ import annotations

from typing import Any

import pytest
from django.db import OperationalError
from django.test import Client


@pytest.mark.django_db
def test_healthz_reports_ok_when_the_database_is_up(client: Client) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["schema_version"]
    assert body["migrations_applied"] > 0


@pytest.mark.django_db
def test_healthz_needs_no_authentication(client: Client) -> None:
    assert client.get("/healthz").status_code == 200


def test_healthz_reports_503_when_the_database_is_down(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise OperationalError("could not connect to host=db user=oxhouse password=hunter2")

    monkeypatch.setattr("apps.platform.views.connection.cursor", fail)

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unavailable"}
    assert "hunter2" not in response.content.decode()


@pytest.mark.django_db
def test_healthz_is_read_only(client: Client) -> None:
    assert client.post("/healthz").status_code == 405


@pytest.mark.django_db
def test_healthz_is_not_cached(client: Client) -> None:
    assert "no-cache" in client.get("/healthz")["Cache-Control"]
