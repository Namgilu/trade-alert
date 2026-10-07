from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import DailyReport, EventSummary, StockAnalysis, ThemeAnalysis
from .providers import JevEventProvider, NaverNewsProvider, NaverThemeProvider, OpenDartProvider, deduplicate_events


def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, value))


def _scale(value: float | None, low: float, high: float, default: float = 50.0) -> float:
    if value is None or high <= low:
        return default
    return _clamp((value - low) / (high - low) * 100.0)


def _relative(value: float | None, values: list[float]) -> float:
    if value is None or not values:
        return 0.0
    low, high = min(values), max(values)
    if high == low:
        return 50.0
    return _scale(value, low, high)


def _event_score(summary: EventSummary | None) -> float:
    return 50.0 if summary is None else _clamp((summary.score + 100.0) / 2.0)


def _premarket_signal(score: float) -> str:
    if score >= 70:
        return "장전 우선관심"
    if score >= 55:
        return "장전 관심"
    return "낮은 우선순위"


def _confirmation_signal(score: float) -> str:
    if score >= 75:
        return "거래 확인"
    if score >= 60:
        return "관심 유지"
    if score >= 45:
        return "관망"
    return "신호 무효"


class MarketAlertService:
    def __init__(
        self,
        themes: NaverThemeProvider,
        news: NaverNewsProvider,
        dart: OpenDartProvider,
        event_model: JevEventProvider,
        *,
        theme_limit: int,
        theme_candidate_pool: int,
        stocks_per_theme: int,
        news_per_stock: int,
        max_events_per_stock: int,
        news_lookback_hours: int,
    ) -> None:
        self.themes = themes
        self.news = news
        self.dart = dart
        self.event_model = event_model
        self.theme_limit = theme_limit
        self.theme_candidate_pool = max(theme_limit, theme_candidate_pool)
        self.stocks_per_theme = stocks_per_theme
        self.news_per_stock = news_per_stock
        self.max_events_per_stock = max_events_per_stock
        self.news_lookback_hours = news_lookback_hours

    def _analyze_stock(self, stock: Stock, now: datetime, warnings: list[str]) -> StockAnalysis:
        events = []
        errors: list[str] = []
        try:
            events.extend(self.news.recent(stock, self.news_per_stock, self.news_lookback_hours, now))
        except Exception as exc:
            errors.append(f"뉴스 {type(exc).__name__}: {exc}")
        if self.dart.enabled:
            try:
                events.extend(self.dart.recent(stock, self.news_lookback_hours, now))
            except Exception as exc:
                errors.append(f"공시 {type(exc).__name__}: {exc}")
        events = deduplicate_events(events, self.max_events_per_stock)
        summary = None
        if events:
            try:
                summary = self.event_model.analyze(stock, events, now)
            except Exception as exc:
                errors.append(f"JEV {type(exc).__name__}: {exc}")
        if errors:
            warnings.append(f"{stock.name}: {'; '.join(errors)}")
        return StockAnalysis(stock=stock, event_summary=summary, events=tuple(events), error="; ".join(errors) or None)

    def run(self, mode: str, now: datetime | None = None) -> DailyReport:
        if mode not in {"premarket", "confirmation"}:
            raise ValueError("mode must be premarket or confirmation")
        now = now or datetime.now(ZoneInfo("Asia/Seoul"))
        candidates = self.themes.top_themes(self.theme_candidate_pool, self.stocks_per_theme)
        warnings: list[str] = []
        raw_analyses: list[ThemeAnalysis] = []
        for theme in candidates:
            stocks = tuple(self._analyze_stock(stock, now, warnings) for stock in theme.stocks)
            raw_analyses.append(ThemeAnalysis(theme=theme, stocks=stocks))

        theme_values = [theme.theme.trading_value for theme in raw_analyses if theme.theme.trading_value is not None]
        stock_values = [
            result.stock.trade_amount
            for theme in raw_analyses
            for result in theme.stocks
            if result.stock.trade_amount is not None
        ]
        scored_themes: list[ThemeAnalysis] = []
        for analysis in raw_analyses:
            theme = analysis.theme
            breadth_score = (theme.breadth if theme.breadth is not None else 0.5) * 100.0
            change_score = _scale(theme.change_rate, -2.0, 8.0)
            trading_score = _relative(theme.trading_value, theme_values)
            leader_change = max(
                (stock.stock.change_rate for stock in analysis.stocks if stock.stock.change_rate is not None),
                default=None,
            )
            leader_score = _scale(leader_change, -2.0, 12.0)
            event_scores = [_event_score(stock.event_summary) for stock in analysis.stocks]
            news_score = sum(event_scores) / len(event_scores) if event_scores else 50.0
            if mode == "premarket":
                theme_score = (
                    0.25 * breadth_score
                    + 0.15 * change_score
                    + 0.10 * trading_score
                    + 0.10 * leader_score
                    + 0.40 * news_score
                )
            else:
                theme_score = (
                    0.25 * breadth_score
                    + 0.20 * change_score
                    + 0.20 * trading_score
                    + 0.15 * leader_score
                    + 0.20 * news_score
                )

            scored_stocks: list[StockAnalysis] = []
            for stock_result in analysis.stocks:
                stock = stock_result.stock
                event = _event_score(stock_result.event_summary)
                if mode == "premarket":
                    score = 0.55 * event + 0.25 * theme_score + 0.20 * _scale(stock.change_rate, -3.0, 10.0)
                    signal = _premarket_signal(score)
                else:
                    amount = _relative(stock.trade_amount, stock_values)
                    price = _scale(stock.change_rate, -3.0, 10.0)
                    volume = _scale(stock.relative_volume, 0.0, 0.15, default=0.0)
                    score = 0.30 * amount + 0.20 * price + 0.20 * volume + 0.15 * event + 0.15 * theme_score
                    signal = _confirmation_signal(score)
                scored_stocks.append(replace(stock_result, score=_clamp(score), signal=signal))
            scored_stocks.sort(key=lambda item: item.score, reverse=True)
            scored_themes.append(ThemeAnalysis(theme=theme, stocks=tuple(scored_stocks), score=_clamp(theme_score)))

        scored_themes.sort(key=lambda item: item.score, reverse=True)
        return DailyReport(now, mode, tuple(scored_themes[: self.theme_limit]), tuple(warnings))


_EVENT_TYPE_LABELS = {
    "earnings": "실적",
    "contract": "수주·계약",
    "policy": "정책·규제",
    "product": "제품·기술",
    "financing": "자금조달",
    "governance": "지분·경영권",
    "legal_risk": "법률·사고",
    "market_commentary": "시황·반복보도",
}
_HORIZON_LABELS = {"open": "시초가", "intraday": "당일", "short_term": "단기", "long_term": "중장기"}


def _format_amount(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value / 100_000_000:.0f}억"


def _event_lines(summary: EventSummary | None) -> list[str]:
    if summary is None:
        return ["     이벤트: 분석할 신규 뉴스·공시 없음"]
    event_type = _EVENT_TYPE_LABELS.get(summary.event_type, summary.event_type)
    horizon = _HORIZON_LABELS.get(summary.horizon, summary.horizon)
    lines = [f"     이벤트: {summary.direction} {summary.score:+.0f} · {event_type} · {horizon}"]
    if summary.primary_event:
        source = "공시" if summary.primary_event.source_kind == "disclosure" else "뉴스"
        lines.append(f"     핵심 {source}: {summary.primary_event.title[:68]}")
    return lines


def format_report(report: DailyReport) -> str:
    if report.mode == "premarket":
        lines = [f"🌅 {report.generated_at:%Y-%m-%d} 07:30 장전 관심 후보", ""]
    else:
        lines = [f"🔔 {report.generated_at:%Y-%m-%d} 09:10 시장 확인", ""]
    if not report.themes:
        lines.append("조회된 테마가 없습니다.")
    for theme_rank, analysis in enumerate(report.themes, 1):
        theme = analysis.theme
        rate = f"{theme.change_rate:+.2f}%" if theme.change_rate is not None else "-"
        breadth = f"{theme.breadth:.0%}" if theme.breadth is not None else "-"
        lines.append(f"{theme_rank}. {theme.name} · 강도 {analysis.score:.0f}/100")
        lines.append(f"   등락 {rate} · 확산도 {breadth}")
        for stock_rank, result in enumerate(analysis.stocks, 1):
            stock = result.stock
            rate = f"{stock.change_rate:+.2f}%" if stock.change_rate is not None else "-"
            if report.mode == "premarket":
                lines.append(
                    f"   {stock_rank}) {stock.name}({stock.code}) · {result.signal} {result.score:.0f} · 전일 {rate}"
                )
            else:
                relative_volume = f"{stock.relative_volume:.2f}배" if stock.relative_volume is not None else "-"
                lines.append(f"   {stock_rank}) {stock.name}({stock.code}) · {result.signal} {result.score:.0f}")
                lines.append(
                    f"     등락 {rate} · 거래대금 {_format_amount(stock.trade_amount)} · 전일거래량 대비 {relative_volume}"
                )
            lines.extend(_event_lines(result.event_summary))
        lines.append("")
    if report.warnings:
        lines.append(f"⚠️ 일부 데이터 처리 실패: {len(report.warnings)}건")
    if report.mode == "premarket":
        lines.append("※ 장전 후보이며 09:10 시장 확인 전에는 거래 신호가 아닙니다.")
    else:
        lines.append("※ 뉴스·공시·초기 수급 기반 참고 지표이며 투자 권유가 아닙니다.")
    return "\n".join(lines).strip()
