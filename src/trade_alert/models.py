from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Stock:
    code: str
    name: str
    change_rate: float | None = None
    trade_amount: float | None = None
    trade_volume: float | None = None
    previous_volume: float | None = None

    @property
    def relative_volume(self) -> float | None:
        if not self.trade_volume or not self.previous_volume:
            return None
        return self.trade_volume / self.previous_volume


@dataclass(frozen=True)
class Theme:
    id: str
    name: str
    change_rate: float | None = None
    breadth: float | None = None
    trading_value: float | None = None
    stocks: tuple[Stock, ...] = ()


@dataclass(frozen=True)
class MarketEvent:
    title: str
    description: str
    url: str
    published_at: datetime
    source_kind: str = "news"


@dataclass(frozen=True)
class EventSummary:
    score: float
    relevance: float
    confirmed: float
    event_type: str
    horizon: str
    confidence: float
    event_count: int
    primary_event: MarketEvent | None = None

    @property
    def direction(self) -> str:
        if self.score >= 45:
            return "강한 호재"
        if self.score >= 15:
            return "호재"
        if self.score <= -45:
            return "강한 악재"
        if self.score <= -15:
            return "악재"
        return "중립"


@dataclass(frozen=True)
class StockAnalysis:
    stock: Stock
    event_summary: EventSummary | None
    events: tuple[MarketEvent, ...] = ()
    score: float = 0.0
    signal: str = "관망"
    error: str | None = None


@dataclass(frozen=True)
class ThemeAnalysis:
    theme: Theme
    stocks: tuple[StockAnalysis, ...] = ()
    score: float = 0.0


@dataclass(frozen=True)
class DailyReport:
    generated_at: datetime
    mode: str
    themes: tuple[ThemeAnalysis, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
