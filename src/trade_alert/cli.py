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
from .service import MarketAlertService, format_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send the morning Korean stock theme sentiment report")
    parser.add_argument(
        "--mode",
        choices=("premarket", "preopen", "confirmation"),
        default="premarket",
        help="07:30 candidate discovery, 08:55 pre-open checkpoint, or 09:10 market confirmation",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the report without sending Telegram")
    parser.add_argument(
        "--candidate-file",
        help="reuse the 07:30 candidate snapshot for later stages",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env(require_telegram=not args.dry_run)
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
        candidate_path = Path(args.candidate_file) if args.candidate_file else None
        screened = None
        screening_warnings: tuple[str, ...] = ()
        if candidate_path is not None and args.mode != "premarket" and candidate_path.exists():
            try:
                screened = load_candidates(candidate_path, now.date())
            except (OSError, ValueError) as exc:
                print(f"candidate cache ignored: {exc}", file=sys.stderr)
        if screened is None:
            screened, screening_warnings = service.screen_candidates(now)
            if candidate_path is not None:
                save_candidates(candidate_path, now.date(), screened)

        message = format_report(
            service.run(
                args.mode,
                now,
                screened_candidates=screened,
                screening_warnings=screening_warnings,
            )
        )
        print(message)
        if not args.dry_run:
            TelegramNotifier(http, settings.telegram_bot_token, settings.telegram_chat_id).send(message)
        return 0
    except Exception as exc:
        print(f"trade-alert failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
