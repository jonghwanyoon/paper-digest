"""urllib 기반 HTTP GET. 호스트별 최소 간격과 429/5xx 재시도만 한다."""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# NCBI 무인증 3 req/s, 나머지는 예의상 1 req/s 이하로 유지
_MIN_INTERVAL = {"eutils.ncbi.nlm.nih.gov": 0.4}
_DEFAULT_INTERVAL = 1.0
_last_call: dict[str, float] = {}


class HttpError(Exception):
    def __init__(self, url: str, status: int | None, reason: str):
        super().__init__(f"{status or 'ERR'} {reason} <{url}>")
        self.status = status


class Client:
    def __init__(self, email: str, timeout: float = 30.0, retries: int = 2):
        self.user_agent = f"paper-digest/0.1 (mailto:{email})"
        self.timeout = timeout
        self.retries = retries

    def _throttle(self, host: str) -> None:
        wait = _MIN_INTERVAL.get(host, _DEFAULT_INTERVAL) - (time.monotonic() - _last_call.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.monotonic()

    def get(self, url: str, accept: str | None = None) -> bytes:
        host = urlsplit(url).hostname or ""
        headers = {"User-Agent": self.user_agent}
        if accept:
            headers["Accept"] = accept
        for attempt in range(self.retries + 1):
            self._throttle(host)
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                            timeout=self.timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    delay = 5 * (attempt + 1)
                    log.info("HTTP %s, %ss 후 재시도: %s", e.code, delay, url)
                    time.sleep(delay)
                    continue
                raise HttpError(url, e.code, e.reason) from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < self.retries:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise HttpError(url, None, str(e)) from e
        raise AssertionError("unreachable")
