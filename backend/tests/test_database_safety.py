"""Guards that keep tests and fixture loads away from a shared database."""

from __future__ import annotations

from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.platform.models import PipelineRun
from apps.platform.safety import RemoteDatabaseRefused, ensure_local_database


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_local_hosts_are_allowed(host: str) -> None:
    ensure_local_database(host, "do something")


def test_a_remote_host_is_refused_with_its_name() -> None:
    with pytest.raises(RemoteDatabaseRefused, match="ep-x.neon.tech"):
        ensure_local_database("ep-x.neon.tech", "load fixture data")


def test_a_compose_service_name_can_be_declared_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OXHOUSE_LOCAL_DB_HOSTS", raising=False)
    with pytest.raises(RemoteDatabaseRefused):
        ensure_local_database("db", "run the tests")
    monkeypatch.setenv("OXHOUSE_LOCAL_DB_HOSTS", "db, other")
    ensure_local_database("db", "run the tests")
    ensure_local_database("other", "run the tests")
    with pytest.raises(RemoteDatabaseRefused):
        ensure_local_database("ep-x.neon.tech", "run the tests")


def test_a_remote_host_can_be_allowed_explicitly() -> None:
    ensure_local_database("ep-x.neon.tech", "load fixture data", allowed=True)


@pytest.mark.django_db
def test_load_golden_refuses_a_remote_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from django.db import connection

    monkeypatch.setitem(connection.settings_dict, "HOST", "ep-x.neon.tech")
    export = tmp_path / "golden.csv"
    export.write_text("ticker\n")
    with pytest.raises(CommandError, match="--allow-remote"):
        call_command("load_golden", str(export))
    assert PipelineRun.objects.count() == 0
