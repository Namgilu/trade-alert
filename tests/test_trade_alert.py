from __future__ import annotations

import io
import base64
import json
import os
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from trade_alert.config import Settings
from trade_alert.candidate_cache import load_candidates, save_candidates
from trade_alert.github_repository import GitHubRepository
from trade_alert.models import (
    DailyReport,
    DailyBar,
    EventSummary,
    MarketEvent,
    PreopenQuote,
    Stock,
    StockAnalysis,
    Theme,
    ThemeAnalysis,
    ThemePattern,
)
from trade_alert.providers import KisMarketDataProvider, JevEventProvider, NaverThemeProvider, OpenDartProvider, deduplicate_events
from trade_alert.report_store import save_report
from trade_alert.scheduler import WORKFLOW_SCHEDULES
from trade_alert.service import MarketAlertService, format_report
from trade_alert.theme_history import ThemeDailyPoint, ThemeHistoryStore, ThemeSeries


class FakeHttp:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


class FakeDartHttp:
    def __init__(self, archive: bytes, disclosure: dict):
        self.archive = archive
        self.disclosure = disclosure

    def bytes(self, url, **kwargs):
        return self.archive

    def json(self, url, **kwargs):
        return self.disclosure


def consolidation_bars() -> tuple[DailyBar, ...]:
    bars = []
    start = date(2026, 5, 1)
    for index in range(65):
        if index < 15:
            close = 100.0 + 100.0 * index / 14.0
        elif index < 30:
            close = 200.0 - 20.0 * (index - 14) / 15.0
        else:
            close = 181.0
        volume = 400_000.0 if index >= 55 else 1_000_000.0
        bars.append(DailyBar(start + timedelta(days=index), close, close * 1.01, close * 0.99, close, volume))
    return tuple(bars)


class ThemeProviderTest(unittest.TestCase):
    def test_parses_live_contract_and_selects_top_items(self):
        http = FakeHttp(
            [
                [
                    {"no": "20", "name": "B", "changeRate": "1.2", "riseCnt": "3", "totalCnt": "6", "totalAccAmount": "100"},
                    {"no": "10", "name": "A", "changeRate": "3.5", "riseCnt": "8", "totalCnt": "10", "totalAccAmount": "200"},
                ],
                [
                    {"itemcode": "2", "itemname": "둘", "prevChangeRate": "2", "tradeAmount": "1000", "tradeVolume": "50", "prevQuant": "100"},
                    {"itemcode": "1", "itemname": "하나", "prevChangeRate": "5", "tradeAmount": "2000", "tradeVolume": "80", "prevQuant": "100"},
                ],
                [{"itemcode": "3", "itemname": "셋", "prevChangeRate": "1"}],
            ]
        )
        result = NaverThemeProvider(http, "themes", "theme/{theme_id}").top_themes(2, 1)
        self.assertEqual([theme.name for theme in result], ["A", "B"])
        self.assertEqual(result[0].stocks[0].name, "하나")
        self.assertEqual(result[0].breadth, 0.8)
        self.assertEqual(result[0].stocks[0].relative_volume, 0.8)

    def test_screening_universe_uses_market_cap_leaders(self):
        http = FakeHttp(
            [
                [{"no": "10", "name": "반도체"}],
                [
                    {"itemcode": "005930", "itemname": "삼성전자", "marketSum": "500"},
                    {"itemcode": "000660", "itemname": "SK하이닉스", "marketSum": "400"},
                ],
            ]
        )
        result = NaverThemeProvider(http, "https://themes?startIdx=0&pageSize=20", "theme/{theme_id}").screening_themes(100, 2)
        self.assertEqual([stock.code for stock in result[0].stocks], ["005930", "000660"])
        self.assertIn("pageSize=100", http.calls[0][0])
        self.assertIn("orderType=marketSum", http.calls[1][0])
        self.assertEqual(len(http.calls), 2)


class EventProviderTest(unittest.TestCase):
    def test_scores_structured_krx_event(self):
        now = datetime(2026, 10, 7, 7, 30, tzinfo=ZoneInfo("Asia/Seoul"))
        event = MarketEvent("대규모 공급계약", "매출액 대비 30%", "https://example.com", now - timedelta(hours=1))
        response = {
            "answers": {
                "relevance": {"noul": 0.95},
                "primary_event": {"choice": "event_0"},
                "event_type": {"choice": "contract"},
                "impact": {
                    "choice": "strong_positive",
                    "probabilities": {"strong_positive": 0.8, "positive": 0.2},
                    "confidence": 0.9,
                },
                "horizon": {"choice": "open"},
                "confirmed": {"noul": 0.9},
            }
        }
        http = FakeHttp([response])
        result = JevEventProvider(http, "https://jev", "secret", "jev-latest").analyze(
            Stock("005930", "삼성전자"), [event], now
        )
        self.assertIsNotNone(result)
        self.assertGreater(result.score, 60)
        self.assertEqual(result.event_type, "contract")
        self.assertEqual(result.primary_event.title, "대규모 공급계약")
        self.assertEqual(len(http.calls[0][1]["body"]["questions"]), 6)


class DeduplicationTest(unittest.TestCase):
    def test_prefers_disclosure_over_duplicate_news(self):
        now = datetime.now(ZoneInfo("Asia/Seoul"))
        news = MarketEvent("A사 공급계약 체결", "", "news", now, "news")
        disclosure = MarketEvent("A사, 공급계약 체결", "", "dart", now - timedelta(minutes=5), "disclosure")
        result = deduplicate_events([news, disclosure], 5)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].source_kind, "disclosure")


class OpenDartProviderTest(unittest.TestCase):
    def test_maps_stock_code_and_returns_disclosure(self):
        xml = b"<result><list><corp_code>00126380</corp_code><stock_code>005930</stock_code></list></result>"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("CORPCODE.xml", xml)
        disclosure = {
            "status": "000",
            "list": [{"rcept_no": "202610070001", "report_nm": "공급계약", "rcept_dt": "20261007", "flr_nm": "삼성전자"}],
        }
        provider = OpenDartProvider(FakeDartHttp(buffer.getvalue(), disclosure), "key")
        now = datetime(2026, 10, 7, 7, 30, tzinfo=ZoneInfo("Asia/Seoul"))
        result = provider.recent(Stock("005930", "삼성전자"), 24, now)
        self.assertEqual(result[0].source_kind, "disclosure")
        self.assertIn("202610070001", result[0].url)


class KisMarketDataProviderTest(unittest.TestCase):
    def test_gets_token_once_and_parses_expected_execution(self):
        http = FakeHttp(
            [
                {"access_token": "token"},
                {
                    "rt_cd": "0",
                    "output1": {"total_askp_rsqn": "1000", "total_bidp_rsqn": "2500"},
                    "output2": {
                        "antc_cnpr": "72100",
                        "antc_cntg_prdy_ctrt": "3.15",
                        "antc_vol": "123456",
                    },
                },
            ]
        )
        provider = KisMarketDataProvider(http, "app-key", "app-secret", "https://kis.example")
        quote = provider.quote(Stock("005930", "삼성전자"))
        self.assertEqual(quote.expected_price, 72100)
        self.assertEqual(quote.expected_volume, 123456)
        self.assertEqual(quote.bid_ask_ratio, 2.5)
        self.assertIn("FID_INPUT_ISCD=005930", http.calls[1][0])
        self.assertEqual(http.calls[1][1]["headers"]["tr_id"], "FHKST01010200")
        self.assertEqual(http.calls[1][1]["headers"]["authorization"], "Bearer token")

    def test_parses_adjusted_daily_history(self):
        http = FakeHttp(
            [
                {"access_token": "token"},
                {
                    "rt_cd": "0",
                    "output2": [
                        {
                            "stck_bsop_date": "20261006",
                            "stck_oprc": "70000",
                            "stck_hgpr": "72000",
                            "stck_lwpr": "69000",
                            "stck_clpr": "71000",
                            "acml_vol": "123456",
                        }
                    ],
                },
            ]
        )
        provider = KisMarketDataProvider(http, "app-key", "app-secret", "https://kis.example")
        bars = provider.history(Stock("005930", "삼성전자"), date(2026, 5, 1), date(2026, 10, 6))
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].close, 71000)
        self.assertIn("FID_ORG_ADJ_PRC=0", http.calls[1][0])
        self.assertEqual(http.calls[1][1]["headers"]["tr_id"], "FHKST03010100")
        cached = provider.history(Stock("005930", "삼성전자"), date(2026, 5, 1), date(2026, 10, 6))
        self.assertIs(cached, bars)
        self.assertEqual(len(http.calls), 2)


class CandidateCacheTest(unittest.TestCase):
    def test_round_trips_candidates_and_rejects_another_market_date(self):
        market_date = date(2026, 10, 7)
        theme = Theme("10", "반도체", 2.5, 0.75, 1_000, (Stock("005930", "삼성전자"),))
        pattern = ThemePattern(65, -12, 8, 0.55, 0.75, 84, 3.2, 20, 0.4)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            save_candidates(path, market_date, [(theme, pattern)])
            loaded = load_candidates(path, market_date)
            self.assertEqual(loaded, [(theme, pattern)])
            with self.assertRaisesRegex(ValueError, "today's Korean market date"):
                load_candidates(path, market_date + timedelta(days=1))


class ThemeHistoryTest(unittest.TestCase):
    def test_daily_collection_uses_one_theme_list_call_and_no_kis(self):
        now = datetime(2026, 10, 7, 16, 10, tzinfo=ZoneInfo("Asia/Seoul"))
        theme = Theme("10", "전력설비", 1.0, 0.7, 1_000)
        points = tuple(
            ThemeDailyPoint(
                now.date() - timedelta(days=65 - index), 100.0, 100.0, 100.0, 100.0
            )
            for index in range(65)
        )
        store = ThemeHistoryStore({theme.id: ThemeSeries(theme, points)})

        class Themes:
            calls = 0

            def list_themes(self, *_):
                self.calls += 1
                return [Theme("10", "전력설비", 2.0, 0.8, 2_000)]

        class NoMarketCalls:
            enabled = True

            def __getattr__(self, name):
                raise AssertionError(f"daily collection must not call KIS {name}")

        themes = Themes()
        service = MarketAlertService(
            themes, None, None, None, NoMarketCalls(), theme_limit=1, theme_candidate_pool=1,
            stocks_per_theme=1, news_per_stock=3, max_events_per_stock=3, news_lookback_hours=24,
        )
        collected, warnings, action = service.collect_history(now, store)
        self.assertEqual(action, "appended")
        self.assertEqual(warnings, ())
        self.assertEqual(themes.calls, 1)
        self.assertEqual(collected.series["10"].points[-1].turnover, 2_000)

    def test_persists_snapshot_and_screens_past_turnover_spike(self):
        now = datetime(2026, 10, 7, 7, 30, tzinfo=ZoneInfo("Asia/Seoul"))
        theme = Theme("10", "전력설비", 1.0, 0.7, 1_000, (Stock("000001", "대표주"),))
        points = []
        start = now.date() - timedelta(days=65)
        for index in range(65):
            if index < 15:
                close = 100.0 + 100.0 * index / 14.0
            elif index < 30:
                close = 200.0 - 19.0 * (index - 14) / 15.0
            else:
                close = 185.0
            turnover = 500.0 if index == 30 else (50.0 if index >= 60 else 100.0)
            points.append(ThemeDailyPoint(start + timedelta(days=index), close, close, close, turnover))
        store = ThemeHistoryStore({theme.id: ThemeSeries(theme, tuple(points))})

        class NoCalls:
            enabled = True

            def __getattr__(self, name):
                raise AssertionError(f"history-backed screening must not call {name}")

        service = MarketAlertService(
            NoCalls(), None, None, None, NoCalls(), theme_limit=1, theme_candidate_pool=1,
            stocks_per_theme=1, news_per_stock=3, max_events_per_stock=3, news_lookback_hours=24,
        )
        candidates, warnings, returned_store = service.prepare_candidates(now, store)
        self.assertIs(returned_store, store)
        self.assertEqual(warnings, ())
        self.assertEqual(candidates[0][0].name, "전력설비")
        self.assertEqual(candidates[0][1].turnover_spike_ratio, 5.0)
        self.assertEqual(candidates[0][1].days_since_turnover_spike, 34)
        self.assertEqual(candidates[0][1].turnover_cooldown_ratio, 0.1)

        store.append_snapshot([Theme("10", "전력설비", 2.0, 0.8, 2_000)], now.date())
        first_close = store.series["10"].points[-1].close
        store.append_snapshot([Theme("10", "전력설비", 2.0, 0.8, 2_000)], now.date())
        self.assertEqual(store.series["10"].points[-1].close, first_close)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            store.save(path)
            loaded = ThemeHistoryStore.load(path)
        self.assertEqual(loaded.series["10"].points[-1].turnover, 2_000)
        self.assertEqual(loaded.series["10"].theme.stocks[0].name, "대표주")

    def test_exports_repository_snapshots_and_only_versions_membership_changes(self):
        market_date = date(2026, 10, 7)
        theme = Theme(
            "10",
            "전력설비",
            2.0,
            0.8,
            2_000,
            (Stock("000001", "대표주"),),
        )
        store = ThemeHistoryStore(
            {
                theme.id: ThemeSeries(
                    theme,
                    (ThemeDailyPoint(market_date, 102.0, 102.0, 102.0, 2_000),),
                )
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = store.save_repository(root, market_date)
            second = store.save_repository(root, market_date)

            self.assertTrue((root / "state" / "theme-history.json").exists())
            self.assertTrue((root / "catalog" / "themes.json").exists())
            self.assertTrue((root / "memberships" / "2026-10-07.json").exists())
            daily_path = root / "daily" / "2026" / "10" / "2026-10-07.json"
            daily = json.loads(daily_path.read_text(encoding="utf-8"))
            self.assertEqual(daily["themes"][0]["turnover"], 2_000)
            self.assertEqual(len(first), 4)
            self.assertEqual(len(second), 3)


class ServiceScoringTest(unittest.TestCase):
    def test_preopen_reuses_screened_candidates_without_history_calls(self):
        now = datetime(2026, 10, 7, 8, 55, tzinfo=ZoneInfo("Asia/Seoul"))
        stock = Stock("005930", "삼성전자", 3.0)
        theme = Theme("1", "반도체", 2.0, 0.8, 1_000, (stock,))
        pattern = ThemePattern(65, -12, 8, 0.55, 0.75, 84)

        class Themes:
            def screening_themes(self, *_):
                raise AssertionError("cached run must not screen all themes")

            def hydrate_theme(self, value, *_):
                return value

        class News:
            def recent(self, *_):
                return []

        class Dart:
            enabled = False

        class Model:
            def analyze(self, *_):
                raise AssertionError("no events means no JEV call")

        class MarketData:
            enabled = True

            def history(self, *_):
                raise AssertionError("cached run must not request history")

            def quote(self, *_):
                return PreopenQuote(72_000, 2.0, 100_000, 100_000, 200_000)

        service = MarketAlertService(
            Themes(), News(), Dart(), Model(), MarketData(), theme_limit=1, theme_candidate_pool=1,
            stocks_per_theme=1, news_per_stock=3, max_events_per_stock=3, news_lookback_hours=24,
        )
        report = service.run("preopen", now, screened_candidates=[(theme, pattern)])
        self.assertEqual(report.themes[0].pattern, pattern)
        self.assertIsNotNone(report.themes[0].stocks[0].preopen_quote)

    def test_confirmation_requires_market_and_event_strength(self):
        now = datetime(2026, 10, 7, 9, 10, tzinfo=ZoneInfo("Asia/Seoul"))
        stock = Stock("005930", "삼성전자", 7.0, 50_000_000_000, 200_000, 1_000_000)
        theme = Theme("1", "반도체", 5.0, 1.0, 1000, (stock,))
        event = MarketEvent("공급계약", "", "url", now, "disclosure")
        summary = EventSummary(80, 1, 1, "contract", "open", 0.9, 1, event)

        class Themes:
            def screening_themes(self, *_):
                return [theme]

            def hydrate_theme(self, value, *_):
                return value

        class News:
            def recent(self, *_):
                return [event]

        class Dart:
            enabled = False

        class Model:
            def analyze(self, *_):
                return summary

        class MarketData:
            enabled = True

            def history(self, *_):
                return consolidation_bars()

        service = MarketAlertService(
            Themes(), News(), Dart(), Model(), MarketData(), theme_limit=1, theme_candidate_pool=1,
            stocks_per_theme=1, news_per_stock=3, max_events_per_stock=3, news_lookback_hours=24,
        )
        report = service.run("confirmation", now)
        self.assertEqual(report.themes[0].stocks[0].signal, "거래 확인")
        self.assertGreaterEqual(report.themes[0].stocks[0].score, 75)

    def test_preopen_combines_expected_execution_and_orderbook(self):
        now = datetime(2026, 10, 7, 8, 55, tzinfo=ZoneInfo("Asia/Seoul"))
        stock = Stock("005930", "삼성전자", 7.0, 50_000_000_000, 200_000, 1_000_000)
        theme = Theme("1", "반도체", 5.0, 1.0, 1000, (stock,))
        event = MarketEvent("공급계약", "", "url", now, "disclosure")
        summary = EventSummary(80, 1, 1, "contract", "open", 0.9, 1, event)

        class Themes:
            def screening_themes(self, *_):
                return [theme]

            def hydrate_theme(self, value, *_):
                return value

        class News:
            def recent(self, *_):
                return [event]

        class Dart:
            enabled = False

        class Model:
            def analyze(self, *_):
                return summary

        class MarketData:
            enabled = True

            def quote(self, *_):
                return PreopenQuote(80_000, 10.0, 300_000, 100_000, 900_000)

            def history(self, *_):
                return consolidation_bars()

        service = MarketAlertService(
            Themes(), News(), Dart(), Model(), MarketData(), theme_limit=1, theme_candidate_pool=1,
            stocks_per_theme=1, news_per_stock=3, max_events_per_stock=3, news_lookback_hours=24,
        )
        report = service.run("preopen", now)
        self.assertEqual(report.themes[0].stocks[0].signal, "장전 유효")
        self.assertGreaterEqual(report.themes[0].stocks[0].score, 72)

    def test_screen_excludes_theme_without_meaningful_drawdown(self):
        now = datetime(2026, 10, 7, 7, 30, tzinfo=ZoneInfo("Asia/Seoul"))
        stock = Stock("005930", "삼성전자")
        theme = Theme("1", "반도체", stocks=(stock,))

        class Themes:
            def screening_themes(self, *_):
                return [theme]

        class MarketData:
            enabled = True

            def history(self, *_):
                bars = list(consolidation_bars())
                latest = bars[-1]
                bars[-1] = DailyBar(latest.date, 200, 201, 199, 200, latest.volume)
                return tuple(bars)

        service = MarketAlertService(
            Themes(), None, None, None, MarketData(), theme_limit=1, theme_candidate_pool=1,
            stocks_per_theme=1, news_per_stock=3, max_events_per_stock=3, news_lookback_hours=24,
        )
        report = service.run("premarket", now)
        self.assertEqual(report.themes, ())

    def test_preopen_requires_kis_credentials(self):
        class DisabledMarketData:
            enabled = False

        service = MarketAlertService(
            None, None, None, None, DisabledMarketData(), theme_limit=1, theme_candidate_pool=1,
            stocks_per_theme=1, news_per_stock=3, max_events_per_stock=3, news_lookback_hours=24,
        )
        with self.assertRaisesRegex(ValueError, "KIS_APP_KEY"):
            service.run("preopen")


class FormattingTest(unittest.TestCase):
    def test_formats_confirmation_report(self):
        now = datetime(2026, 10, 7, 9, 10, tzinfo=ZoneInfo("Asia/Seoul"))
        event = MarketEvent("공급계약", "", "https://example.com", now, "disclosure")
        summary = EventSummary(72, 0.9, 1.0, "contract", "open", 0.9, 2, event)
        stock = Stock("005930", "삼성전자", 4.2, 42_000_000_000, 150_000, 1_000_000)
        theme = Theme("1", "반도체", 3.2, 0.8, 1000, (stock,))
        result = StockAnalysis(stock, summary, (event,), 81, "거래 확인")
        report = DailyReport(now, "confirmation", (ThemeAnalysis(theme, (result,), 84),))
        message = format_report(report)
        self.assertIn("09:10 장초 최종확인", message)
        self.assertIn("거래 확인", message)
        self.assertIn("전일거래량 대비 0.15배", message)
        self.assertIn("핵심 공시", message)
        self.assertIn("모든 투자 판단과 손익의 책임은 이용자 본인", message)
        self.assertIn("서비스 제공자는 투자 결과에 책임지지 않습니다", message)

    def test_formats_preopen_report_as_intermediate_signal(self):
        now = datetime(2026, 10, 7, 8, 55, tzinfo=ZoneInfo("Asia/Seoul"))
        stock = Stock("005930", "삼성전자", 4.2)
        quote = PreopenQuote(72_100, 3.15, 123_456, 1_000, 2_500)
        result = StockAnalysis(stock, None, score=78, signal="장전 유효", preopen_quote=quote)
        theme = Theme("1", "반도체", 3.2, 0.8, 1000, (stock,))
        pattern = ThemePattern(65, -12, 8, 0.55, 0.75, 84, 3.2, 20, 0.4)
        report = DailyReport(now, "preopen", (ThemeAnalysis(theme, (result,), 82, pattern),))
        message = format_report(report)
        self.assertIn("08:55 장전 중간확정", message)
        self.assertIn("예상 +3.15%", message)
        self.assertIn("매수/매도 잔량비 2.50배", message)
        self.assertIn("3개월 고점상승 +65.0%", message)
        self.assertIn("하락거래대금비 0.55", message)
        self.assertIn("거래대금 최대 3.2배", message)
        self.assertIn("폭발 후 20거래일", message)
        self.assertIn("09:10 최종 확인 전 중간 신호", message)
        self.assertIn("모든 투자 판단과 손익의 책임은 이용자 본인", message)


class ReportStoreTest(unittest.TestCase):
    def test_saves_web_report_without_detailed_provider_errors(self):
        now = datetime(2026, 10, 7, 9, 10, tzinfo=ZoneInfo("Asia/Seoul"))
        event = MarketEvent("공급계약", "not persisted", "https://example.com/news", now, "news")
        summary = EventSummary(72, 0.9, 1.0, "contract", "open", 0.9, 1, event)
        stock = StockAnalysis(
            Stock("005930", "삼성전자", 4.2, 42_000_000_000, 150_000, 1_000_000),
            summary,
            (event,),
            81,
            "거래 확인",
            "DART request contained secret-key",
        )
        report = DailyReport(
            now,
            "confirmation",
            (ThemeAnalysis(Theme("1", "반도체", 3.2, 0.8, 1000), (stock,), 84),),
            ("provider URL contained secret-key",),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reports" / "2026-10-07" / "confirmation.json"
            save_report(path, report, "telegram output")
            payload = json.loads(path.read_text(encoding="utf-8"))
            index = json.loads((path.parent.parent / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["warning_count"], 1)
        self.assertNotIn("warnings", payload)
        self.assertTrue(payload["themes"][0]["stocks"][0]["has_error"])
        self.assertNotIn("error", payload["themes"][0]["stocks"][0])
        self.assertEqual(payload["themes"][0]["stocks"][0]["events"][0]["title"], "공급계약")
        self.assertNotIn("description", payload["themes"][0]["stocks"][0]["events"][0])
        self.assertEqual(index["version"], 1)
        self.assertEqual(
            index["reports"],
            [
                {
                    "market_date": "2026-10-07",
                    "mode": "confirmation",
                    "generated_at": "2026-10-07T09:10:00+09:00",
                    "path": "reports/2026-10-07/confirmation.json",
                }
            ],
        )

    def test_report_index_keeps_existing_reports_in_latest_first_order(self):
        morning = datetime(2026, 10, 7, 7, 30, tzinfo=ZoneInfo("Asia/Seoul"))
        confirmation = datetime(2026, 10, 7, 9, 10, tzinfo=ZoneInfo("Asia/Seoul"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "reports" / "2026-10-07"
            save_report(root / "premarket.json", DailyReport(morning, "premarket"), "morning")
            save_report(
                root / "confirmation.json",
                DailyReport(confirmation, "confirmation"),
                "confirmation",
            )
            index = json.loads((root.parent / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [item["mode"] for item in index["reports"]],
            ["confirmation", "premarket"],
        )
        self.assertEqual(index["updated_at"], confirmation.isoformat())


class GitHubRepositoryTest(unittest.TestCase):
    class Http:
        def __init__(self, responses=()):
            self.responses = iter(responses)
            self.calls = []

        def json(self, url, **kwargs):
            self.calls.append(("json", url, kwargs))
            return next(self.responses)

        def bytes(self, url, **kwargs):
            self.calls.append(("bytes", url, kwargs))
            return b""

    def test_dispatches_named_workflow(self):
        http = self.Http()
        repository = GitHubRepository(http, "Namgilu/trade-alert", "token")
        repository.dispatch("preopen")
        kind, url, kwargs = http.calls[0]
        self.assertEqual(kind, "bytes")
        self.assertTrue(url.endswith("/actions/workflows/preopen-alert.yml/dispatches"))
        self.assertEqual(kwargs["body"], {"ref": "main"})

    def test_loads_reports_from_data_branch(self):
        report = {"market_date": "2026-10-07", "generated_at": "2026-10-07T09:10:00+09:00", "mode": "confirmation"}
        blob = base64.b64encode(json.dumps(report).encode()).decode()
        http = self.Http(
            [
                {"truncated": False, "tree": [{"type": "blob", "path": "reports/2026-10-07/confirmation.json", "sha": "abc"}]},
                {"encoding": "base64", "content": blob},
            ]
        )
        repository = GitHubRepository(http, "Namgilu/trade-alert", "token")
        self.assertEqual(repository.reports(5), [report])
        self.assertIn("/git/trees/data?recursive=1", http.calls[0][1])


class SchedulerTest(unittest.TestCase):
    def test_has_three_korean_market_dispatches(self):
        self.assertEqual(
            [(item.mode, item.hour, item.minute) for item in WORKFLOW_SCHEDULES],
            [("premarket", 7, 30), ("preopen", 8, 55), ("confirmation", 9, 10)],
        )


class SettingsTest(unittest.TestCase):
    def test_optional_dart_and_limits(self):
        env = {
            "NAVER_CLIENT_ID": "id",
            "NAVER_CLIENT_SECRET": "secret",
            "JEV_API_KEY": "jev",
            "THEME_LIMIT": "9",
            "THEME_CANDIDATE_POOL": "30",
            "STOCKS_PER_THEME": "7",
            "MAX_EVENTS_PER_STOCK": "30",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env(require_telegram=False)
        self.assertEqual(settings.theme_limit, 3)
        self.assertEqual(settings.theme_candidate_pool, 10)
        self.assertEqual(settings.theme_scan_limit, 100)
        self.assertEqual(settings.theme_screen_stocks, 3)
        self.assertEqual(settings.stocks_per_theme, 3)
        self.assertEqual(settings.max_events_per_stock, 10)
        self.assertEqual(settings.dart_api_key, "")
        self.assertEqual(settings.kis_app_key, "")
        self.assertEqual(settings.kis_base_url, "https://openapi.koreainvestment.com:9443")
        self.assertEqual(settings.history_lookback_days, 100)
        self.assertEqual(settings.history_min_bars, 50)


if __name__ == "__main__":
    unittest.main()
