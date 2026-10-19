from __future__ import annotations

import json
import time
from http.client import RemoteDisconnected
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class HttpClient:
    def __init__(self, timeout: int = 20, *, max_attempts: int = 3, retry_delay_seconds: float = 0.5) -> None:
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)

    def json(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        raw = self.bytes(url, method=method, headers=headers, body=body)
        return json.loads(raw.decode("utf-8"))

    def bytes(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> bytes:
        request_headers = {"Accept": "application/json", "User-Agent": "morning-theme-alert/0.2"}
        request_headers.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = Request(url, data=data, headers=request_headers, method=method)
        for attempt in range(1, self.max_attempts + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except HTTPError as exc:
                detail = exc.read(500).decode("utf-8", errors="replace")
                retryable = exc.code == 429 or exc.code >= 500
                if not retryable or attempt == self.max_attempts:
                    raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
            except (URLError, RemoteDisconnected, ConnectionResetError, TimeoutError) as exc:
                if attempt == self.max_attempts:
                    reason = exc.reason if isinstance(exc, URLError) else exc
                    raise RuntimeError(f"request failed for {url}: {reason}") from exc
            if self.retry_delay_seconds:
                time.sleep(self.retry_delay_seconds * attempt)
        raise AssertionError("unreachable")
