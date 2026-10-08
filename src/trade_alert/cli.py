from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .candidate_cache import load_candidates, save_candidates
from .config import Settings
from .http import HttpClient
from .providers import (
    JevEventProvider,
    KisMarketDataProvider,
    NaverNewsProvider,
    NaverThemeProvider,
    OpenDartProvider,
    TelegramNotifier,
)
from .report_store import save_report
from .service import MarketAlertService, format_report
from .theme_history import ThemeHistoryStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send the morning Korean stock theme sentiment report")
    parser.add_argument(
        "--mode",
        choices=("premarket", "preopen", "confirmation", "collect"),
        default="premarket",
        help="07:30 discovery, 08:55 checkpoint, 09:10 confirmation, or 16:10 history collection",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the report without sending Telegram")
    parser.add_argument(
        "--candidate-file",
        help="reuse the 07:30 candidate snapshot for later stages",
    )
    parser.add_argument(
        "--history-file",
        help="persist the rolling theme catalog and daily turnover history",
    )
    parser.add_argument(
        "--history-repository",
        help="also export catalog and dated snapshots into a checked-out data branch",
    )
    parser.add_argument(
        "--report-file",
        help="write the complete alert result as JSON for the web dashboard",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env(require_telegram=not args.dry_run and args.mode != "collect")
        http = HttpClient(settings.http_timeout_seconds)
        service = MarketAlertService(
            NaverThemeProvider(http, settings.theme_list_url, settings.theme_stocks_url_template),
            NaverNewsProvider(http, settings.naver_client_id, settings.naver_client_secret),
            OpenDartProvider(http, settings.dart_api_key),
            JevEventProvider(http, settings.jev_api_url, settings.jev_api_key, settings.jev_model),
            KisMarketDataProvider(
                http,
                settings.kis_app_key,
                settings.kis_app_secret,
                settings.kis_base_url,
                request_interval_seconds=0.06,
            ),
            theme_limit=settings.theme_limit,
            theme_candidate_pool=settings.theme_candidate_pool,
            theme_scan_limit=settings.theme_scan_limit,
            theme_screen_stocks=settings.theme_screen_stocks,
            stocks_per_theme=settings.stocks_per_theme,
            news_per_stock=settings.news_per_stock,
            max_events_per_stock=settings.max_events_per_stock,
            news_lookback_hours=settings.news_lookback_hours,
            history_lookback_days=settings.history_lookback_days,
            history_min_bars=settings.history_min_bars,
        )
        now = datetime.now(ZoneInfo("Asia/Seoul"))
        history_repository = Path(args.history_repository) if args.history_repository else None
        history_path = Path(args.history_file) if args.history_file else None
        if history_path is None and history_repository is not None:
            history_path = history_repository / "state" / "theme-history.json"
        history = None
        if history_path is not None and history_path.exists():
            try:
                history = ThemeHistoryStore.load(history_path)
            except (OSError, ValueError) as exc:
                print(f"theme history ignored: {exc}", file=sys.stderr)

        if args.mode == "collect":
            if history_path is None:
                raise ValueError("--history-file is required for collect mode")
            history, warnings, action = service.collect_history(now, history)
            if history_repository is not None and action != "unchanged":
                latest_dates = [
                    item.points[-1].date for item in history.series.values() if item.points
                ]
                repository_date = max(latest_dates, default=now.date())
                written = history.save_repository(history_repository, repository_date)
            elif history_repository is not None:
                written = ()
            else:
                history.save(history_path)
                written = (history_path,)
            print(
                f"theme history {action}: {len(history.series)} themes, "
                f"{sum(len(item.points) for item in history.series.values())} points, "
                f"{len(written)} files"
            )
            if warnings:
                print(f"theme history warnings: {len(warnings)}", file=sys.stderr)
            return 0

        candidate_path = Path(args.candidate_file) if args.candidate_file else None
        screened = None
        screening_warnings: tuple[str, ...] = ()
        if candidate_path is not None and args.mode != "premarket" and candidate_path.exists():
            try:
                screened = load_candidates(candidate_path, now.date())
            except (OSError, ValueError) as exc:
                print(f"candidate cache ignored: {exc}", file=sys.stderr)
        if screened is None:
            screened, screening_warnings, history = service.prepare_candidates(now, history)
            if history_path is not None:
                history.save(history_path)
            if candidate_path is not None:
                save_candidates(candidate_path, now.date(), screened)

        report = service.run(
            args.mode,
            now,
            screened_candidates=screened,
            screening_warnings=screening_warnings,
        )
        message = format_report(report)
        if args.report_file:
            save_report(Path(args.report_file), report, message)
        print(message)
        if not args.dry_run:
            TelegramNotifier(http, settings.telegram_bot_token, settings.telegram_chat_id).send(message)
        return 0
    except Exception as exc:
        print(f"trade-alert failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
