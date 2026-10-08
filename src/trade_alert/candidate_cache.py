from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .models import Stock, Theme, ThemePattern


# v4 stores the final morning selection, in display order (not the screening pool).
CACHE_VERSION = 4


def _number(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid candidate cache field: {field}") from exc


def _optional_number(value: Any, field: str) -> float | None:
    return None if value is None else _number(value, field)


def _integer(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid candidate cache field: {field}") from exc


def save_candidates(
    path: Path,
    market_date: date,
    candidates: list[tuple[Theme, ThemePattern]],
) -> None:
    payload = {
        "version": CACHE_VERSION,
        "market_date": market_date.isoformat(),
        "candidates": [
            {
                "theme": {
                    "id": theme.id,
                    "name": theme.name,
                    "change_rate": theme.change_rate,
                    "breadth": theme.breadth,
                    "trading_value": theme.trading_value,
                    "stocks": [{"code": stock.code, "name": stock.name} for stock in theme.stocks],
                },
                "pattern": {
                    "peak_return": pattern.peak_return,
                    "drawdown": pattern.drawdown,
                    "consolidation_range": pattern.consolidation_range,
                    "down_volume_ratio": pattern.down_volume_ratio,
                    "trend_breadth": pattern.trend_breadth,
                    "score": pattern.score,
                    "turnover_spike_ratio": pattern.turnover_spike_ratio,
                    "days_since_turnover_spike": pattern.days_since_turnover_spike,
                    "turnover_cooldown_ratio": pattern.turnover_cooldown_ratio,
                },
            }
            for theme, pattern in candidates
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_candidates(path: Path, market_date: date) -> list[tuple[Theme, ThemePattern]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("candidate cache root must be an object")
    if payload.get("version") != CACHE_VERSION:
        raise ValueError("unsupported candidate cache version")
    if payload.get("market_date") != market_date.isoformat():
        raise ValueError("candidate cache is not for today's Korean market date")

    rows = payload.get("candidates")
    if not isinstance(rows, list):
        raise ValueError("candidate cache candidates must be a list")
    candidates: list[tuple[Theme, ThemePattern]] = []
    seen_ids: set[str] = set()
    if len(rows) > 5:
        raise ValueError("daily selection must contain at most five themes")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("candidate cache row must be an object")
        theme_data = row.get("theme", {})
        pattern_data = row.get("pattern", {})
        if not isinstance(theme_data, dict) or not isinstance(pattern_data, dict):
            raise ValueError("candidate cache theme and pattern must be objects")
        theme_id = str(theme_data.get("id", "")).strip()
        theme_name = str(theme_data.get("name", "")).strip()
        if not theme_id or not theme_name:
            raise ValueError("candidate cache contains a theme without an id or name")
        if theme_id in seen_ids:
            raise ValueError("daily selection contains duplicate theme ids")
        seen_ids.add(theme_id)
        stock_rows = theme_data.get("stocks", [])
        if not isinstance(stock_rows, list):
            raise ValueError("candidate cache stocks must be a list")
        stocks = tuple(
            Stock(str(stock.get("code", "")).strip(), str(stock.get("name", "")).strip())
            for stock in stock_rows
            if isinstance(stock, dict)
            if stock.get("code") and stock.get("name")
        )
        theme = Theme(
            id=theme_id,
            name=theme_name,
            change_rate=_optional_number(theme_data.get("change_rate"), "change_rate"),
            breadth=_optional_number(theme_data.get("breadth"), "breadth"),
            trading_value=_optional_number(theme_data.get("trading_value"), "trading_value"),
            stocks=stocks,
        )
        pattern = ThemePattern(
            peak_return=_number(pattern_data.get("peak_return"), "peak_return"),
            drawdown=_number(pattern_data.get("drawdown"), "drawdown"),
            consolidation_range=_number(pattern_data.get("consolidation_range"), "consolidation_range"),
            down_volume_ratio=_number(pattern_data.get("down_volume_ratio"), "down_volume_ratio"),
            trend_breadth=_number(pattern_data.get("trend_breadth"), "trend_breadth"),
            score=_number(pattern_data.get("score"), "score"),
            turnover_spike_ratio=_number(
                pattern_data.get("turnover_spike_ratio", 1.0), "turnover_spike_ratio"
            ),
            days_since_turnover_spike=_integer(
                pattern_data.get("days_since_turnover_spike", 0), "days_since_turnover_spike"
            ),
            turnover_cooldown_ratio=_number(
                pattern_data.get("turnover_cooldown_ratio", 1.0), "turnover_cooldown_ratio"
            ),
        )
        candidates.append((theme, pattern))
    return candidates
