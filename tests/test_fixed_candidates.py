from __future__ import annotations

import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from trade_alert.candidate_cache import load_candidates, save_candidates
from trade_alert.cli import main
from trade_alert.models import DailyReport, PreopenQuote, Stock, Theme, ThemeAnalysis, ThemePattern
from trade_alert.report_store import report_payload
from trade_alert.service import MarketAlertService, format_report


NOW = datetime(2026, 10, 8, 7, 30, tzinfo=ZoneInfo("Asia/Seoul"))
PATTERN = ThemePattern(65, -12, 8, 0.55, 0.75, 84)


class FixedCandidatesTest(unittest.TestCase):
    def setUp(self):
        self.themes = [
            Theme(str(i), f"테마{i}", i * 2, 0.8, i * 1000,
                  (Stock(str(i), f"종목{i}", i * 3, i * 100000, i * 100, 1000),))
            for i in range(1, 6)
        ]
        self.candidates = [(theme, PATTERN) for theme in self.themes]
        self.provider = Mock()
        # The provider's response order must not determine display order.
        self.provider.refresh_themes.return_value = self.themes[::-1]
        self.provider.hydrate_theme.side_effect = lambda theme, _: replace(
            theme, stocks=self.themes[int(theme.id) - 1].stocks
        )
        news = Mock()
        news.recent.return_value = []
        market = Mock()
        market.quote.return_value = PreopenQuote(100, 2, 100, 100, 200)
        self.service = MarketAlertService(
            self.provider, news, Mock(enabled=False), Mock(), market,
            theme_limit=1, theme_candidate_pool=5, stocks_per_theme=3,
            news_per_stock=10, max_events_per_stock=5, news_lookback_hours=24,
        )

    def test_followups_keep_every_saved_theme_and_morning_order_despite_new_scores(self):
        for mode in ("preopen", "confirmation", "premarket"):
            with self.subTest(mode=mode):
                report = self.service.run(mode, NOW, screened_candidates=self.candidates, fixed_selection=True)
                self.assertEqual([item.theme.id for item in report.themes], ["1", "2", "3", "4", "5"])
                if mode != "premarket":
                    self.assertGreater(report.themes[-1].score, report.themes[0].score)
        self.provider.screening_themes.assert_not_called()

    def test_morning_five_theme_selection_is_the_persisted_followup_universe(self):
        self.service.theme_limit = 5
        morning = self.service.run("premarket", NOW, screened_candidates=self.candidates)
        self.assertEqual(len(morning.themes), 5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            save_candidates(path, NOW.date(), [(item.theme, item.pattern) for item in morning.themes])
            candidates = load_candidates(path, NOW.date())
            for mode in ("preopen", "confirmation"):
                followup = self.service.run(mode, NOW, screened_candidates=candidates)
                self.assertEqual(
                    [item.theme.id for item in followup.themes],
                    [item.theme.id for item in morning.themes],
                )

    def test_hydration_failure_or_empty_response_keeps_placeholder(self):
        for empty in (False, True):
            def hydrate(theme, _):
                if theme.id == "2":
                    if empty:
                        return Theme(theme.id, theme.name)
                    raise RuntimeError("provider unavailable")
                return theme
            self.provider.hydrate_theme.side_effect = hydrate
            report = self.service.run("confirmation", NOW, screened_candidates=self.candidates)
            self.assertEqual([item.theme.id for item in report.themes], ["1", "2", "3", "4", "5"])
            failed = report.themes[1]
            self.assertTrue(failed.data_error)
            self.assertIsNone(failed.theme.change_rate)
            self.assertIn("2. 테마2 · 데이터 확인 실패", format_report(report))
            payload = report_payload(report, format_report(report))
            self.assertTrue(payload["themes"][1]["data_error"])
            self.assertNotIn("provider unavailable", json.dumps(payload))

    def test_refresh_missing_theme_or_failure_never_reuses_stale_market_data(self):
        self.provider.refresh_themes.return_value = [self.themes[0], self.themes[2]]
        report = self.service.run("confirmation", NOW, screened_candidates=self.candidates)
        self.assertFalse(report.themes[1].data_error)
        self.assertFalse(report.themes[0].summary_unavailable)
        self.assertIsNotNone(report.themes[0].score)
        self.assertTrue(report.themes[1].summary_unavailable)
        self.assertEqual(len(report.themes), 5)
        self.assertEqual(self.provider.hydrate_theme.call_count, 5)
        for index in (1, 3, 4):
            item = report.themes[index]
            self.assertIsNone(item.score)
            self.assertIsNone(item.theme.change_rate)
            self.assertIsNone(item.theme.breadth)
            self.assertIsNone(item.theme.trading_value)
            self.assertTrue(item.stocks)
        payload = report_payload(report, format_report(report))
        self.assertTrue(payload["themes"][1]["summary_unavailable"])
        self.assertIsNone(payload["themes"][1]["score"])
        self.assertIn("테마 전체 지표 미확보", format_report(report))
        self.assertNotIn("데이터 확인 실패", format_report(report))
        self.provider.refresh_themes.side_effect = RuntimeError("offline")
        report = self.service.run("confirmation", NOW, screened_candidates=self.candidates)
        self.assertTrue(all(not item.data_error and item.summary_unavailable and item.stocks for item in report.themes))
        self.assertTrue(all(item.theme.change_rate is None for item in report.themes))

    def test_partial_summary_uses_stock_only_score_without_fabricated_theme_score(self):
        self.provider.refresh_themes.return_value = [replace(self.themes[0], breadth=None)]
        report = self.service.run("confirmation", NOW, screened_candidates=[self.candidates[0]])
        item = report.themes[0]
        self.assertTrue(item.summary_unavailable)
        self.assertIsNone(item.score)
        # Amount: 50 (one stock); price: (3 + 3) / 13 * 100;
        # volume: 100 / 1000 / .15 * 100; event: neutral 50.
        expected = (0.30 * 50 + 0.20 * (6 / 13 * 100) + 0.20 * (0.1 / 0.15 * 100) + 0.15 * 50) / 0.85
        self.assertAlmostEqual(item.stocks[0].score, expected)

    def test_missing_summary_does_not_mask_real_stock_failure(self):
        self.provider.refresh_themes.return_value = []
        self.provider.hydrate_theme.side_effect = RuntimeError("offline")
        report = self.service.run("confirmation", NOW, screened_candidates=self.candidates)
        self.assertTrue(all(item.data_error and not item.stocks for item in report.themes))

    def test_followups_without_selection_fail_without_screening(self):
        for mode in ("preopen", "confirmation"):
            with self.assertRaisesRegex(ValueError, "당일 장전 후보"):
                self.service.run(mode, NOW)
        self.provider.screening_themes.assert_not_called()

    def test_quote_outage_keeps_all_themes_without_false_signals(self):
        self.service.market_data.quote.side_effect = RuntimeError("offline")
        report = self.service.run("preopen", NOW, screened_candidates=self.candidates)
        self.assertEqual(len(report.themes), 5)
        self.assertTrue(all(item.data_error for item in report.themes))
        self.assertNotIn("장전 유효", format_report(report))

    def test_v3_pool_is_not_accepted_as_a_final_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            save_candidates(path, NOW.date(), self.candidates)
            payload = json.loads(path.read_text())
            payload["version"] = 3
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "unsupported"):
                load_candidates(path, NOW.date())

    def test_five_candidates_roundtrip_and_duplicates_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            save_candidates(path, NOW.date(), self.candidates)
            self.assertEqual(len(load_candidates(path, NOW.date())), 5)
            save_candidates(path, NOW.date(), [self.candidates[0], self.candidates[0]])
            with self.assertRaisesRegex(ValueError, "duplicate"):
                load_candidates(path, NOW.date())

    def test_cli_saves_final_report_selection_and_reuses_it_on_rerun(self):
        selected = [self.candidates[2], self.candidates[0]]
        report = DailyReport(NOW, "premarket", tuple(
            ThemeAnalysis(theme, pattern=pattern) for theme, pattern in selected
        ))
        with tempfile.TemporaryDirectory() as directory, \
                patch("trade_alert.cli.Settings.from_env"), \
                patch("trade_alert.cli.HttpClient"), \
                patch("trade_alert.cli.MarketAlertService") as factory, \
                patch("trade_alert.cli.datetime") as clock, \
                patch("sys.stdout", new_callable=io.StringIO):
            clock.now.return_value = NOW
            service = factory.return_value
            service.prepare_candidates.return_value = (self.candidates, (), None)
            service.run.return_value = report
            path = Path(directory) / "candidates.json"
            args = ["--mode", "premarket", "--candidate-file", str(path), "--dry-run"]
            self.assertEqual(main(args), 0)
            self.assertEqual([theme.id for theme, _ in load_candidates(path, NOW.date())], ["3", "1"])
            original_bytes = path.read_bytes()
            service.prepare_candidates.reset_mock()
            self.assertEqual(main(args), 0)
            service.prepare_candidates.assert_not_called()
            self.assertTrue(service.run.call_args.kwargs["fixed_selection"])
            self.assertEqual(path.read_bytes(), original_bytes)

    def test_cli_missing_invalid_or_wrong_date_selection_fails_before_analysis(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("trade_alert.cli.Settings.from_env"), \
                patch("trade_alert.cli.HttpClient"), \
                patch("trade_alert.cli.MarketAlertService") as factory, \
                patch("trade_alert.cli.datetime") as clock, \
                patch("sys.stderr", new_callable=io.StringIO):
            clock.now.return_value = NOW
            path = Path(directory) / "candidates.json"
            args = ["--mode", "confirmation", "--candidate-file", str(path), "--dry-run"]
            self.assertEqual(main(args), 1)
            save_candidates(path, NOW.date().replace(day=7), self.candidates)
            self.assertEqual(main(args), 1)
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(main(args), 1)
            factory.return_value.prepare_candidates.assert_not_called()
            factory.return_value.run.assert_not_called()

    def test_empty_selection_stays_empty(self):
        report = self.service.run("preopen", NOW, screened_candidates=[])
        self.assertEqual(report.themes, ())
        self.provider.hydrate_theme.assert_not_called()


if __name__ == "__main__":
    unittest.main()
