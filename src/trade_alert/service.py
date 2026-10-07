from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import DailyReport, EventSummary, StockAnalysis, ThemeAnalysis
from .providers import (
    JevEventProvider,
    KisPreopenProvider,
    NaverNewsProvider,
    NaverThemeProvider,
    OpenDartProvider,
    deduplicate_events,
)


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


def _preopen_signal(score: float) -> str:
    if score >= 72:
        return "장전 유효"
    if score >= 55:
        return "장전 주의"
    return "장전 제외"


class MarketAlertService:
    def __init__(
        self,
        themes: NaverThemeProvider,
        news: NaverNewsProvider,
        dart: OpenDartProvider,
        event_model: JevEventProvider,
        kis: KisPreopenProvider | None = None,
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
        self.kis = kis
        self.theme_limit = theme_limit
        self.theme_candidate_pool = max(theme_limit, theme_candidate_pool)
        self.stocks_per_theme = stocks_per_theme
        self.news_per_stock = news_per_stock
        self.max_events_per_stock = max_events_per_stock
        self.news_lookback_hours = news_lookback_hours

    def _analyze_stock(
        self,
        stock: Stock,
        now: datetime,
        warnings: list[str],
        *,
        include_preopen: bool,
    ) -> StockAnalysis:
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
        preopen_quote = None
        if include_preopen:
            try:
                if self.kis is None:
                    raise RuntimeError("KIS provider is not configured")
                preopen_quote = self.kis.quote(stock)
            except Exception as exc:
                errors.append(f"KIS {type(exc).__name__}: {exc}")
        if errors:
            warnings.append(f"{stock.name}: {'; '.join(errors)}")
        return StockAnalysis(
            stock=stock,
            event_summary=summary,
            events=tuple(events),
            error="; ".join(errors) or None,
            preopen_quote=preopen_quote,
        )

    def run(self, mode: str, now: datetime | None = None) -> DailyReport:
        if mode not in {"premarket", "preopen", "confirmation"}:
            raise ValueError("mode must be premarket, preopen, or confirmation")
        if mode == "preopen" and (self.kis is None or not self.kis.enabled):
            raise ValueError("08:55 preopen mode requires KIS_APP_KEY and KIS_APP_SECRET")
        now = now or datetime.now(ZoneInfo("Asia/Seoul"))
        candidates = self.themes.top_themes(self.theme_candidate_pool, self.stocks_per_theme)
        warnings: list[str] = []
        raw_analyses: list[ThemeAnalysis] = []
        for theme in candidates:
            stocks = tuple(
                self._analyze_stock(stock, now, warnings, include_preopen=mode == "preopen")
                for stock in theme.stocks
            )
            raw_analyses.append(ThemeAnalysis(theme=theme, stocks=stocks))

        if mode == "preopen" and not any(
            result.preopen_quote is not None for analysis in raw_analyses for result in analysis.stocks
        ):
            raise RuntimeError("KIS did not return pre-open expected execution data for any candidate stock")

        theme_values = [theme.theme.trading_value for theme in raw_analyses if theme.theme.trading_value is not None]
        stock_values = [
            result.stock.trade_amount
            for theme in raw_analyses
            for result in theme.stocks
            if result.stock.trade_amount is not None
        ]
        preopen_stock_values = [
            result.preopen_quote.expected_trade_amount
            for analysis in raw_analyses
            for result in analysis.stocks
            if result.preopen_quote and result.preopen_quote.expected_trade_amount is not None
        ]
        preopen_theme_values = [
            sum(
                result.preopen_quote.expected_trade_amount
                for result in analysis.stocks
                if result.preopen_quote and result.preopen_quote.expected_trade_amount is not None
            )
            for analysis in raw_analyses
        ]
        scored_themes: list[ThemeAnalysis] = []
        for theme_index, analysis in enumerate(raw_analyses):
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
            elif mode == "confirmation":
                theme_score = (
                    0.25 * breadth_score
                    + 0.20 * change_score
                    + 0.20 * trading_score
                    + 0.15 * leader_score
                    + 0.20 * news_score
                )
            else:
                quotes = [result.preopen_quote for result in analysis.stocks if result.preopen_quote]
                expected_rates = [quote.expected_change_rate for quote in quotes if quote.expected_change_rate is not None]
                expected_rate_score = (
                    sum(_scale(rate, -3.0, 10.0) for rate in expected_rates) / len(expected_rates)
                    if expected_rates
                    else 0.0
                )
                expected_breadth = (
                    sum(rate > 0 for rate in expected_rates) / len(expected_rates) * 100.0 if expected_rates else 0.0
                )
                imbalance_scores = [
                    _scale(quote.order_imbalance, -1.0, 1.0)
                    for quote in quotes
                    if quote.order_imbalance is not None
                ]
                orderbook_score = (
                    sum(imbalance_scores) / len(imbalance_scores) if imbalance_scores else 0.0
                )
                prior_theme_score = 0.40 * breadth_score + 0.35 * change_score + 0.25 * leader_score
                theme_score = (
                    0.25 * expected_breadth
                    + 0.20 * expected_rate_score
                    + 0.15 * _relative(preopen_theme_values[theme_index], preopen_theme_values)
                    + 0.10 * orderbook_score
                    + 0.20 * news_score
                    + 0.10 * prior_theme_score
                )

            scored_stocks: list[StockAnalysis] = []
            for stock_result in analysis.stocks:
                stock = stock_result.stock
                event = _event_score(stock_result.event_summary)
                if mode == "premarket":
                    score = 0.55 * event + 0.25 * theme_score + 0.20 * _scale(stock.change_rate, -3.0, 10.0)
                    signal = _premarket_signal(score)
                elif mode == "confirmation":
                    amount = _relative(stock.trade_amount, stock_values)
                    price = _scale(stock.change_rate, -3.0, 10.0)
                    volume = _scale(stock.relative_volume, 0.0, 0.15, default=0.0)
                    score = 0.30 * amount + 0.20 * price + 0.20 * volume + 0.15 * event + 0.15 * theme_score
                    signal = _confirmation_signal(score)
                else:
                    quote = stock_result.preopen_quote
                    price = _scale(quote.expected_change_rate, -3.0, 10.0, default=0.0) if quote else 0.0
                    amount = (
                        _relative(quote.expected_trade_amount, preopen_stock_values)
                        if quote and quote.expected_trade_amount is not None
                        else 0.0
                    )
                    orderbook = (
                        _scale(quote.order_imbalance, -1.0, 1.0, default=0.0)
                        if quote and quote.order_imbalance is not None
                        else 0.0
                    )
                    score = 0.30 * price + 0.20 * amount + 0.20 * orderbook + 0.15 * event + 0.15 * theme_score
                    signal = _preopen_signal(score)
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


def _format_integer(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "-"
    return f"{value:,.0f}{suffix}"


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
    elif report.mode == "preopen":
        lines = [f"⏱️ {report.generated_at:%Y-%m-%d} 08:55 장전 중간확정", ""]
    else:
        lines = [f"🔔 {report.generated_at:%Y-%m-%d} 09:10 장초 최종확인", ""]
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
            elif report.mode == "confirmation":
                relative_volume = f"{stock.relative_volume:.2f}배" if stock.relative_volume is not None else "-"
                lines.append(f"   {stock_rank}) {stock.name}({stock.code}) · {result.signal} {result.score:.0f}")
                lines.append(
                    f"     등락 {rate} · 거래대금 {_format_amount(stock.trade_amount)} · 전일거래량 대비 {relative_volume}"
                )
            else:
                lines.append(f"   {stock_rank}) {stock.name}({stock.code}) · {result.signal} {result.score:.0f}")
                quote = result.preopen_quote
                if quote:
                    expected_rate = (
                        f"{quote.expected_change_rate:+.2f}%" if quote.expected_change_rate is not None else "-"
                    )
                    ratio = f"{quote.bid_ask_ratio:.2f}배" if quote.bid_ask_ratio is not None else "-"
                    lines.append(
                        "     예상 "
                        f"{expected_rate} · 체결가 {_format_integer(quote.expected_price, '원')}"
                        f" · 거래량 {_format_integer(quote.expected_volume, '주')}"
                    )
                    lines.append(f"     매수/매도 잔량비 {ratio}")
                else:
                    lines.append("     예상체결 데이터 없음")
            lines.extend(_event_lines(result.event_summary))
        lines.append("")
    if report.warnings:
        lines.append(f"⚠️ 일부 데이터 처리 실패: {len(report.warnings)}건")
    if report.mode == "premarket":
        lines.append("※ 장전 후보이며 09:10 시장 확인 전에는 거래 신호가 아닙니다.")
    elif report.mode == "preopen":
        lines.append("※ 동시호가 예상체결 데이터는 09:00 전 바뀔 수 있으며 09:10 최종 확인 전 중간 신호입니다.")
    else:
        lines.append("※ 뉴스·공시·초기 수급 기반 참고 지표이며 투자 권유가 아닙니다.")
    return "\n".join(lines).strip()
