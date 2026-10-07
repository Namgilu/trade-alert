from __future__ import annotations

import io
import os
import unittest
import zipfile
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from trade_alert.config import Settings
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
from trade_alert.providers import KiwoomMarketDataProvider, JevEventProvider, NaverThemeProvider, OpenDartProvider, deduplicate_events
from trade_alert.service import MarketAlertService, format_report


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
    for index in range(100):
        if index < 20:
            close = 100.0 + 100.0 * index / 19.0
        elif index < 40:
            close = 200.0 - 20.0 * (index - 19) / 20.0
        else:
            close = 180.0
        volume = 400_000.0 if index >= 90 else 1_000_000.0
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


class KiwoomMarketDataProviderTest(unittest.TestCase):
    def test_gets_token_once_and_parses_expected_execution(self):
        http = FakeHttp(
            [
                {
                    "token": "token",
                    "expires_dt": "20261007235959",
                    "return_code": 0,
                },
                {
                    "return_code": 0,
                    "exp_cntr_pric": "+72100",
                    "exp_cntr_qty": "123456",
                    "base_pric": "69900",
                    "flu_rt": "+3.15",
                },
                {"return_code": 0, "tot_sel_req": "1000", "tot_buy_req": "2500"},
            ]
        )
        provider = KiwoomMarketDataProvider(http, "app-key", "app-secret", "https://kiwoom.example")
        quote = provider.quote(Stock("005930", "삼성전자"))
        self.assertEqual(quote.expected_price, 72100)
        self.assertEqual(quote.expected_volume, 123456)
        self.assertAlmostEqual(quote.expected_change_rate, (72100 / 69900 - 1) * 100)
        self.assertEqual(quote.bid_ask_ratio, 2.5)
        self.assertEqual(http.calls[0][0], "https://kiwoom.example/oauth2/token")
        self.assertEqual(http.calls[0][1]["body"]["secretkey"], "app-secret")
        self.assertEqual(http.calls[1][0], "https://kiwoom.example/api/dostk/stkinfo")
        self.assertEqual(http.calls[1][1]["method"], "POST")
        self.assertEqual(http.calls[1][1]["body"], {"stk_cd": "005930"})
        self.assertEqual(http.calls[1][1]["headers"]["api-id"], "ka10001")
        self.assertEqual(http.calls[1][1]["headers"]["authorization"], "Bearer token")
        self.assertEqual(http.calls[2][1]["headers"]["api-id"], "ka10004")

    def test_parses_adjusted_daily_history(self):
        http = FakeHttp(
            [
                {"token": "token", "expires_dt": "20261007235959", "return_code": 0},
                {
                    "return_code": 0,
                    "stk_dt_pole_chart_qry": [
                        {
                            "dt": "20261006",
                            "open_pric": "-70000",
                            "high_pric": "+72000",
                            "low_pric": "-69000",
                            "cur_prc": "+71000",
                            "trde_qty": "123456",
                        }
                    ],
                },
            ]
        )
        provider = KiwoomMarketDataProvider(http, "app-key", "app-secret", "https://kiwoom.example")
        bars = provider.history(Stock("005930", "삼성전자"), date(2026, 5, 1), date(2026, 10, 6))
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].close, 71000)
        self.assertEqual(bars[0].low, 69000)
        self.assertEqual(http.calls[1][1]["headers"]["api-id"], "ka10081")
        self.assertEqual(
            http.calls[1][1]["body"],
            {"stk_cd": "005930", "base_dt": "20261006", "upd_stkpc_tp": "1"},
        )
        cached = provider.history(Stock("005930", "삼성전자"), date(2026, 5, 1), date(2026, 10, 6))
        self.assertIs(cached, bars)
        self.assertEqual(len(http.calls), 2)


class ServiceScoringTest(unittest.TestCase):
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

    def test_preopen_requires_kiwoom_credentials(self):
        class DisabledMarketData:
            enabled = False

        service = MarketAlertService(
            None, None, None, None, DisabledMarketData(), theme_limit=1, theme_candidate_pool=1,
            stocks_per_theme=1, news_per_stock=3, max_events_per_stock=3, news_lookback_hours=24,
        )
        with self.assertRaisesRegex(ValueError, "KIWOOM_APP_KEY"):
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

    def test_formats_preopen_report_as_intermediate_signal(self):
        now = datetime(2026, 10, 7, 8, 55, tzinfo=ZoneInfo("Asia/Seoul"))
        stock = Stock("005930", "삼성전자", 4.2)
        quote = PreopenQuote(72_100, 3.15, 123_456, 1_000, 2_500)
        result = StockAnalysis(stock, None, score=78, signal="장전 유효", preopen_quote=quote)
        theme = Theme("1", "반도체", 3.2, 0.8, 1000, (stock,))
        pattern = ThemePattern(65, -12, 8, 0.55, 0.75, 84)
        report = DailyReport(now, "preopen", (ThemeAnalysis(theme, (result,), 82, pattern),))
        message = format_report(report)
        self.assertIn("08:55 장전 중간확정", message)
        self.assertIn("예상 +3.15%", message)
        self.assertIn("매수/매도 잔량비 2.50배", message)
        self.assertIn("5개월 고점상승 +65.0%", message)
        self.assertIn("하락거래량비 0.55", message)
        self.assertIn("09:10 최종 확인 전 중간 신호", message)


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
        self.assertEqual(settings.kiwoom_app_key, "")
        self.assertEqual(settings.kiwoom_base_url, "https://api.kiwoom.com")
        self.assertEqual(settings.history_min_bars, 80)


if __name__ == "__main__":
    unittest.main()
