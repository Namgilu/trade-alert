from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Stock:
    code: str
    name: str
    change_rate: float | None = None


@dataclass(frozen=True)
class Theme:
    id: str
    name: str
    change_rate: float | None = None
    stocks: tuple[Stock, ...] = ()


@dataclass(frozen=True)
class NewsArticle:
    title: str
    description: str
    url: str
    published_at: datetime


@dataclass(frozen=True)
class Sentiment:
    positive: float
    neutral: float
    negative: float
    article_count: int

    @property
    def outlook(self) -> str:
        score = self.positive - self.negative
        if score >= 0.35:
            return "강한 긍정"
        if score >= 0.12:
            return "긍정"
        if score <= -0.35:
            return "강한 부정"
        if score <= -0.12:
            return "부정"
        return "중립"


@dataclass(frozen=True)
class StockAnalysis:
    stock: Stock
    sentiment: Sentiment | None
    articles: tuple[NewsArticle, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class ThemeAnalysis:
    theme: Theme
    stocks: tuple[StockAnalysis, ...] = ()


@dataclass(frozen=True)
class DailyReport:
    generated_at: datetime
    themes: tuple[ThemeAnalysis, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
