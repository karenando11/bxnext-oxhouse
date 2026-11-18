"""T10.6, T10.7 (unit): the download client with a fake transport and a fake clock."""

from __future__ import annotations

import logging
import socket
from typing import Any

import pytest

from apps.marketdata.finviz.client import (
    BACKOFF_SECONDS,
    TOKEN_ENV_VAR,
    FinvizDownloadError,
    FinvizRequest,
    FinvizTokenMissing,
    download,
    read_token,
    urllib_transport,
)

TOKEN = "s3cr3t-token-value-123"
CSV = b"Ticker,Company\nAAA,Acme\n"


class FakeTransport:
    """Plays back a list of outcomes: a (status, body) pair or an exception to raise."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.urls: list[str] = []
        self.timeouts: list[float] = []

    def __call__(self, url: str, timeout: float) -> tuple[int, bytes]:
        self.urls.append(url)
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def run(transport: FakeTransport, request: FinvizRequest | None = None) -> tuple[Any, list[float]]:
    sleeps: list[float] = []
    got = download(
        request or FinvizRequest(), token=TOKEN, transport=transport, sleep=sleeps.append
    )
    return got, sleeps


def test_a_good_response_is_returned_after_one_attempt() -> None:
    transport = FakeTransport((200, CSV))
    got, sleeps = run(transport)
    assert (got.content, got.http_status, got.attempts) == (CSV, 200, 1)
    assert sleeps == []


def test_the_url_uses_the_view_filter_and_columns_from_the_request() -> None:
    transport = FakeTransport((200, CSV))
    request = FinvizRequest(view=151, sector_filter="sec_healthcare", columns="1,2,6")
    run(transport, request)
    url = transport.urls[0]
    assert url.startswith("https://elite.finviz.com/export.ashx?")
    assert "v=151" in url and "f=sec_healthcare" in url and "c=1,2,6" in url
    assert f"auth={TOKEN}" in url


def test_columns_are_left_out_when_not_set() -> None:
    transport = FakeTransport((200, CSV))
    run(transport)
    assert "c=" not in transport.urls[0]


def test_the_public_url_and_scope_never_contain_the_token() -> None:
    request = FinvizRequest(columns="1,2,6")
    assert TOKEN not in request.public_url()
    assert "auth" not in request.public_url()
    assert request.scope() == "view=151;f=sec_healthcare;c=1,2,6"


@pytest.mark.parametrize(
    ("status", "reason"), [(429, "rate_limited"), (500, "http_5xx"), (503, "http_5xx")]
)
def test_transient_statuses_are_retried_up_to_three_times_with_backoff(
    status: int, reason: str
) -> None:
    transport = FakeTransport((status, b""), (status, b""), (status, b""))
    sleeps: list[float] = []
    with pytest.raises(FinvizDownloadError) as raised:
        download(FinvizRequest(), token=TOKEN, transport=transport, sleep=sleeps.append)
    assert len(transport.urls) == 3
    assert sleeps == [BACKOFF_SECONDS[0], BACKOFF_SECONDS[1]]
    assert (raised.value.reason_code, raised.value.attempts, raised.value.http_status) == (
        reason,
        3,
        status,
    )


def test_a_retry_that_succeeds_returns_the_file_and_the_attempt_count() -> None:
    transport = FakeTransport((503, b""), (429, b""), (200, CSV))
    got, sleeps = run(transport)
    assert got.attempts == 3
    assert got.content == CSV
    assert sleeps == [1.0, 2.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_other_client_errors_fail_at_once_without_retrying(status: int) -> None:
    transport = FakeTransport((status, b""))
    with pytest.raises(FinvizDownloadError) as raised:
        run(transport)
    assert len(transport.urls) == 1
    assert (raised.value.reason_code, raised.value.attempts) == ("http_4xx", 1)


def test_timeouts_and_connection_errors_are_retried_with_their_own_reasons() -> None:
    transport = FakeTransport(TimeoutError("slow"), TimeoutError("slow"), TimeoutError("slow"))
    with pytest.raises(FinvizDownloadError) as raised:
        run(transport)
    assert raised.value.reason_code == "timeout"
    assert len(transport.urls) == 3
    transport = FakeTransport(ConnectionResetError(), OSError("x"), socket.gaierror("dns"))
    with pytest.raises(FinvizDownloadError) as raised:
        run(transport)
    assert raised.value.reason_code == "fetch_error"


def test_the_attempt_count_and_timeout_come_from_the_request() -> None:
    transport = FakeTransport((500, b""), (500, b""))
    with pytest.raises(FinvizDownloadError):
        run(transport, FinvizRequest(attempts=2, timeout=7.5))
    assert len(transport.urls) == 2
    assert transport.timeouts == [7.5, 7.5]


# ------------------------------------------------------------------ T10.7 token never leaks


def test_the_token_is_not_in_errors_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    transport = FakeTransport(
        OSError(f"cannot reach https://x/?auth={TOKEN}"), (503, b""), (401, b"")
    )
    with pytest.raises(FinvizDownloadError) as raised:
        run(transport)
    assert TOKEN not in str(raised.value)
    assert TOKEN not in repr(raised.value.__dict__)
    assert TOKEN not in caplog.text
    assert "Finviz download attempt" in caplog.text


def test_a_transport_error_message_with_the_token_is_scrubbed() -> None:
    transport = FakeTransport(*([TimeoutError(f"timed out for auth={TOKEN}")] * 3))
    with pytest.raises(FinvizDownloadError) as raised:
        run(transport)
    assert TOKEN not in str(raised.value)
    assert "***" in str(raised.value)


# ----------------------------------------------------------------------------- the token


def test_the_token_is_read_from_the_environment() -> None:
    assert read_token({TOKEN_ENV_VAR: f"  {TOKEN}  "}) == TOKEN


@pytest.mark.parametrize("env", [{}, {TOKEN_ENV_VAR: ""}, {TOKEN_ENV_VAR: "   "}])
def test_a_missing_token_names_the_variable_not_a_value(env: dict[str, str]) -> None:
    with pytest.raises(FinvizTokenMissing, match=TOKEN_ENV_VAR):
        read_token(env)


def test_download_reads_the_token_from_the_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, TOKEN)
    transport = FakeTransport((200, CSV))
    download(FinvizRequest(), transport=transport, sleep=lambda _: None)
    assert f"auth={TOKEN}" in transport.urls[0]
    monkeypatch.delenv(TOKEN_ENV_VAR)
    with pytest.raises(FinvizTokenMissing):
        download(FinvizRequest(), transport=transport)


# ------------------------------------------------------------------- the network guard


def test_the_real_transport_cannot_reach_the_network_in_tests() -> None:
    with pytest.raises(AssertionError, match="network connection"):
        urllib_transport("https://example.com/", 1.0)
