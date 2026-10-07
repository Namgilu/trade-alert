from __future__ import annotations

import os
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from trade_alert.config import Settings
from trade_alert.models import DailyReport, Sentiment, Stock, StockAnalysis, Theme, ThemeAnalysis
from trade_alert.providers import JevSentimentProvider, NaverThemeProvider
from trade_alert.service import format_report


class FakeHttp:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


class ThemeProviderTest(unittest.TestCase):
    def test_selects_top_themes_and_stocks(self):
        http = FakeHttp(
            [
                {"items": [{"no": "20", "name": "B", "changeRate": "1.2"}, {"no": "10", "name": "A", "changeRate": "3.5"}]},
                [{"itemcode": "2", "itemname": "둘", "prevChangeRate": "2"}, {"itemcode": "1", "itemname": "하나", "prevChangeRate": "5"}],
                [{"itemcode": "3", "itemname": "셋", "prevChangeRate": "1"}],
            ]
        )
        provider = NaverThemeProvider(http, "themes", "theme/{theme_id}")
        result = provider.top_themes(2, 1)
        self.assertEqual([theme.name for theme in result], ["A", "B"])
        self.assertEqual(result[0].stocks[0].name, "하나")


class JevProviderTest(unittest.TestCase):
    def test_averages_probabilities(self):
        now = datetime.now(ZoneInfo("Asia/Seoul"))
        from trade_alert.models import NewsArticle

        articles = [NewsArticle("좋은 뉴스", "설명", "https://example.com", now)]
        http = FakeHttp([{"answers": {"news_0": {"choice": "positive", "probabilities": {"positive": 0.8, "neutral": 0.1, "negative": 0.1}}}}])
        provider = JevSentimentProvider(http, "https://jev", "secret", "jev-latest")
        result = provider.analyze(Stock("005930", "삼성전자"), articles)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.positive, 0.8)
        self.assertEqual(result.outlook, "강한 긍정")
        self.assertEqual(http.calls[0][1]["headers"]["Authorization"], "Bearer secret")


class FormattingTest(unittest.TestCase):
    def test_formats_report(self):
        stock = Stock("005930", "삼성전자", 2.1)
        theme = Theme("1", "반도체", 3.2, (stock,))
        result = StockAnalysis(stock, Sentiment(0.7, 0.2, 0.1, 5))
        report = DailyReport(datetime(2026, 10, 7, 7, 30, tzinfo=ZoneInfo("Asia/Seoul")), (ThemeAnalysis(theme, (result,)),))
        message = format_report(report)
        self.assertIn("반도체", message)
        self.assertIn("긍정 70%", message)
        self.assertIn("투자 권유가 아닙니다", message)


class SettingsTest(unittest.TestCase):
    def test_limits_requested_ranks_to_three(self):
        env = {
            "NAVER_CLIENT_ID": "id",
            "NAVER_CLIENT_SECRET": "secret",
            "JEV_API_KEY": "jev",
            "THEME_LIMIT": "9",
            "STOCKS_PER_THEME": "7",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env(require_telegram=False)
        self.assertEqual(settings.theme_limit, 3)
        self.assertEqual(settings.stocks_per_theme, 3)


if __name__ == "__main__":
    unittest.main()
