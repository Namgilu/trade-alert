from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from .models import DailyBar, DailyReport, EventSummary, Stock, StockAnalysis, Theme, ThemeAnalysis, ThemePattern
from .providers import (
    JevEventProvider,
    KisMarketDataProvider,
    NaverNewsProvider,
    NaverThemeProvider,
    OpenDartProvider,
    deduplicate_events,
)
from .theme_history import ThemeDailyPoint, ThemeHistoryStore


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


def _inverse_scale(value: float, best: float, worst: float) -> float:
    if worst <= best:
        return 0.0
    return _clamp((worst - value) / (worst - best) * 100.0)


def _drawdown_score(drawdown: float) -> float:
    depth = abs(drawdown)
    if depth < 5.0 or depth > 30.0:
        return 0.0
    if depth <= 15.0:
        return _scale(depth, 5.0, 15.0, default=0.0)
    return _inverse_scale(depth, 15.0, 30.0)


@dataclass(frozen=True)
class _StockPattern:
    peak_return: float
    drawdown: float
    consolidation_range: float
    down_volume_ratio: float
    above_sma60: bool


def _stock_pattern(bars: tuple[DailyBar, ...], min_bars: int) -> _StockPattern | None:
    if len(bars) < min_bars:
        return None
    sample = bars[-65:]
    start_close = sample[0].close
    current_close = sample[-1].close
    peak_close = max(bar.close for bar in sample)
    if start_close <= 0 or peak_close <= 0:
        return None
    recent = sample[-15:]
    recent_low = min(bar.low for bar in recent)
    consolidation_range = (max(bar.high for bar in recent) / recent_low - 1.0) * 100.0
    prior_volume_window = sample[-30:-10]
    prior_average_volume = (
        sum(bar.volume for bar in prior_volume_window) / len(prior_volume_window)
        if prior_volume_window
        else 0.0
    )
    recent_down_volumes = [
        sample[index].volume
        for index in range(max(1, len(sample) - 10), len(sample))
        if sample[index].close < sample[index - 1].close
    ]
    down_volume = (
        sum(recent_down_volumes) / len(recent_down_volumes)
        if recent_down_volumes
        else 0.0
    )
    down_volume_ratio = down_volume / prior_average_volume if prior_average_volume > 0 else 1.0
    sma60 = sum(bar.close for bar in sample[-60:]) / min(60, len(sample))
    return _StockPattern(
        peak_return=(peak_close / start_close - 1.0) * 100.0,
        drawdown=(current_close / peak_close - 1.0) * 100.0,
        consolidation_range=consolidation_range,
        down_volume_ratio=down_volume_ratio,
        above_sma60=current_close >= sma60,
    )


def _theme_pattern(patterns: list[_StockPattern]) -> ThemePattern:
    return ThemePattern(
        peak_return=median(pattern.peak_return for pattern in patterns),
        drawdown=median(pattern.drawdown for pattern in patterns),
        consolidation_range=median(pattern.consolidation_range for pattern in patterns),
        down_volume_ratio=median(pattern.down_volume_ratio for pattern in patterns),
        trend_breadth=sum(pattern.above_sma60 for pattern in patterns) / len(patterns),
    )


def _history_pattern(points: tuple[ThemeDailyPoint, ...], min_bars: int) -> ThemePattern | None:
    if len(points) < min_bars:
        return None
    sample = points[-65:]
    start_close = sample[0].close
    current_close = sample[-1].close
    peak_close = max(point.close for point in sample)
    if start_close <= 0 or peak_close <= 0:
        return None

    recent = sample[-15:]
    recent_low = min(point.low for point in recent)
    if recent_low <= 0:
        return None
    consolidation_range = (max(point.high for point in recent) / recent_low - 1.0) * 100.0
    prior_turnover_window = sample[-30:-10]
    prior_average_turnover = (
        sum(point.turnover for point in prior_turnover_window) / len(prior_turnover_window)
        if prior_turnover_window
        else 0.0
    )
    recent_down_turnovers = [
        sample[index].turnover
        for index in range(max(1, len(sample) - 10), len(sample))
        if sample[index].close < sample[index - 1].close
    ]
    down_turnover = (
        sum(recent_down_turnovers) / len(recent_down_turnovers)
        if recent_down_turnovers
        else 0.0
    )
    down_turnover_ratio = (
        down_turnover / prior_average_turnover if prior_average_turnover > 0 else 1.0
    )

    spikes: list[tuple[float, int]] = []
    for index in range(20, len(sample)):
        baseline = median(point.turnover for point in sample[index - 20:index])
        if baseline > 0:
            spikes.append((sample[index].turnover / baseline, index))
    spike_ratio, spike_index = max(spikes, default=(1.0, len(sample) - 1))
    spike_turnover = sample[spike_index].turnover
    recent_turnover = median(point.turnover for point in sample[-5:])
    cooldown_ratio = recent_turnover / spike_turnover if spike_turnover > 0 else 1.0
    sma60 = sum(point.close for point in sample[-60:]) / min(60, len(sample))
    return ThemePattern(
        peak_return=(peak_close / start_close - 1.0) * 100.0,
        drawdown=(current_close / peak_close - 1.0) * 100.0,
        consolidation_range=consolidation_range,
        down_volume_ratio=down_turnover_ratio,
        trend_breadth=1.0 if current_close >= sma60 else 0.0,
        turnover_spike_ratio=spike_ratio,
        days_since_turnover_spike=len(sample) - 1 - spike_index,
        turnover_cooldown_ratio=cooldown_ratio,
    )


def _pattern_is_eligible(pattern: ThemePattern) -> bool:
    return (
        pattern.peak_return >= 20.0
        and -30.0 <= pattern.drawdown <= -5.0
        and pattern.consolidation_range <= 20.0
        and pattern.trend_breadth >= 0.5
    )


def _history_pattern_is_eligible(pattern: ThemePattern) -> bool:
    return (
        _pattern_is_eligible(pattern)
        and pattern.turnover_spike_ratio >= 1.8
        and 10 <= pattern.days_since_turnover_spike <= 45
        and pattern.turnover_cooldown_ratio <= 0.8
    )


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
        market_data: KisMarketDataProvider | None = None,
        *,
        theme_limit: int,
        theme_candidate_pool: int,
        stocks_per_theme: int,
        news_per_stock: int,
        max_events_per_stock: int,
        news_lookback_hours: int,
        theme_scan_limit: int = 100,
        theme_screen_stocks: int = 3,
        history_lookback_days: int = 100,
        history_min_bars: int = 50,
    ) -> None:
        self.themes = themes
        self.news = news
        self.dart = dart
        self.event_model = event_model
        self.market_data = market_data
        self.theme_limit = theme_limit
        self.theme_candidate_pool = max(theme_limit, theme_candidate_pool)
        self.theme_scan_limit = max(self.theme_candidate_pool, theme_scan_limit)
        self.theme_screen_stocks = theme_screen_stocks
        self.stocks_per_theme = stocks_per_theme
        self.news_per_stock = news_per_stock
        self.max_events_per_stock = max_events_per_stock
        self.news_lookback_hours = news_lookback_hours
        self.history_lookback_days = history_lookback_days
        self.history_min_bars = history_min_bars

    def _score_patterns(
        self,
        raw_patterns: list[tuple[Theme, ThemePattern]],
        *,
        require_turnover_spike: bool,
    ) -> list[tuple[Theme, ThemePattern]]:
        predicate = _history_pattern_is_eligible if require_turnover_spike else _pattern_is_eligible
        eligible = [(theme, pattern) for theme, pattern in raw_patterns if predicate(pattern)]
        momentum_values = [pattern.peak_return for _, pattern in eligible]
        scored: list[tuple[Theme, ThemePattern]] = []
        for theme, pattern in eligible:
            if require_turnover_spike:
                score = (
                    0.20 * _relative(pattern.peak_return, momentum_values)
                    + 0.20 * _drawdown_score(pattern.drawdown)
                    + 0.15 * _inverse_scale(pattern.consolidation_range, 5.0, 20.0)
                    + 0.10 * _inverse_scale(pattern.down_volume_ratio, 0.5, 1.2)
                    + 0.10 * pattern.trend_breadth * 100.0
                    + 0.15 * _scale(pattern.turnover_spike_ratio, 1.8, 5.0)
                    + 0.10 * _inverse_scale(pattern.turnover_cooldown_ratio, 0.2, 0.8)
                )
            else:
                score = (
                    0.25 * _relative(pattern.peak_return, momentum_values)
                    + 0.25 * _drawdown_score(pattern.drawdown)
                    + 0.20 * _inverse_scale(pattern.consolidation_range, 5.0, 20.0)
                    + 0.20 * _inverse_scale(pattern.down_volume_ratio, 0.5, 1.2)
                    + 0.10 * pattern.trend_breadth * 100.0
                )
            scored.append((theme, replace(pattern, score=_clamp(score))))
        scored.sort(key=lambda item: item[1].score, reverse=True)
        return scored[: self.theme_candidate_pool]

    def _screen_candidates(self, now: datetime, warnings: list[str]) -> list[tuple[Theme, ThemePattern]]:
        if self.market_data is None or not self.market_data.enabled:
            raise ValueError("3-month theme screening requires KIS_APP_KEY and KIS_APP_SECRET")
        end = now.date() - timedelta(days=1)
        start = end - timedelta(days=self.history_lookback_days)
        raw_patterns: list[tuple[Theme, ThemePattern]] = []
        successful_histories = 0
        screening_themes = self.themes.screening_themes(self.theme_scan_limit, self.theme_screen_stocks)
        if not screening_themes:
            raise RuntimeError("Naver did not return theme constituents for screening")
        for theme in screening_themes:
            stock_patterns: list[_StockPattern] = []
            for stock in theme.stocks:
                try:
                    bars = self.market_data.history(stock, start, end)
                    pattern = _stock_pattern(bars, self.history_min_bars)
                    if pattern is not None:
                        successful_histories += 1
                        stock_patterns.append(pattern)
                except Exception as exc:
                    warnings.append(f"{theme.name}/{stock.name} 일봉 {type(exc).__name__}: {exc}")
            if stock_patterns:
                raw_patterns.append((theme, _theme_pattern(stock_patterns)))

        if successful_histories == 0:
            detail = f" First error: {warnings[0]}" if warnings else ""
            raise RuntimeError(f"KIS did not return enough daily history for any theme.{detail}")
        return self._score_patterns(raw_patterns, require_turnover_spike=False)

    def _bootstrap_history(
        self,
        now: datetime,
        warnings: list[str],
        *,
        include_today: bool = False,
    ) -> ThemeHistoryStore:
        if self.market_data is None or not self.market_data.enabled:
            raise ValueError("theme history bootstrap requires KIS_APP_KEY and KIS_APP_SECRET")
        end = now.date() if include_today else now.date() - timedelta(days=1)
        start = end - timedelta(days=self.history_lookback_days)
        themes = self.themes.screening_themes(self.theme_scan_limit, self.theme_screen_stocks)
        if not themes:
            raise RuntimeError("Naver did not return theme constituents for history bootstrap")
        store = ThemeHistoryStore()
        successful_histories = 0
        for theme in themes:
            histories: list[tuple[Stock, tuple[DailyBar, ...]]] = []
            for stock in theme.stocks:
                try:
                    bars = self.market_data.history(stock, start, end)
                    if len(bars) >= self.history_min_bars:
                        successful_histories += 1
                        histories.append((stock, bars))
                except Exception as exc:
                    warnings.append(f"{theme.name}/{stock.name} 일봉 {type(exc).__name__}: {exc}")
            store.add_bootstrap(theme, histories)
        if successful_histories == 0 or not store.ready(self.history_min_bars):
            detail = f" First error: {warnings[0]}" if warnings else ""
            raise RuntimeError(f"KIS did not return enough history to bootstrap themes.{detail}")
        return store

    def _screen_history(
        self, store: ThemeHistoryStore, now: datetime
    ) -> list[tuple[Theme, ThemePattern]]:
        raw_patterns: list[tuple[Theme, ThemePattern]] = []
        for item in store.series.values():
            if not item.points or (now.date() - item.points[-1].date).days > 10:
                continue
            pattern = _history_pattern(item.points, self.history_min_bars)
            if pattern is not None:
                raw_patterns.append((item.theme, pattern))
        return self._score_patterns(raw_patterns, require_turnover_spike=True)

    def prepare_candidates(
        self,
        now: datetime,
        store: ThemeHistoryStore | None,
    ) -> tuple[list[tuple[Theme, ThemePattern]], tuple[str, ...], ThemeHistoryStore]:
        warnings: list[str] = []
        if (
            store is None
            or not store.ready(self.history_min_bars)
            or not store.fresh(now.date())
        ):
            store = self._bootstrap_history(now, warnings)
        return self._screen_history(store, now), tuple(warnings), store

    def collect_history(
        self,
        now: datetime,
        store: ThemeHistoryStore | None,
    ) -> tuple[ThemeHistoryStore, tuple[str, ...], str]:
        warnings: list[str] = []
        if (
            store is None
            or not store.ready(self.history_min_bars)
            or not store.fresh(now.date())
        ):
            return (
                self._bootstrap_history(now, warnings, include_today=True),
                tuple(warnings),
                "bootstrapped",
            )
        themes = self.themes.list_themes(self.theme_scan_limit)
        if not themes:
            raise RuntimeError("Naver did not return a theme snapshot")
        if store.snapshot_unchanged(themes):
            warnings.append("테마 스냅샷이 이전 거래일과 같아 휴장일로 간주하고 적재를 건너뜀")
            return store, tuple(warnings), "unchanged"
        store.append_snapshot(themes, now.date())
        return store, tuple(warnings), "appended"

    def screen_candidates(
        self, now: datetime
    ) -> tuple[list[tuple[Theme, ThemePattern]], tuple[str, ...]]:
        warnings: list[str] = []
        return self._screen_candidates(now, warnings), tuple(warnings)

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
                if self.market_data is None:
                    raise RuntimeError("KIS provider is not configured")
                preopen_quote = self.market_data.quote(stock)
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

    def run(
        self,
        mode: str,
        now: datetime | None = None,
        *,
        screened_candidates: list[tuple[Theme, ThemePattern]] | None = None,
        screening_warnings: tuple[str, ...] = (),
    ) -> DailyReport:
        if mode not in {"premarket", "preopen", "confirmation"}:
            raise ValueError("mode must be premarket, preopen, or confirmation")
        now = now or datetime.now(ZoneInfo("Asia/Seoul"))
        warnings = list(screening_warnings)
        screened = screened_candidates
        if screened is None:
            screened = self._screen_candidates(now, warnings)
        elif mode == "confirmation":
            try:
                refreshed = self.themes.refresh_themes(
                    [theme for theme, _ in screened], self.theme_scan_limit
                )
                screened = [
                    (refreshed_theme, pattern)
                    for refreshed_theme, (_, pattern) in zip(refreshed, screened)
                ]
            except Exception as exc:
                warnings.append(f"테마 당일지표 {type(exc).__name__}: {exc}")
        raw_analyses: list[ThemeAnalysis] = []
        for screening_theme, pattern in screened:
            try:
                theme = self.themes.hydrate_theme(screening_theme, self.stocks_per_theme)
            except Exception as exc:
                warnings.append(f"{screening_theme.name} 구성종목 {type(exc).__name__}: {exc}")
                continue
            stocks = tuple(
                self._analyze_stock(stock, now, warnings, include_preopen=mode == "preopen")
                for stock in theme.stocks
            )
            raw_analyses.append(ThemeAnalysis(theme=theme, stocks=stocks, pattern=pattern))

        if mode == "preopen" and raw_analyses and not any(
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
            technical_score = analysis.pattern.score if analysis.pattern else 0.0
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
                theme_score = 0.85 * technical_score + 0.15 * news_score
            elif mode == "confirmation":
                theme_score = (
                    0.30 * technical_score
                    + 0.20 * breadth_score
                    + 0.15 * change_score
                    + 0.15 * trading_score
                    + 0.10 * leader_score
                    + 0.10 * news_score
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
                    0.30 * technical_score
                    + 0.20 * expected_breadth
                    + 0.15 * expected_rate_score
                    + 0.10 * _relative(preopen_theme_values[theme_index], preopen_theme_values)
                    + 0.10 * orderbook_score
                    + 0.10 * news_score
                    + 0.05 * prior_theme_score
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
            scored_themes.append(
                ThemeAnalysis(
                    theme=theme,
                    stocks=tuple(scored_stocks),
                    score=_clamp(theme_score),
                    pattern=analysis.pattern,
                )
            )

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
_INVESTMENT_DISCLAIMER = (
    "※ 본 알림은 정보 제공용 참고자료이며 투자 권유나 매매 신호가 아닙니다. "
    "모든 투자 판단과 손익의 책임은 이용자 본인에게 있으며 서비스 제공자는 투자 결과에 책임지지 않습니다."
)


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
        lines = [f"🌅 {report.generated_at:%Y-%m-%d} 07:30 3개월 조정 후보", ""]
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
        if analysis.pattern:
            pattern = analysis.pattern
            lines.append(
                f"   3개월 고점상승 {pattern.peak_return:+.1f}% · 고점대비 {pattern.drawdown:+.1f}%"
            )
            lines.append(
                f"   15일 변동폭 {pattern.consolidation_range:.1f}% · 하락거래대금비 {pattern.down_volume_ratio:.2f}"
                f" · 60일선 상회 {pattern.trend_breadth:.0%}"
            )
            if pattern.turnover_spike_ratio > 1.0:
                lines.append(
                    f"   거래대금 최대 {pattern.turnover_spike_ratio:.1f}배 · "
                    f"폭발 후 {pattern.days_since_turnover_spike}거래일 · "
                    f"현재/고점 {pattern.turnover_cooldown_ratio:.2f}배"
                )
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
        lines.append("※ 3개월 가격·거래량 패턴과 뉴스·공시를 결합한 후보이며 09:10 최종확인 전에는 거래 신호가 아닙니다.")
    elif report.mode == "preopen":
        lines.append("※ 동시호가 예상체결 데이터는 09:00 전 바뀔 수 있으며 09:10 최종 확인 전 중간 신호입니다.")
    else:
        lines.append("※ 뉴스·공시·초기 수급을 결합한 참고 지표입니다.")
    lines.extend(["", _INVESTMENT_DISCLAIMER])
    return "\n".join(lines).strip()
