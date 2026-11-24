"""Raw downloads are kept on local disk in development and never kept in production."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from apps.companies.models import Company
from apps.marketdata.finra.ingest import collect_short_volume
from apps.marketdata.models import FinvizSnapshot, ShortVolumeDaily
from apps.marketdata.storage import (
    LocalRawStorage,
    NullRawStorage,
    get_raw_storage,
    sha256_hex,
)
from apps.platform.models import SourceCall
from apps.sec.client import SecClient, SecLimiter
from apps.sec.collector import FACTS_URL, SUBMISSIONS_URL, TICKERS_URL, collect_sec
from apps.sec.models import SecFact
from tests import factories
from tests.finra_samples import FakeFinra, finra_file
from tests.finviz_samples import export_csv, universe
from tests.sec_samples import US_FACTS, FakeSec, submissions_json, tickers_json
from tests.test_finviz_client import TOKEN, FakeTransport
from tests.test_sec_client import UA, Clock

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DAY = date(2026, 10, 12)


# ------------------------------------------------------------------------- the storage


def test_null_storage_keeps_nothing() -> None:
    storage = NullRawStorage()
    assert storage.put("finra/2026-10-09/abc.txt", b"payload") == ""
    with pytest.raises(FileNotFoundError):
        storage.get("")


def test_the_factory_follows_the_setting(settings: Any, tmp_path: Path) -> None:
    settings.RAW_STORAGE_BACKEND = "none"
    assert isinstance(get_raw_storage(), NullRawStorage)
    settings.RAW_STORAGE_BACKEND = "local"
    settings.RAW_STORAGE_DIR = tmp_path
    storage = get_raw_storage()
    assert isinstance(storage, LocalRawStorage) and storage.root == tmp_path


# ------------------------------------------------------------------------- the settings


def load_settings(**env: str) -> subprocess.CompletedProcess[str]:
    base = {
        **{
            k: v for k, v in os.environ.items() if not k.startswith(("RAW_STORAGE", "DJANGO_DEBUG"))
        },
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "DJANGO_SECRET_KEY": "x" * 20,
        "DATABASE_URL": "postgres://u:p@localhost:5432/db",
    }
    code = "from django.conf import settings; print(settings.RAW_STORAGE_BACKEND)"
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_ROOT,
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_development_defaults_to_local_disk() -> None:
    result = load_settings(DJANGO_DEBUG="true")
    assert (result.returncode, result.stdout.strip()) == (0, "local")


def test_production_defaults_to_keeping_nothing() -> None:
    result = load_settings(DJANGO_DEBUG="false")
    assert (result.returncode, result.stdout.strip()) == (0, "none")
    # Not setting DJANGO_DEBUG at all is production too.
    assert load_settings().stdout.strip() == "none"


def test_production_refuses_local_disk() -> None:
    result = load_settings(DJANGO_DEBUG="false", RAW_STORAGE_BACKEND="local")
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert "must not be kept on local disk in production" in result.stderr


def test_an_unknown_backend_is_refused() -> None:
    result = load_settings(DJANGO_DEBUG="true", RAW_STORAGE_BACKEND="s3")
    assert result.returncode != 0 and "RAW_STORAGE_BACKEND" in result.stderr


def test_production_may_say_none_explicitly() -> None:
    result = load_settings(DJANGO_DEBUG="false", RAW_STORAGE_BACKEND="none")
    assert (result.returncode, result.stdout.strip()) == (0, "none")


# ---------------------------------------------------- the collectors with nothing kept


@pytest.fixture
def no_storage(settings: Any, tmp_path: Path) -> Path:
    """The production setting, with the disk folder pointing somewhere we can check is empty."""
    settings.RAW_STORAGE_BACKEND = "none"
    settings.RAW_STORAGE_DIR = tmp_path / "must-stay-empty"
    return tmp_path / "must-stay-empty"


@pytest.mark.django_db
def test_finviz_loads_and_writes_no_file(no_storage: Path) -> None:
    from apps.marketdata.finviz.ingest import ingest_finviz

    raw = export_csv(universe(5))
    result = ingest_finviz(DAY, token=TOKEN, transport=FakeTransport((200, raw), (200, raw)))
    assert FinvizSnapshot.objects.count() == 5
    call = result.call
    assert (call.status, call.storage_uri, call.content_hash) == ("ok", "", sha256_hex(raw))
    assert call.bytes == len(raw)
    assert not no_storage.exists()
    again = ingest_finviz(DAY, token=TOKEN, transport=FakeTransport((200, raw)))
    assert again.unchanged is True  # the hash is enough to see it is the same file


@pytest.mark.django_db
def test_finra_loads_and_writes_no_file(no_storage: Path) -> None:
    company = factories.company(name="AAA")
    factories.ticker(company, ticker="AAA")
    friday = date(2026, 10, 9)
    content = finra_file(friday, [("AAA", 600, 1000, "B,Q,N")])
    fake = FakeFinra({friday: content})
    result = collect_short_volume(DAY, dates=[friday], transport=fake, sleep=lambda s: None)
    assert result.of("loaded") == [friday]
    assert ShortVolumeDaily.objects.count() == 1
    call = SourceCall.objects.get(feed="finra_regsho", status="ok")
    assert (call.storage_uri, call.content_hash) == ("", sha256_hex(content))
    again = collect_short_volume(DAY, dates=[friday], transport=fake, sleep=lambda s: None)
    assert again.of("unchanged") == [friday]
    assert not no_storage.exists()


@pytest.mark.django_db
def test_sec_loads_and_writes_no_file(no_storage: Path) -> None:
    company = factories.company(name="KTTA", cik=None)
    factories.ticker(company, ticker="KTTA", valid_from=DAY)
    cik = "0001000001"
    routes = {
        TICKERS_URL: tickers_json(),
        SUBMISSIONS_URL.format(cik=cik): submissions_json(
            [("0001000001-26-000001", "10-K", "2026-03-01", "2025-12-31")]
        ),
        FACTS_URL.format(cik=cik): US_FACTS,
    }
    clock = Clock()
    client = SecClient(
        user_agent=UA,
        limiter=SecLimiter(clock=clock.now, sleep=clock.sleep),
        transport=FakeSec(routes),
        sleep=clock.sleep,
    )
    result = collect_sec(DAY, client=client)
    assert result.failures == {} and SecFact.objects.filter(company=company).count() == 2
    calls = SourceCall.objects.filter(feed__startswith="sec_", status="ok")
    assert calls.count() == 3 and set(calls.values_list("storage_uri", flat=True)) == {""}
    assert all(c.content_hash for c in calls)
    assert not no_storage.exists()
    # Without a stored copy the ticker file is fetched again on a second run, which is harmless.
    again = collect_sec(DAY, client=client)
    assert (again.filings_added, again.facts_added) == (0, 0)
    assert Company.objects.get(pk=company.pk).cik == cik
