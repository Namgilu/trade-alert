from __future__ import annotations

import base64
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .http import HttpClient


REPORT_PATH = re.compile(
    r"^reports/(?P<date>\d{4}-\d{2}-\d{2})/(?P<mode>premarket|preopen|confirmation)\.json$"
)
WORKFLOWS = {
    "premarket": "premarket-alert.yml",
    "preopen": "preopen-alert.yml",
    "confirmation": "confirmation-alert.yml",
}


@dataclass(frozen=True)
class StoredReport:
    path: str
    sha: str


class GitHubRepository:
    def __init__(
        self,
        http: HttpClient,
        repository: str,
        token: str = "",
        *,
        data_branch: str = "data",
        ref: str = "main",
        cache_seconds: int = 60,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("GITHUB_REPOSITORY must use the owner/repository form")
        self.http = http
        self.repository = repository
        self.token = token.strip()
        self.data_branch = data_branch
        self.ref = ref
        self.cache_seconds = max(0, cache_seconds)
        self._cache_lock = threading.Lock()
        self._cache_time = 0.0
        self._cache: list[dict[str, Any]] = []
        self._cache_limit = 0
        self._cache_complete = False

    @property
    def headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def dispatch(self, mode: str) -> None:
        workflow = WORKFLOWS.get(mode)
        if workflow is None:
            raise ValueError(f"unsupported workflow mode: {mode}")
        if not self.token:
            raise RuntimeError("GITHUB_TOKEN is required to dispatch workflows")
        url = (
            f"https://api.github.com/repos/{self.repository}/actions/workflows/"
            f"{quote(workflow, safe='')}/dispatches"
        )
        self.http.bytes(
            url,
            method="POST",
            headers=self.headers,
            body={"ref": self.ref},
        )

    def _stored_reports(self) -> list[StoredReport]:
        branch = quote(self.data_branch, safe="")
        payload = self.http.json(
            f"https://api.github.com/repos/{self.repository}/git/trees/{branch}?recursive=1",
            headers=self.headers,
        )
        if not isinstance(payload, dict) or payload.get("truncated"):
            raise RuntimeError("GitHub data tree is unavailable or truncated")
        tree = payload.get("tree")
        if not isinstance(tree, list):
            raise RuntimeError("GitHub data tree response is invalid")
        reports = [
            StoredReport(str(item["path"]), str(item["sha"]))
            for item in tree
            if isinstance(item, dict)
            and item.get("type") == "blob"
            and isinstance(item.get("path"), str)
            and REPORT_PATH.fullmatch(item["path"])
            and item.get("sha")
        ]
        return sorted(reports, key=lambda item: item.path, reverse=True)

    def _load_blob(self, report: StoredReport) -> dict[str, Any]:
        payload = self.http.json(
            f"https://api.github.com/repos/{self.repository}/git/blobs/{report.sha}",
            headers=self.headers,
        )
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            raise RuntimeError(f"GitHub report blob is invalid: {report.path}")
        try:
            raw = base64.b64decode(str(payload["content"]), validate=False)
            decoded = json.loads(raw.decode("utf-8"))
        except (KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"GitHub report JSON is invalid: {report.path}") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError(f"GitHub report root is invalid: {report.path}")
        return decoded

    def reports(self, limit: int = 30, *, refresh: bool = False) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        now = time.monotonic()
        with self._cache_lock:
            cache_is_large_enough = self._cache_complete or self._cache_limit >= limit
            if (
                not refresh
                and self._cache
                and cache_is_large_enough
                and now - self._cache_time < self.cache_seconds
            ):
                return self._cache[:limit]

        paths = self._stored_reports()
        # At most three reports exist per trading date. Loading a few extra paths
        # ensures generated_at sorting does not omit a later stage.
        loaded = [self._load_blob(item) for item in paths[: min(len(paths), limit + 3)]]
        loaded.sort(key=lambda item: str(item.get("generated_at", "")), reverse=True)
        with self._cache_lock:
            self._cache = loaded
            self._cache_time = now
            self._cache_limit = limit
            self._cache_complete = len(paths) <= limit + 3
        return loaded[:limit]

    def report(self, market_date: str, mode: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", market_date) or mode not in WORKFLOWS:
            return None
        expected = f"reports/{market_date}/{mode}.json"
        report = next((item for item in self._stored_reports() if item.path == expected), None)
        return self._load_blob(report) if report else None
