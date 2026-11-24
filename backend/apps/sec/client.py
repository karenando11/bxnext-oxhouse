"""The one HTTP client every SEC collector uses.

Every request carries the declared User-Agent, passes the single shared limiter (consecutive
requests at least 110 ms apart, across company facts, submissions and Form 4 documents) and is
retried on 429, 5xx and timeouts with exponential backoff. There is no other way to reach the SEC
from this code base, so no collector can have a limiter of its own.

The User-Agent comes from the ``SEC_USER_AGENT`` environment variable at call time (the SEC asks
for a name and a contact address).
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

USER_AGENT_ENV_VAR = "SEC_USER_AGENT"
MIN_INTERVAL_SECONDS = 0.110
DEFAULT_ATTEMPTS = 3
DEFAULT_TIMEOUT_SECONDS = 60.0
BACKOFF_SECONDS = (1.0, 2.0, 4.0)

# A transport gets (url, headers, timeout) and returns (status, body). It raises TimeoutError or
# OSError on a connection problem.
Transport = Callable[[str, dict[str, str], float], tuple[int, bytes]]


class SecUserAgentMissing(RuntimeError):
    """The User-Agent environment variable is not set."""


class SecFetchError(RuntimeError):
    def __init__(
        self, reason_code: str, attempts: int, message: str, http_status: int | None = None
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.attempts = attempts
        self.http_status = http_status


@dataclass(frozen=True)
class SecResponse:
    content: bytes
    http_status: int
    attempts: int


class SecLimiter:
    """Keeps consecutive requests at least ``min_interval`` seconds apart, across threads."""

    def __init__(
        self,
        min_interval: float = MIN_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            if self._last is not None:
                remaining = self.min_interval - (self._clock() - self._last)
                if remaining > 0:
                    self._sleep(remaining)
            self._last = self._clock()


_shared_limiter = SecLimiter()


def shared_limiter() -> SecLimiter:
    """The process-wide limiter. Every ``SecClient`` uses it unless a test passes its own."""
    return _shared_limiter


def read_user_agent(environ: dict[str, str] | None = None) -> str:
    value = (environ if environ is not None else os.environ).get(USER_AGENT_ENV_VAR, "").strip()
    if not value:
        raise SecUserAgentMissing(f"The environment variable {USER_AGENT_ENV_VAR} is not set.")
    return value


def urllib_transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


class SecClient:
    def __init__(
        self,
        *,
        user_agent: str | None = None,
        limiter: SecLimiter | None = None,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        attempts: int = DEFAULT_ATTEMPTS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.user_agent = user_agent if user_agent is not None else read_user_agent()
        self.limiter = limiter if limiter is not None else shared_limiter()
        self.transport = transport
        self._sleep = sleep
        self.attempts = attempts
        self.timeout = timeout

    def get(self, url: str) -> SecResponse:
        """Fetch ``url``. Raises ``SecFetchError`` with a reason code after the last attempt."""
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        reason, status, message = "fetch_error", None, "no attempt was made"
        for attempt in range(1, self.attempts + 1):
            self.limiter.wait()
            try:
                status, body = self.transport(url, headers, self.timeout)
            except TimeoutError as exc:
                reason, status, message = "timeout", None, f"timed out: {exc}"
            except OSError as exc:
                reason, status = "fetch_error", None
                message = f"connection problem: {type(exc).__name__}"
            else:
                if status == 200:
                    return SecResponse(body, status, attempt)
                message = f"HTTP {status}"
                if status == 429:
                    reason = "rate_limited"
                elif 500 <= status < 600:
                    reason = "http_5xx"
                else:
                    raise SecFetchError("http_4xx", attempt, message, status)
            logger.warning(
                "SEC request attempt %d of %d failed: %s", attempt, self.attempts, message
            )
            if attempt < self.attempts:
                self._sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
        raise SecFetchError(reason, self.attempts, message, status)
