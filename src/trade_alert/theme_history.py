from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any

from .models import DailyBar, Stock, Theme


HISTORY_VERSION = 1


def _float(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid theme history field: {field}") from exc


@dataclass(frozen=True)
class ThemeDailyPoint:
    date: date
    close: float
    high: float
    low: float
    turnover: float


@dataclass(frozen=True)
class ThemeSeries:
    theme: Theme
    points: tuple[ThemeDailyPoint, ...] = ()


class ThemeHistoryStore:
    def __init__(self, series: dict[str, ThemeSeries] | None = None) -> None:
        self.series = series or {}

    def ready(self, minimum_points: int) -> bool:
        return any(len(item.points) >= minimum_points for item in self.series.values())

    def fresh(self, market_date: date, maximum_age_days: int = 10) -> bool:
        latest_dates = [item.points[-1].date for item in self.series.values() if item.points]
        return bool(latest_dates) and (market_date - max(latest_dates)).days <= maximum_age_days

    def snapshot_unchanged(self, themes: list[Theme]) -> bool:
        comparisons = 0
        unchanged = 0
        for theme in themes:
            existing = self.series.get(theme.id)
            if existing is None:
                continue
            comparisons += 1
            if (
                theme.change_rate == existing.theme.change_rate
                and theme.trading_value == existing.theme.trading_value
                and theme.breadth == existing.theme.breadth
            ):
                unchanged += 1
        return comparisons >= 10 and unchanged / comparisons >= 0.95

    def add_bootstrap(
        self,
        theme: Theme,
        histories: list[tuple[Stock, tuple[DailyBar, ...]]],
    ) -> None:
        normalized: list[tuple[dict[date, DailyBar], float]] = []
        for _, bars in histories:
            if not bars or bars[0].close <= 0:
                continue
            normalized.append(({bar.date: bar for bar in bars}, bars[0].close))
        if not normalized:
            return

        dates = sorted({day for rows, _ in normalized for day in rows})
        points: list[ThemeDailyPoint] = []
        for day in dates:
            closes: list[float] = []
            highs: list[float] = []
            lows: list[float] = []
            turnover = 0.0
            for rows, base_close in normalized:
                bar = rows.get(day)
                if bar is None:
                    continue
                closes.append(bar.close / base_close * 100.0)
                highs.append(bar.high / base_close * 100.0)
                lows.append(bar.low / base_close * 100.0)
                turnover += bar.close * bar.volume
            if closes:
                points.append(
                    ThemeDailyPoint(day, median(closes), median(highs), median(lows), turnover)
                )

        if points and theme.trading_value is not None and theme.trading_value > 0:
            latest_turnover = points[-1].turnover
            if latest_turnover > 0:
                scale = theme.trading_value / latest_turnover
                points = [
                    ThemeDailyPoint(
                        point.date,
                        point.close,
                        point.high,
                        point.low,
                        point.turnover * scale,
                    )
                    for point in points
                ]
        self.series[theme.id] = ThemeSeries(theme, tuple(points))

    def append_snapshot(self, themes: list[Theme], market_date: date) -> None:
        for theme in themes:
            existing = self.series.get(theme.id)
            existing_points = list(existing.points if existing else ())
            if existing_points and existing_points[-1].date == market_date:
                previous_close = existing_points[-2].close if len(existing_points) > 1 else 100.0
            else:
                previous_close = existing_points[-1].close if existing_points else 100.0
            change_rate = theme.change_rate or 0.0
            close = previous_close * (1.0 + change_rate / 100.0)
            point = ThemeDailyPoint(
                market_date,
                close,
                close,
                close,
                max(0.0, theme.trading_value or 0.0),
            )
            points = existing_points
            if points and points[-1].date == market_date:
                points[-1] = point
            else:
                points.append(point)
            stocks = existing.theme.stocks if existing and not theme.stocks else theme.stocks
            self.series[theme.id] = ThemeSeries(
                Theme(
                    theme.id,
                    theme.name,
                    theme.change_rate,
                    theme.breadth,
                    theme.trading_value,
                    stocks,
                ),
                tuple(points),
            )

    def save(self, path: Path) -> None:
        payload = {
            "version": HISTORY_VERSION,
            "themes": [
                {
                    "theme": {
                        "id": item.theme.id,
                        "name": item.theme.name,
                        "change_rate": item.theme.change_rate,
                        "breadth": item.theme.breadth,
                        "trading_value": item.theme.trading_value,
                        "stocks": [
                            {"code": stock.code, "name": stock.name} for stock in item.theme.stocks
                        ],
                    },
                    "points": [
                        {
                            "date": point.date.isoformat(),
                            "close": point.close,
                            "high": point.high,
                            "low": point.low,
                            "turnover": point.turnover,
                        }
                        for point in item.points
                    ],
                }
                for item in self.series.values()
            ],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    @classmethod
    def load(cls, path: Path) -> ThemeHistoryStore:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != HISTORY_VERSION:
            raise ValueError("unsupported theme history version")
        rows = payload.get("themes")
        if not isinstance(rows, list):
            raise ValueError("theme history themes must be a list")

        series: dict[str, ThemeSeries] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("theme history row must be an object")
            theme_data = row.get("theme")
            point_rows = row.get("points")
            if not isinstance(theme_data, dict) or not isinstance(point_rows, list):
                raise ValueError("theme history row is malformed")
            theme_id = str(theme_data.get("id", "")).strip()
            theme_name = str(theme_data.get("name", "")).strip()
            if not theme_id or not theme_name:
                raise ValueError("theme history contains an unnamed theme")
            stock_rows = theme_data.get("stocks", [])
            stocks = tuple(
                Stock(str(stock.get("code", "")).strip(), str(stock.get("name", "")).strip())
                for stock in stock_rows
                if isinstance(stock, dict) and stock.get("code") and stock.get("name")
            )
            theme = Theme(
                theme_id,
                theme_name,
                theme_data.get("change_rate"),
                theme_data.get("breadth"),
                theme_data.get("trading_value"),
                stocks,
            )
            points = tuple(
                ThemeDailyPoint(
                    date.fromisoformat(str(point["date"])),
                    _float(point.get("close"), "close"),
                    _float(point.get("high"), "high"),
                    _float(point.get("low"), "low"),
                    _float(point.get("turnover"), "turnover"),
                )
                for point in point_rows
                if isinstance(point, dict)
            )
            series[theme_id] = ThemeSeries(theme, points)
        return cls(series)
