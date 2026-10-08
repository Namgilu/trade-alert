from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .github_repository import GitHubRepository, WORKFLOWS
from .http import HttpClient
from .scheduler import WORKFLOW_SCHEDULES, create_scheduler


LOGGER = logging.getLogger(__name__)


def _boolean(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def create_app(repository: GitHubRepository | None = None):
    try:
        from fastapi import FastAPI, Header, HTTPException, Query
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:  # pragma: no cover - depends on optional server extra
        raise RuntimeError("install the server dependencies with: pip install '.[server]'") from exc

    github = repository or GitHubRepository(
        HttpClient(int(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))),
        os.getenv("GITHUB_REPOSITORY", "Namgilu/trade-alert"),
        os.getenv("GITHUB_TOKEN", ""),
        data_branch=os.getenv("GITHUB_DATA_BRANCH", "data"),
        ref=os.getenv("GITHUB_WORKFLOW_REF", "main"),
        cache_seconds=int(os.getenv("REPORT_CACHE_SECONDS", "300")),
    )
    scheduler_enabled = _boolean("SCHEDULER_ENABLED", False)
    if scheduler_enabled and not github.token:
        raise RuntimeError("GITHUB_TOKEN is required when SCHEDULER_ENABLED=true")
    admin_token = os.getenv("WEB_ADMIN_TOKEN", "").strip()
    scheduler: Any = None

    @asynccontextmanager
    async def lifespan(_: Any):
        nonlocal scheduler
        if scheduler_enabled:
            scheduler = create_scheduler(github.dispatch)
            scheduler.start()
        yield
        if scheduler is not None:
            scheduler.shutdown(wait=False)

    app = FastAPI(
        title="국장 테마 알림",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "repository": github.repository,
            "data_branch": github.data_branch,
            "scheduler_enabled": scheduler_enabled,
        }

    @app.get("/api/schedules")
    def schedules() -> dict[str, Any]:
        return {
            "timezone": "Asia/Seoul",
            "enabled": scheduler_enabled,
            "items": [
                {
                    "mode": item.mode,
                    "workflow": item.workflow,
                    "time": f"{item.hour:02d}:{item.minute:02d}",
                    "weekdays": "mon-fri",
                    "label": item.label,
                }
                for item in WORKFLOW_SCHEDULES
            ],
        }

    @app.get("/api/reports")
    def reports(limit: int = Query(30, ge=1, le=100), refresh: bool = False):
        try:
            return {"reports": github.reports(limit, refresh=refresh)}
        except RuntimeError as exc:
            LOGGER.exception("failed to load reports from GitHub")
            raise HTTPException(
                status_code=502,
                detail="GitHub에서 분석 결과를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.",
            ) from exc

    @app.get("/api/reports/{market_date}/{mode}")
    def report(market_date: str, mode: str):
        try:
            result = github.report(market_date, mode)
        except RuntimeError as exc:
            LOGGER.exception("failed to load %s/%s from GitHub", market_date, mode)
            raise HTTPException(
                status_code=502,
                detail="GitHub에서 분석 결과를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.",
            ) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="report not found")
        return result

    @app.post("/api/dispatch/{mode}", status_code=202)
    def dispatch(mode: str, x_admin_token: str | None = Header(default=None)):
        if mode not in WORKFLOWS:
            raise HTTPException(status_code=404, detail="unknown workflow mode")
        if not admin_token:
            raise HTTPException(status_code=503, detail="manual dispatch is disabled")
        if not x_admin_token or not hmac.compare_digest(x_admin_token, admin_token):
            raise HTTPException(status_code=401, detail="invalid admin token")
        try:
            github.dispatch(mode)
        except RuntimeError as exc:
            LOGGER.exception("failed to dispatch %s", mode)
            raise HTTPException(status_code=502, detail="GitHub 워크플로 호출에 실패했습니다.") from exc
        return {"accepted": True, "mode": mode}

    static_root = Path(__file__).with_name("static")
    app.mount("/assets", StaticFiles(directory=static_root), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static_root / "index.html")

    # Keep the same asset URLs when the dashboard is served by Firebase Hosting
    # or by this optional FastAPI server.
    app.mount("/", StaticFiles(directory=static_root), name="static")

    return app


def main() -> None:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - depends on optional server extra
        raise RuntimeError("install the server dependencies with: pip install '.[server]'") from exc
    uvicorn.run(
        "trade_alert.webapp:create_app",
        factory=True,
        host=os.getenv("WEB_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
