from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from .models import (
    DailyBar,
    DailyReport,
    EmergingThemeAnalysis,
    EventSummary,
    MarketEvent,
    Stock,
    StockAnalysis,
    Theme,
    ThemeAnalysis,
    ThemePattern,
)
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


_TOPIC_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")
_TOPIC_STOPWORDS = {
    "관련주", "거래", "급등", "기대", "뉴스", "다시", "단독", "대비", "랠리", "매수",
    "매일경제", "머니투데이", "부각", "상승", "상한가", "서울경제", "소식", "수혜", "시장",
    "아시아경제", "오늘", "이데일리", "이슈", "장중", "재개", "전망", "정부", "정책", "종목",
    "주가", "주식", "증시", "코스닥", "코스피", "특징주", "투자", "파이낸셜뉴스", "한국경제",
    "확대", "뉴스핌",
}
_BROAD_TOPIC_WORDS = {"건설", "공장", "반도체", "바이오", "전력", "클러스터"}
_TOPIC_ALIASES = {"호남권": "호남", "수도권": "수도권", "충청권": "충청"}


def _topic_tokens(events: list[MarketEvent], stock: Stock) -> Counter[str]:
    excluded = {stock.name.lower(), re.sub(r"(?:\d*우B?|우선주)$", "", stock.name, flags=re.IGNORECASE).lower()}
    tokens: Counter[str] = Counter()
    for event in events:
        for raw in _TOPIC_WORD_RE.findall(event.title):
            value = _TOPIC_ALIASES.get(raw.lower(), raw.lower())
            if value in _TOPIC_STOPWORDS or value in excluded or len(value) < 2:
                continue
            tokens[value] += 1
    return tokens


def _event_theme_clusters(
    rows: list[tuple[Stock, list[MarketEvent]]], minimum_stocks: int = 3
) -> list[tuple[list[int], tuple[str, ...]]]:
    """Cluster moving stocks when their recent headlines share a concrete catalyst."""
    topics = [_topic_tokens(events, stock) for stock, events in rows]
    company_names = {
        value.lower()
        for stock, _ in rows
        for value in (
            stock.name,
            re.sub(r"(?:\d*우B?|우선주)$", "", stock.name, flags=re.IGNORECASE),
        )
        if value
    }
    for row in topics:
        for company_name in company_names:
            row.pop(company_name, None)
    document_frequency = Counter(token for row in topics for token in row)
    parents = list(range(len(rows)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left in range(len(rows)):
        for right in range(left + 1, len(rows)):
            shared = set(topics[left]) & set(topics[right])
            concrete = {token for token in shared if token not in _BROAD_TOPIC_WORDS}
            if len(shared) >= 2 or any(document_frequency[token] >= minimum_stocks for token in concrete):
                union(left, right)

    components: dict[int, list[int]] = {}
    for index in range(len(rows)):
        components.setdefault(find(index), []).append(index)

    result: list[tuple[list[int], tuple[str, ...]]] = []
    for indices in components.values():
        if len(indices) < minimum_stocks:
            continue
        frequency = Counter()
        for index in indices:
            frequency.update(topics[index])
        shared_tokens = [
            token for token, count in frequency.items()
            if sum(token in topics[index] for index in indices) >= 2
        ]
        shared_tokens.sort(
            key=lambda token: (
                token not in _BROAD_TOPIC_WORDS,
                document_frequency[token],
                frequency[token],
                len(token),
            ),
            reverse=True,
        )
        if not shared_tokens:
            continue
        result.append((indices, tuple(shared_tokens[:3])))
    return result


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

    def _discover_emerging_themes(
        self,
        now: datetime,
        tracked: list[ThemeAnalysis],
        warnings: list[str],
        *,
        stock_limit: int = 30,
        theme_limit: int = 3,
    ) -> tuple[EmergingThemeAnalysis, ...]:
        """Discover event-driven themes without changing the fixed morning selection."""
        market_movers = getattr(self.themes, "market_movers", None)
        if not callable(market_movers) or self.news is None:
            return ()
        try:
            raw_movers = market_movers(stock_limit)
        except Exception as exc:
            warnings.append(f"신규테마 시장상위 {type(exc).__name__}: {exc}")
            return ()
        if not isinstance(raw_movers, (list, tuple)):
            return ()
        movers = [
            stock for stock in raw_movers
            if (stock.change_rate or 0.0) >= 5.0 and (stock.trade_amount or 0.0) >= 300_000_000
        ][:stock_limit]
        rows: list[tuple[Stock, list[MarketEvent]]] = []
        news_failures = 0
        for stock in movers:
            try:
                events = deduplicate_events(
                    self.news.recent(
                        stock,
                        min(self.news_per_stock, 5),
                        min(self.news_lookback_hours, 24),
                        now,
                    ),
                    min(self.max_events_per_stock, 5),
                )
            except Exception:
                news_failures += 1
                continue
            if events:
                rows.append((stock, events))
        if news_failures:
            warnings.append(f"신규테마 뉴스 조회 실패: {news_failures}건")

        tracked_codes = {
            result.stock.code for analysis in tracked for result in analysis.stocks
        }
        tracked_names = [analysis.theme.name.lower() for analysis in tracked]
        discovered: list[EmergingThemeAnalysis] = []
        for indices, keywords in _event_theme_clusters(rows):
            cluster_rows = [rows[index] for index in indices]
            cluster_codes = {stock.code for stock, _ in cluster_rows}
            if len(cluster_codes & tracked_codes) >= 2:
                continue
            if any(sum(keyword in name for keyword in keywords) >= 2 for name in tracked_names):
                continue

            cluster_rows.sort(
                key=lambda row: (row[0].change_rate or 0.0, row[0].trade_amount or 0.0),
                reverse=True,
            )
            stock_analyses: list[StockAnalysis] = []
            all_events: list[MarketEvent] = []
            for stock, events in cluster_rows[:5]:
                summary = None
                if self.event_model is not None:
                    try:
                        summary = self.event_model.analyze(stock, events, now)
                    except Exception as exc:
                        warnings.append(f"{stock.name} 신규테마 JEV {type(exc).__name__}: {exc}")
                price_score = _scale(stock.change_rate, 5.0, 30.0, default=0.0)
                volume_score = _scale(stock.relative_volume, 1.5, 10.0, default=0.0)
                amount_score = _scale(
                    math.log10(max(stock.trade_amount or 1.0, 1.0)), 8.5, 11.0, default=0.0
                )
                event_score = _event_score(summary)
                score = (
                    0.45 * price_score
                    + 0.25 * volume_score
                    + 0.15 * amount_score
                    + 0.15 * event_score
                )
                signal = "신규 주도" if score >= 75 else "신규 확산" if score >= 55 else "신규 관찰"
                stock_analyses.append(
                    StockAnalysis(stock, summary, tuple(events), _clamp(score), signal)
                )
                all_events.extend(events)

            if len(stock_analyses) < 3:
                continue
            stock_analyses.sort(key=lambda item: item.score, reverse=True)
            changes = [item.stock.change_rate or 0.0 for item in stock_analyses]
            total_trading_value = sum(item.stock.trade_amount or 0.0 for item in stock_analyses)
            breadth_score = min(100.0, 40.0 + len(cluster_rows) * 10.0)
            strength_score = sum(_scale(value, 5.0, 30.0) for value in changes) / len(changes)
            trading_score = _scale(math.log10(max(total_trading_value, 1.0)), 9.0, 11.5)
            event_score = sum(_event_score(item.event_summary) for item in stock_analyses) / len(stock_analyses)
            latest_event = max(all_events, key=lambda event: event.published_at)
            age_hours = max(
                0.0,
                (now.astimezone(ZoneInfo("UTC")) - latest_event.published_at.astimezone(ZoneInfo("UTC"))).total_seconds() / 3600,
            )
            freshness_score = math.exp(-age_hours / 24.0) * 100.0
            score = _clamp(
                0.30 * breadth_score
                + 0.25 * strength_score
                + 0.20 * trading_score
                + 0.15 * event_score
                + 0.10 * freshness_score
            )
            if score < 55.0:
                continue
            catalyst = max(
                all_events,
                key=lambda event: (
                    sum(keyword in event.title.lower() for keyword in keywords),
                    event.published_at,
                ),
            )
            identity = hashlib.sha1(
                f"{now.date().isoformat()}|{'|'.join(keywords)}".encode("utf-8")
            ).hexdigest()[:10]
            discovered.append(
                EmergingThemeAnalysis(
                    id=f"emerging:{now.date().isoformat()}:{identity}",
                    name=" · ".join(keywords),
                    stocks=tuple(stock_analyses),
                    score=score,
                    change_rate=median(changes),
                    trading_value=total_trading_value,
                    keywords=keywords,
                    catalyst=catalyst,
                )
            )
        discovered.sort(key=lambda item: item.score, reverse=True)
        return tuple(discovered[:theme_limit])

    def run(
        self,
        mode: str,
        now: datetime | None = None,
        *,
        screened_candidates: list[tuple[Theme, ThemePattern]] | None = None,
        screening_warnings: tuple[str, ...] = (),
        fixed_selection: bool = False,
    ) -> DailyReport:
        if mode not in {"premarket", "preopen", "confirmation"}:
            raise ValueError("mode must be premarket, preopen, or confirmation")
        now = now or datetime.now(ZoneInfo("Asia/Seoul"))
        warnings = list(screening_warnings)
        screened = screened_candidates
        if mode != "premarket":
            if screened is None:
                raise ValueError("당일 장전 후보가 필요합니다. premarket 작업을 먼저 실행하세요.")
            fixed_selection = True
        if fixed_selection and screened is None:
            raise ValueError("fixed selection requires saved morning candidates")
        refresh_failed: set[str] = set()
        if screened is None:
            screened = self._screen_candidates(now, warnings)
        elif mode == "confirmation" and screened:
            try:
                refreshed = self.themes.refresh_themes(
                    [theme for theme, _ in screened], self.theme_scan_limit
                )
                by_id = {theme.id: theme for theme in refreshed}
                refresh_failed = {theme.id for theme, _ in screened if theme.id not in by_id}
                screened = [(by_id.get(theme.id, theme), pattern) for theme, pattern in screened]
                if refresh_failed:
                    warnings.append(f"테마 당일지표 누락: {len(refresh_failed)}건")
            except Exception as exc:
                warnings.append(f"테마 당일지표 {type(exc).__name__}: {exc}")
                refresh_failed = {theme.id for theme, _ in screened}
        raw_analyses: list[ThemeAnalysis] = []
        for screening_theme, pattern in screened:
            summary_unavailable = mode == "confirmation" and (
                screening_theme.id in refresh_failed
                or any(value is None for value in (
                    screening_theme.change_rate, screening_theme.breadth, screening_theme.trading_value
                ))
            )
            if screening_theme.id in refresh_failed:
                # The ranking endpoint is not a complete theme directory. Keep
                # the fixed ID, discard stale aggregates, and fetch its stocks.
                screening_theme = Theme(screening_theme.id, screening_theme.name)
            try:
                theme = self.themes.hydrate_theme(screening_theme, self.stocks_per_theme)
                if not theme.stocks:
                    raise RuntimeError("theme constituents unavailable")
            except Exception as exc:
                warnings.append(f"{screening_theme.name} 구성종목 {type(exc).__name__}: {exc}")
                if fixed_selection:
                    raw_analyses.append(ThemeAnalysis(
                        theme=Theme(screening_theme.id, screening_theme.name),
                        pattern=pattern, data_error=True,
                    ))
                continue
            stocks = tuple(
                self._analyze_stock(stock, now, warnings, include_preopen=mode == "preopen")
                for stock in theme.stocks
            )
            if mode == "preopen" and not any(stock.preopen_quote is not None for stock in stocks):
                warnings.append(f"{theme.name}: 예상체결 데이터 확인 실패")
                raw_analyses.append(ThemeAnalysis(
                    theme=Theme(screening_theme.id, screening_theme.name),
                    pattern=pattern, data_error=True,
                ))
                continue
            raw_analyses.append(ThemeAnalysis(
                theme=theme, stocks=stocks, pattern=pattern, summary_unavailable=summary_unavailable,
            ))

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
            if analysis.data_error:
                scored_themes.append(analysis)
                continue
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
                    stock_score = 0.30 * amount + 0.20 * price + 0.20 * volume + 0.15 * event
                    # Normalize the available stock components instead of
                    # awarding points for unknown theme aggregates.
                    score = stock_score / 0.85 if analysis.summary_unavailable else stock_score + 0.15 * theme_score
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
                    score=None if analysis.summary_unavailable else _clamp(theme_score),
                    pattern=analysis.pattern,
                    summary_unavailable=analysis.summary_unavailable,
                )
            )

        if not fixed_selection:
            scored_themes.sort(key=lambda item: item.score if item.score is not None else -1.0, reverse=True)
            scored_themes = scored_themes[: self.theme_limit]
        emerging_themes = (
            self._discover_emerging_themes(now, scored_themes, warnings)
            if mode == "confirmation"
            else ()
        )
        return DailyReport(
            now,
            mode,
            tuple(scored_themes),
            tuple(warnings),
            emerging_themes,
        )


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
    else:
        lines.extend(["당일 추적 테마 · 장전 선정 순서", ""])
    for theme_rank, analysis in enumerate(report.themes, 1):
        theme = analysis.theme
        if analysis.data_error:
            lines.extend([f"{theme_rank}. {theme.name} · 데이터 확인 실패", "   당일 추적 테마 유지 · 이번 단계 평가 불가", ""])
            continue
        rate = f"{theme.change_rate:+.2f}%" if theme.change_rate is not None else "-"
        breadth = f"{theme.breadth:.0%}" if theme.breadth is not None else "-"
        strength = "종합점수 미제공" if analysis.score is None else f"강도 {analysis.score:.0f}/100"
        lines.append(f"{theme_rank}. {theme.name} · {strength}")
        if analysis.summary_unavailable:
            lines.append("   테마 전체 지표 미확보 · 종목 점수는 테마 가중치를 제외한 참고 평가")
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
    if report.emerging_themes:
        lines.extend(["⚡ 오늘의 신규·재점화 테마", ""])
        for theme_rank, analysis in enumerate(report.emerging_themes, 1):
            rate = f"{analysis.change_rate:+.2f}%" if analysis.change_rate is not None else "-"
            lines.append(
                f"{theme_rank}. {analysis.name} · 신규강도 {analysis.score:.0f}/100"
            )
            lines.append(
                f"   동반종목 {len(analysis.stocks)}개 · 중앙등락 {rate}"
                f" · 거래대금 {_format_amount(analysis.trading_value)}"
            )
            if analysis.catalyst:
                lines.append(f"   공통 재료: {analysis.catalyst.title[:72]}")
            for stock_rank, result in enumerate(analysis.stocks, 1):
                stock = result.stock
                stock_rate = f"{stock.change_rate:+.2f}%" if stock.change_rate is not None else "-"
                lines.append(
                    f"   {stock_rank}) {stock.name}({stock.code}) · {result.signal} {result.score:.0f}"
                    f" · {stock_rate} · {_format_amount(stock.trade_amount)}"
                )
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
