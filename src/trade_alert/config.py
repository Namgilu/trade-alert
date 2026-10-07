from __future__ import annotations

import os
from dataclasses import dataclass


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    naver_client_id: str
    naver_client_secret: str
    jev_api_key: str
    jev_api_url: str
    jev_model: str
    dart_api_key: str
    kiwoom_app_key: str
    kiwoom_app_secret: str
    kiwoom_base_url: str
    telegram_bot_token: str
    telegram_chat_id: str
    theme_limit: int
    theme_candidate_pool: int
    theme_scan_limit: int
    theme_screen_stocks: int
    stocks_per_theme: int
    news_per_stock: int
    max_events_per_stock: int
    news_lookback_hours: int
    history_lookback_days: int
    history_min_bars: int
    http_timeout_seconds: int
    theme_list_url: str
    theme_stocks_url_template: str

    @classmethod
    def from_env(cls, *, require_telegram: bool = True) -> "Settings":
        token = _required("TELEGRAM_BOT_TOKEN") if require_telegram else os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = _required("TELEGRAM_CHAT_ID") if require_telegram else os.getenv("TELEGRAM_CHAT_ID", "")
        return cls(
            naver_client_id=_required("NAVER_CLIENT_ID"),
            naver_client_secret=_required("NAVER_CLIENT_SECRET"),
            jev_api_key=_required("JEV_API_KEY"),
            jev_api_url=os.getenv("JEV_API_URL", "https://api.typesafe.ai/v1/systemone"),
            jev_model=os.getenv("JEV_MODEL", "jev-latest"),
            dart_api_key=os.getenv("DART_API_KEY", "").strip(),
            kiwoom_app_key=os.getenv("KIWOOM_APP_KEY", "").strip(),
            kiwoom_app_secret=os.getenv("KIWOOM_APP_SECRET", "").strip(),
            kiwoom_base_url=os.getenv("KIWOOM_BASE_URL", "https://api.kiwoom.com").strip(),
            telegram_bot_token=token,
            telegram_chat_id=chat_id,
            theme_limit=min(_positive_int("THEME_LIMIT", 3), 3),
            theme_candidate_pool=min(_positive_int("THEME_CANDIDATE_POOL", 5), 10),
            theme_scan_limit=min(_positive_int("THEME_SCAN_LIMIT", 100), 100),
            theme_screen_stocks=min(_positive_int("THEME_SCREEN_STOCKS", 3), 5),
            stocks_per_theme=min(_positive_int("STOCKS_PER_THEME", 3), 3),
            news_per_stock=min(_positive_int("NEWS_PER_STOCK", 10), 100),
            max_events_per_stock=min(_positive_int("MAX_EVENTS_PER_STOCK", 5), 10),
            news_lookback_hours=_positive_int("NEWS_LOOKBACK_HOURS", 24),
            history_lookback_days=_positive_int("HISTORY_LOOKBACK_DAYS", 180),
            history_min_bars=_positive_int("HISTORY_MIN_BARS", 80),
            http_timeout_seconds=_positive_int("HTTP_TIMEOUT_SECONDS", 20),
            theme_list_url=os.getenv(
                "THEME_LIST_URL",
                "https://stock.naver.com/api/domestic/market/theme/list?startIdx=0&pageSize=20&sortType=changeRate",
            ),
            theme_stocks_url_template=os.getenv(
                "THEME_STOCKS_URL_TEMPLATE",
                "https://stock.naver.com/api/domestic/market/theme/{theme_id}/stocklist?marketType=ALL&orderType=up&startIdx=0&pageSize=20",
            ),
        )
