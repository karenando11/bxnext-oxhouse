"""T11.1, T11.2, T11.9 (unit): the SEC client, limiter and retries with a fake clock."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

from apps.sec.client import (
    BACKOFF_SECONDS,
    MIN_INTERVAL_SECONDS,
    USER_AGENT_ENV_VAR,
    SecClient,
    SecFetchError,
    SecLimiter,
    SecUserAgentMissing,
    read_user_agent,
    shared_limiter,
)

UA = "Example Research research@example.com"
URLS = {
    "facts": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
    "submissions": "https://data.sec.gov/submissions/CIK0000000001.json",
    "form4": "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/form4.xml",
}


class Clock:
    """A fake clock: sleeping moves time forward and nothing really waits."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class Recorder:
    """A transport that notes the clock at each request and plays back outcomes."""

    def __init__(self, clock: Clock, *outcomes: Any) -> None:
        self.clock = clock
        self.outcomes = list(outcomes)
        self.times: list[float] = []
        self.headers: list[dict[str, str]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.times.append(self.clock.now())
        self.headers.append(headers)
        outcome = self.outcomes.pop(0) if self.outcomes else (200, b"{}")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make(clock: Clock, *outcomes: Any, limiter: SecLimiter | None = None) -> tuple[SecClient, Any]:
    transport = Recorder(clock, *outcomes)
    client = SecClient(
        user_agent=UA,
        limiter=limiter or SecLimiter(clock=clock.now, sleep=clock.sleep),
        transport=transport,
        sleep=clock.sleep,
    )
    return client, transport


def gaps(times: list[float]) -> list[float]:
    return [round(b - a, 6) for a, b in zip(times, times[1:], strict=False)]


# --------------------------------------------------------------------- T11.1 throttle


def test_limiter_keeps_requests_at_least_110_ms_apart() -> None:
    clock = Clock()
    client, transport = make(clock)
    for _ in range(6):
        client.get(URLS["facts"])
    assert len(transport.times) == 6
    assert all(gap >= MIN_INTERVAL_SECONDS for gap in gaps(transport.times))
    assert MIN_INTERVAL_SECONDS == 0.110


def test_a_slow_caller_is_not_delayed() -> None:
    clock = Clock()
    limiter = SecLimiter(clock=clock.now, sleep=clock.sleep)
    limiter.wait()
    clock.t += 1.0
    limiter.wait()
    assert clock.sleeps == []


def test_every_request_declares_the_user_agent() -> None:
    clock = Clock()
    client, transport = make(clock)
    client.get(URLS["facts"])
    client.get(URLS["submissions"])
    assert [h["User-Agent"] for h in transport.headers] == [UA, UA]


def test_user_agent_is_read_from_the_environment() -> None:
    assert read_user_agent({USER_AGENT_ENV_VAR: f"  {UA} "}) == UA
    with pytest.raises(SecUserAgentMissing):
        read_user_agent({})
    with pytest.raises(SecUserAgentMissing):
        read_user_agent({USER_AGENT_ENV_VAR: "   "})


def test_the_default_client_uses_the_shared_limiter() -> None:
    assert SecClient(user_agent=UA).limiter is shared_limiter()


# ---------------------------------------------------------------- T11.9 one shared limiter


def test_facts_submissions_and_form4_requests_share_one_limiter() -> None:
    clock = Clock()
    limiter = SecLimiter(clock=clock.now, sleep=clock.sleep)
    # Separate clients, as three collectors would have, but one limiter.
    facts, transport_a = make(clock, limiter=limiter)
    subs, transport_b = make(clock, limiter=limiter)
    form4, transport_c = make(clock, limiter=limiter)
    order = [facts, subs, form4, form4, facts, subs, subs, form4, facts]
    urls = [
        URLS["facts"],
        URLS["submissions"],
        URLS["form4"],
        URLS["form4"],
        URLS["facts"],
        URLS["submissions"],
        URLS["submissions"],
        URLS["form4"],
        URLS["facts"],
    ]
    for client, url in zip(order, urls, strict=True):
        client.get(url)
    times = sorted(transport_a.times + transport_b.times + transport_c.times)
    assert len(times) == 9
    assert all(gap >= MIN_INTERVAL_SECONDS for gap in gaps(times))


def test_only_the_client_module_makes_a_limiter() -> None:
    """No collector has a limiter of its own."""
    root = Path(__file__).resolve().parents[1] / "apps" / "sec"
    offenders = [
        path.name
        for path in root.rglob("*.py")
        if path.name != "client.py" and "SecLimiter(" in path.read_text()
    ]
    assert offenders == []


# --------------------------------------------------------------------- T11.2 retries


def test_a_429_then_a_503_then_success_is_retried_with_backoff() -> None:
    clock = Clock()
    client, transport = make(clock, (429, b""), (503, b""), (200, b'{"ok": 1}'))
    response = client.get(URLS["facts"])
    assert (response.http_status, response.attempts, response.content) == (200, 3, b'{"ok": 1}')
    assert len(transport.times) == 3
    backoffs = [s for s in clock.sleeps if s >= BACKOFF_SECONDS[0]]
    assert backoffs[:2] == [BACKOFF_SECONDS[0], BACKOFF_SECONDS[1]]
    assert all(gap >= MIN_INTERVAL_SECONDS for gap in gaps(transport.times))


@pytest.mark.parametrize(
    ("status", "reason"), [(429, "rate_limited"), (503, "http_5xx"), (500, "http_5xx")]
)
def test_three_attempts_then_the_failure_carries_a_reason(status: int, reason: str) -> None:
    clock = Clock()
    client, transport = make(clock, (status, b""), (status, b""), (status, b""), (200, b"x"))
    with pytest.raises(SecFetchError) as caught:
        client.get(URLS["facts"])
    assert (caught.value.reason_code, caught.value.attempts, caught.value.http_status) == (
        reason,
        3,
        status,
    )
    assert len(transport.times) == 3


def test_a_timeout_and_a_connection_error_are_retried() -> None:
    clock = Clock()
    client, _ = make(clock, TimeoutError("slow"), socket.gaierror("no dns"), (200, b"{}"))
    assert client.get(URLS["facts"]).attempts == 3
    client, _ = make(clock, TimeoutError(), TimeoutError(), TimeoutError())
    with pytest.raises(SecFetchError) as caught:
        client.get(URLS["facts"])
    assert caught.value.reason_code == "timeout"


def test_a_404_fails_at_once() -> None:
    clock = Clock()
    client, transport = make(clock, (404, b""), (200, b"{}"))
    with pytest.raises(SecFetchError) as caught:
        client.get(URLS["facts"])
    assert (caught.value.reason_code, caught.value.attempts, caught.value.http_status) == (
        "http_4xx",
        1,
        404,
    )
    assert len(transport.times) == 1
