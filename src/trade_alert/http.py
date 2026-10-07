from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class HttpClient:
    def __init__(self, timeout: int = 20) -> None:
        self.timeout = timeout

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
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except HTTPError as exc:
            detail = exc.read(500).decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"request failed for {url}: {exc.reason}") from exc
