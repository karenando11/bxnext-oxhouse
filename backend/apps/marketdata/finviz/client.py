"""Download the Finviz Elite export.

The token is read from the ``FINVIZ_AUTH_TOKEN`` environment variable at call time. It is part of
the request URL, so it is scrubbed from every error message and log line, and the URL that is
recorded (``public_url``) never contains it.

Failures: 429 and 5xx responses and timeouts are retried with exponential backoff; any other 4xx
fails at once. After the last attempt a ``FinvizDownloadError`` carries a reason code from the
fixed list.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

TOKEN_ENV_VAR = "FINVIZ_AUTH_TOKEN"
DEFAULT_BASE_URL = "https://elite.finviz.com/export.ashx"
DEFAULT_VIEW = 151
DEFAULT_FILTER = "sec_healthcare"
DEFAULT_ATTEMPTS = 3
DEFAULT_TIMEOUT_SECONDS = 60.0
BACKOFF_SECONDS = (1.0, 2.0, 4.0)
USER_AGENT = "oxhouse-research/0.1"

# A transport gets (url, timeout) and returns (status, body). It raises TimeoutError or OSError on
# a connection problem.
Transport = Callable[[str, float], tuple[int, bytes]]


class FinvizTokenMissing(RuntimeError):
    """The token environment variable is not set."""


class FinvizDownloadError(RuntimeError):
    def __init__(
        self, reason_code: str, attempts: int, message: str, http_status: int | None = None
    ):
        super().__init__(message)
        self.reason_code = reason_code
        self.attempts = attempts
        self.http_status = http_status


@dataclass(frozen=True)
class FinvizRequest:
    base_url: str = DEFAULT_BASE_URL
    view: int | str = DEFAULT_VIEW
    sector_filter: str = DEFAULT_FILTER
    columns: str = ""  # the c= parameter; empty uses the account's saved custom view
    attempts: int = DEFAULT_ATTEMPTS
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def query(self) -> list[tuple[str, str]]:
        params = [("v", str(self.view)), ("f", self.sector_filter)]
        if self.columns:
            params.append(("c", self.columns))
        return params

    def public_url(self) -> str:
        """The URL without the token, safe to store and log."""
        return f"{self.base_url}?{urllib.parse.urlencode(self.query(), safe=',_')}"

    def scope(self) -> str:
        """What was asked for, recorded on the source call (without the token)."""
        return f"view={self.view};f={self.sector_filter};c={self.columns}"


@dataclass(frozen=True)
class Download:
    content: bytes
    http_status: int
    attempts: int
    public_url: str


def read_token(environ: dict[str, str] | None = None) -> str:
    token = (environ if environ is not None else os.environ).get(TOKEN_ENV_VAR, "").strip()
    if not token:
        raise FinvizTokenMissing(f"The environment variable {TOKEN_ENV_VAR} is not set.")
    return token


def urllib_transport(url: str, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def _scrub(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def download(
    request: FinvizRequest,
    *,
    token: str | None = None,
    transport: Transport = urllib_transport,
    sleep: Callable[[float], None] = time.sleep,
) -> Download:
    """Fetch the export, retrying transient failures. Never exposes the token."""
    secret = token if token is not None else read_token()
    url = f"{request.base_url}?" + urllib.parse.urlencode(
        [*request.query(), ("auth", secret)], safe=",_"
    )
    last_reason, last_status, last_message = "fetch_error", None, "no attempt was made"
    for attempt in range(1, request.attempts + 1):
        try:
            status, body = transport(url, request.timeout)
        except TimeoutError as exc:
            last_reason, last_status = "timeout", None
            last_message = _scrub(f"timed out: {exc}", secret)
        except OSError as exc:
            last_reason, last_status = "fetch_error", None
            last_message = _scrub(f"connection problem: {type(exc).__name__}", secret)
        else:
            if status == 200:
                return Download(body, status, attempt, request.public_url())
            last_status = status
            last_message = f"HTTP {status}"
            if status == 429:
                last_reason = "rate_limited"
            elif 500 <= status < 600:
                last_reason = "http_5xx"
            else:
                raise FinvizDownloadError("http_4xx", attempt, last_message, status)
        logger.warning(
            "Finviz download attempt %d of %d failed: %s", attempt, request.attempts, last_message
        )
        if attempt < request.attempts:
            sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
    raise FinvizDownloadError(last_reason, request.attempts, last_message, last_status)
