"""Resolve live snapshot dates from exchange daily bars, never wall-clock dates."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import Stock

KST = ZoneInfo("Asia/Seoul")


def resolve_market_date(now: datetime, market_data, requested: date | None = None) -> date:
    if now.tzinfo is None:
        raise ValueError("collection time must be timezone-aware")
    local = now.astimezone(KST)
    # Live Naver rankings can already contain the next session's intraday data.
    # Conservatively reject this window even on holidays and special opening days.
    if time(8) <= local.time().replace(tzinfo=None) < time(16, 10):
        raise ValueError("live collection is allowed only after 16:10 or before 08:00 KST")
    if market_data is None or not market_data.enabled:
        raise ValueError("collection date verification requires KIS credentials")
    end = local.date() if local.hour >= 16 else local.date() - timedelta(days=1)
    dates = []
    for stock in (Stock("005930", "삼성전자"), Stock("000660", "SK하이닉스")):
        bars = market_data.history(stock, end - timedelta(days=30), end)
        valid = [bar.date for bar in bars if bar.date <= end and bar.volume > 0]
        if not valid:
            raise ValueError("cannot verify latest trading session")
        dates.append(max(valid))
    if len(set(dates)) != 1:
        raise ValueError("reference stocks disagree on latest trading session")
    resolved = dates[0]
    if requested is not None and requested != resolved:
        raise ValueError(f"requested market date {requested} differs from live session {resolved}; live backfill is unsafe")
    return resolved
