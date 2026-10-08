from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


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
class DailyBar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class ThemePattern:
    peak_return: float
    drawdown: float
    consolidation_range: float
    down_volume_ratio: float
    trend_breadth: float
    score: float = 0.0
    turnover_spike_ratio: float = 1.0
    days_since_turnover_spike: int = 0
    turnover_cooldown_ratio: float = 1.0


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
class PreopenQuote:
    expected_price: float | None = None
    expected_change_rate: float | None = None
    expected_volume: float | None = None
    total_ask_volume: float | None = None
    total_bid_volume: float | None = None

    @property
    def expected_trade_amount(self) -> float | None:
        if self.expected_price is None or self.expected_volume is None:
            return None
        return self.expected_price * self.expected_volume

    @property
    def bid_ask_ratio(self) -> float | None:
        if self.total_bid_volume is None or not self.total_ask_volume:
            return None
        return self.total_bid_volume / self.total_ask_volume

    @property
    def order_imbalance(self) -> float | None:
        if self.total_bid_volume is None or self.total_ask_volume is None:
            return None
        total = self.total_bid_volume + self.total_ask_volume
        if total <= 0:
            return None
        return (self.total_bid_volume - self.total_ask_volume) / total


@dataclass(frozen=True)
class StockAnalysis:
    stock: Stock
    event_summary: EventSummary | None
    events: tuple[MarketEvent, ...] = ()
    score: float = 0.0
    signal: str = "관망"
    error: str | None = None
    preopen_quote: PreopenQuote | None = None


@dataclass(frozen=True)
class ThemeAnalysis:
    theme: Theme
    stocks: tuple[StockAnalysis, ...] = ()
    score: float | None = 0.0
    pattern: ThemePattern | None = None
    data_error: bool = False
    summary_unavailable: bool = False


@dataclass(frozen=True)
class DailyReport:
    generated_at: datetime
    mode: str
    themes: tuple[ThemeAnalysis, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
