from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import DailyReport, EventSummary, MarketEvent, PreopenQuote, StockAnalysis, ThemeAnalysis


REPORT_VERSION = 1


def _event_payload(event: MarketEvent) -> dict[str, Any]:
    return {
        "title": event.title,
        "url": event.url,
        "published_at": event.published_at.isoformat(),
        "source_kind": event.source_kind,
    }


def _summary_payload(summary: EventSummary | None) -> dict[str, Any] | None:
    if summary is None:
        return None
    return {
        "score": summary.score,
        "direction": summary.direction,
        "relevance": summary.relevance,
        "confirmed": summary.confirmed,
        "event_type": summary.event_type,
        "horizon": summary.horizon,
        "confidence": summary.confidence,
        "event_count": summary.event_count,
        "primary_event": _event_payload(summary.primary_event) if summary.primary_event else None,
    }


def _quote_payload(quote: PreopenQuote | None) -> dict[str, Any] | None:
    if quote is None:
        return None
    return {
        "expected_price": quote.expected_price,
        "expected_change_rate": quote.expected_change_rate,
        "expected_volume": quote.expected_volume,
        "expected_trade_amount": quote.expected_trade_amount,
        "total_ask_volume": quote.total_ask_volume,
        "total_bid_volume": quote.total_bid_volume,
        "bid_ask_ratio": quote.bid_ask_ratio,
        "order_imbalance": quote.order_imbalance,
    }


def _stock_payload(analysis: StockAnalysis) -> dict[str, Any]:
    stock = analysis.stock
    return {
        "code": stock.code,
        "name": stock.name,
        "change_rate": stock.change_rate,
        "trade_amount": stock.trade_amount,
        "trade_volume": stock.trade_volume,
        "previous_volume": stock.previous_volume,
        "relative_volume": stock.relative_volume,
        "score": analysis.score,
        "signal": analysis.signal,
        "event_summary": _summary_payload(analysis.event_summary),
        "events": [_event_payload(event) for event in analysis.events],
        "preopen_quote": _quote_payload(analysis.preopen_quote),
        "has_error": analysis.error is not None,
    }


def _theme_payload(analysis: ThemeAnalysis) -> dict[str, Any]:
    theme = analysis.theme
    pattern = analysis.pattern
    return {
        "id": theme.id,
        "name": theme.name,
        "change_rate": theme.change_rate,
        "breadth": theme.breadth,
        "trading_value": theme.trading_value,
        "score": analysis.score,
        "pattern": (
            {
                "peak_return": pattern.peak_return,
                "drawdown": pattern.drawdown,
                "consolidation_range": pattern.consolidation_range,
                "down_volume_ratio": pattern.down_volume_ratio,
                "trend_breadth": pattern.trend_breadth,
                "score": pattern.score,
                "turnover_spike_ratio": pattern.turnover_spike_ratio,
                "days_since_turnover_spike": pattern.days_since_turnover_spike,
                "turnover_cooldown_ratio": pattern.turnover_cooldown_ratio,
            }
            if pattern
            else None
        ),
        "stocks": [_stock_payload(stock) for stock in analysis.stocks],
    }


def report_payload(report: DailyReport, rendered_text: str) -> dict[str, Any]:
    return {
        "version": REPORT_VERSION,
        "market_date": report.generated_at.date().isoformat(),
        "generated_at": report.generated_at.isoformat(),
        "mode": report.mode,
        "themes": [_theme_payload(theme) for theme in report.themes],
        # Provider errors can contain request URLs. Store only the count so an
        # API key embedded in a third-party query string can never reach Git.
        "warning_count": len(report.warnings),
        "telegram_text": rendered_text,
    }


def save_report(path: Path, report: DailyReport, rendered_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(report_payload(report, rendered_text), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
