from __future__ import annotations

import argparse
import sys

from .config import Settings
from .http import HttpClient
from .providers import JevSentimentProvider, NaverNewsProvider, NaverThemeProvider, TelegramNotifier
from .service import MorningAlertService, format_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send the morning Korean stock theme sentiment report")
    parser.add_argument("--dry-run", action="store_true", help="print the report without sending Telegram")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env(require_telegram=not args.dry_run)
        http = HttpClient(settings.http_timeout_seconds)
        service = MorningAlertService(
            NaverThemeProvider(http, settings.theme_list_url, settings.theme_stocks_url_template),
            NaverNewsProvider(http, settings.naver_client_id, settings.naver_client_secret),
            JevSentimentProvider(http, settings.jev_api_url, settings.jev_api_key, settings.jev_model),
            theme_limit=settings.theme_limit,
            stocks_per_theme=settings.stocks_per_theme,
            news_per_stock=settings.news_per_stock,
            news_lookback_hours=settings.news_lookback_hours,
        )
        message = format_report(service.run())
        print(message)
        if not args.dry_run:
            TelegramNotifier(http, settings.telegram_bot_token, settings.telegram_chat_id).send(message)
        return 0
    except Exception as exc:
        print(f"trade-alert failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
