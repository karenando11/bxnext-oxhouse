"""Download one FINRA consolidated short-volume file (``CNMSshvolYYYYMMDD.txt``).

A date with no file (a market holiday, or a file not published yet) is not a failure: FINRA's
storage answers 404, or 403 for a key that does not exist, and both are reported as
``FinraNoFile``. 429, 5xx and timeouts are retried with exponential backoff; after the last
attempt ``FinraDownloadError`` carries a reason code from the fixed list.
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)

BASE_URL = "https://cdn.finra.org/equity/regsho/daily"
DEFAULT_ATTEMPTS = 3
DEFAULT_TIMEOUT_SECONDS = 60.0
BACKOFF_SECONDS = (1.0, 2.0, 4.0)
USER_AGENT = "oxhouse-research/0.1"
NO_FILE_STATUSES = (403, 404)

# A transport gets (url, timeout) and returns (status, body). It raises TimeoutError or OSError on
# a connection problem.
Transport = Callable[[str, float], tuple[int, bytes]]


class FinraNoFile(Exception):
    """FINRA has no file for this date."""

    def __init__(self, http_status: int) -> None:
        super().__init__(f"HTTP {http_status}: no file")
        self.http_status = http_status


class FinraDownloadError(RuntimeError):
    def __init__(
        self, reason_code: str, attempts: int, message: str, http_status: int | None = None
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.attempts = attempts
        self.http_status = http_status


@dataclass(frozen=True)
class FinraDownload:
    content: bytes
    http_status: int
    attempts: int
    url: str


def file_url(day: date) -> str:
    return f"{BASE_URL}/CNMSshvol{day:%Y%m%d}.txt"


def urllib_transport(url: str, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def download(
    day: date,
    *,
    transport: Transport = urllib_transport,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = DEFAULT_ATTEMPTS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> FinraDownload:
    """Fetch the file for ``day``. Raises ``FinraNoFile`` or ``FinraDownloadError``."""
    url = file_url(day)
    reason, status, message = "fetch_error", None, "no attempt was made"
    for attempt in range(1, attempts + 1):
        try:
            status, body = transport(url, timeout)
        except TimeoutError as exc:
            reason, status, message = "timeout", None, f"timed out: {exc}"
        except OSError as exc:
            reason, status = "fetch_error", None
            message = f"connection problem: {type(exc).__name__}"
        else:
            if status == 200:
                return FinraDownload(body, status, attempt, url)
            if status in NO_FILE_STATUSES:
                raise FinraNoFile(status)
            message = f"HTTP {status}"
            if status == 429:
                reason = "rate_limited"
            elif 500 <= status < 600:
                reason = "http_5xx"
            else:
                raise FinraDownloadError("http_4xx", attempt, message, status)
        logger.warning("FINRA download attempt %d of %d failed: %s", attempt, attempts, message)
        if attempt < attempts:
            sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
    raise FinraDownloadError(reason, attempts, message, status)
