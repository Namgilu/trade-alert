from __future__ import annotations

import os
import io
import unittest
import zipfile
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from trade_alert.config import Settings
from trade_alert.models import (
    DailyReport,
    EventSummary,
    MarketEvent,
    PreopenQuote,
    Stock,
    StockAnalysis,
    Theme,
    ThemeAnalysis,
)
from trade_alert.providers import KisPreopenProvider, JevEventProvider, NaverThemeProvider, OpenDartProvider, deduplicate_events
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


class KisPreopenProviderTest(unittest.TestCase):
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
        provider = KisPreopenProvider(http, "app-key", "app-secret", "https://kis.example")
        quote = provider.quote(Stock("005930", "삼성전자"))
        self.assertEqual(quote.expected_price, 72100)
        self.assertEqual(quote.expected_volume, 123456)
        self.assertEqual(quote.bid_ask_ratio, 2.5)
        self.assertIn("FID_INPUT_ISCD=005930", http.calls[1][0])
        self.assertEqual(http.calls[1][1]["headers"]["tr_id"], "FHKST01010200")
        self.assertEqual(http.calls[1][1]["headers"]["authorization"], "Bearer token")


class ServiceScoringTest(unittest.TestCase):
    def test_confirmation_requires_market_and_event_strength(self):
        now = datetime(2026, 10, 7, 9, 10, tzinfo=ZoneInfo("Asia/Seoul"))
        stock = Stock("005930", "삼성전자", 7.0, 50_000_000_000, 200_000, 1_000_000)
        theme = Theme("1", "반도체", 5.0, 1.0, 1000, (stock,))
        event = MarketEvent("공급계약", "", "url", now, "disclosure")
        summary = EventSummary(80, 1, 1, "contract", "open", 0.9, 1, event)

        class Themes:
            def top_themes(self, *_):
                return [theme]

        class News:
            def recent(self, *_):
                return [event]

        class Dart:
            enabled = False

        class Model:
            def analyze(self, *_):
                return summary

        service = MarketAlertService(
            Themes(), News(), Dart(), Model(), theme_limit=1, theme_candidate_pool=1,
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
            def top_themes(self, *_):
                return [theme]

        class News:
            def recent(self, *_):
                return [event]

        class Dart:
            enabled = False

        class Model:
            def analyze(self, *_):
                return summary

        class Kis:
            enabled = True

            def quote(self, *_):
                return PreopenQuote(80_000, 10.0, 300_000, 100_000, 900_000)

        service = MarketAlertService(
            Themes(), News(), Dart(), Model(), Kis(), theme_limit=1, theme_candidate_pool=1,
            stocks_per_theme=1, news_per_stock=3, max_events_per_stock=3, news_lookback_hours=24,
        )
        report = service.run("preopen", now)
        self.assertEqual(report.themes[0].stocks[0].signal, "장전 유효")
        self.assertGreaterEqual(report.themes[0].stocks[0].score, 72)

    def test_preopen_requires_kis_credentials(self):
        class DisabledKis:
            enabled = False

        service = MarketAlertService(
            None, None, None, None, DisabledKis(), theme_limit=1, theme_candidate_pool=1,
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

    def test_formats_preopen_report_as_intermediate_signal(self):
        now = datetime(2026, 10, 7, 8, 55, tzinfo=ZoneInfo("Asia/Seoul"))
        stock = Stock("005930", "삼성전자", 4.2)
        quote = PreopenQuote(72_100, 3.15, 123_456, 1_000, 2_500)
        result = StockAnalysis(stock, None, score=78, signal="장전 유효", preopen_quote=quote)
        theme = Theme("1", "반도체", 3.2, 0.8, 1000, (stock,))
        report = DailyReport(now, "preopen", (ThemeAnalysis(theme, (result,), 82),))
        message = format_report(report)
        self.assertIn("08:55 장전 중간확정", message)
        self.assertIn("예상 +3.15%", message)
        self.assertIn("매수/매도 잔량비 2.50배", message)
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
        self.assertEqual(settings.stocks_per_theme, 3)
        self.assertEqual(settings.max_events_per_stock, 10)
        self.assertEqual(settings.dart_api_key, "")
        self.assertEqual(settings.kis_app_key, "")
        self.assertEqual(settings.kis_base_url, "https://openapi.koreainvestment.com:9443")


if __name__ == "__main__":
    unittest.main()
