"""A small HTTP client with retries, backoff and logging shared by all sources."""

from __future__ import annotations

import threading
from typing import Any

import requests
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from elecprice import __version__
from elecprice.config import get_settings
from elecprice.logging_utils import get_logger

log = get_logger(__name__)

USER_AGENT = f"elecprice/{__version__} (portfolio project; github.com/ssabeeth/electricity)"
RETRY_STATUS = {429, 500, 502, 503, 504}

_local = threading.local()


class HTTPStatusError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status} for {url}: {body[:300]}")
        self.status = status


def _session() -> requests.Session:
    # requests.Session is not thread-safe; keep one per thread.
    sess = getattr(_local, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers["User-Agent"] = USER_AGENT
        _local.session = sess
    return sess


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, HTTPStatusError):
        return exc.status in RETRY_STATUS
    return isinstance(exc, requests.ConnectionError | requests.Timeout)


@retry(
    retry=retry_if_exception(_retryable),
    stop=stop_after_attempt(6),
    wait=wait_exponential_jitter(initial=2, max=60),
    before_sleep=before_sleep_log(log, 30),  # WARNING
    reraise=True,
)
def get(url: str, params: dict[str, Any] | None = None) -> requests.Response:
    timeout = get_settings().http_timeout_s
    resp = _session().get(url, params=params, timeout=timeout)
    if resp.status_code >= 400:
        raise HTTPStatusError(resp.status_code, resp.url, resp.text)
    log.debug("GET %s -> %s (%d bytes)", resp.url, resp.status_code, len(resp.content))
    return resp


def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    return get(url, params).json()
