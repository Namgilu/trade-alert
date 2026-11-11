import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from trade_alert.collection_date import resolve_market_date
from trade_alert.models import DailyBar, Theme
from trade_alert.service import MarketAlertService
from trade_alert.theme_history import ThemeHistoryStore, ThemeSeries, ThemeDailyPoint


class CollectionDateTest(unittest.TestCase):
    def market(self, day):
        market = MagicMock(enabled=True)
        market.history.return_value = (DailyBar(day, 100, 100, 100, 100, 100),)
        return market

    def now(self, text):
        return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo('Asia/Seoul'))

    def test_midnight_weekend_holiday_and_utc(self):
        for text, day in [('2026-11-11T00:31', date(2026, 11, 10)),
                          ('2026-11-14T16:10', date(2026, 11, 13)),
                          ('2026-10-09T16:10', date(2026, 10, 8))]:
            with self.subTest(text=text):
                market = self.market(day)
                self.assertEqual(resolve_market_date(self.now(text), market), day)
                self.assertEqual(resolve_market_date(self.now(text).astimezone(ZoneInfo('UTC')), market), day)

    def test_unsafe_times_missing_credentials_and_backfill(self):
        day = date(2026, 11, 10)
        for text in ['2026-11-11T08:00', '2026-11-11T10:00', '2026-11-11T16:09']:
            with self.assertRaises(ValueError):
                resolve_market_date(self.now(text), self.market(day))
        with self.assertRaises(ValueError):
            resolve_market_date(self.now('2026-11-11T00:31'), None)
        with self.assertRaises(ValueError):
            resolve_market_date(self.now('2026-11-11T00:31'), self.market(day), date(2026, 11, 9))

    def test_empty_and_conflicting_sources_fail_closed(self):
        market = self.market(date(2026, 11, 10))
        market.history.return_value = ()
        with self.assertRaises(ValueError):
            resolve_market_date(self.now('2026-11-11T00:31'), market)
        market.history.side_effect = [(DailyBar(date(2026,11,10),100,100,100,100,1),),
                                      (DailyBar(date(2026,11,9),100,100,100,100,1),)]
        with self.assertRaises(ValueError):
            resolve_market_date(self.now('2026-11-11T00:31'), market)

    def test_delayed_collection_duplicate_and_future_state(self):
        day = date(2026, 11, 10)
        service = MarketAlertService(MagicMock(), None, None, None, self.market(day),
            theme_limit=1, theme_candidate_pool=1, stocks_per_theme=1,
            news_per_stock=1, max_events_per_stock=1, news_lookback_hours=24)
        service.themes.list_themes.return_value = [Theme('1', 'theme', 2, .5, 200)]
        point = ThemeDailyPoint(date(2026,11,9),100,100,100,100)
        store = ThemeHistoryStore({'1': ThemeSeries(Theme('1','theme'), (point,))})
        service.history_min_bars = 1
        now = self.now('2026-11-11T00:31')
        result, _, action = service.collect_history(now, store)
        self.assertEqual(action, 'appended')
        self.assertEqual(result.series['1'].points[-1].date, day)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result.save_repository(root, day, collected_at=now)
            snapshot = root / 'daily/2026/11/2026-11-10.json'
            before = snapshot.read_bytes()
            self.assertIn(b'collected_at', before)
            self.assertEqual(service.collect_history(now, result)[2], 'unchanged')
            with self.assertRaises(ValueError):
                result.save_repository(root, day, collected_at=now)
            self.assertEqual(snapshot.read_bytes(), before)
        service.market_data = self.market(date(2026,11,9))
        with self.assertRaises(ValueError):
            service.collect_history(now, result)

if __name__ == '__main__':
    unittest.main()
