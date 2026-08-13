import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from kline_agent.models import KLine, Security
from kline_agent.storage import KLineStorage


class StorageTests(unittest.TestCase):
    def test_persists_favorite_independently_from_market_cache(self) -> None:
        """收藏应独立保存，并在证券缓存清空后仍可读取和删除。"""
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            security = Security(1, "600519", "贵州茅台", "SH")
            bars = [
                KLine(
                    (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
                    10 + index,
                    11 + index,
                    9 + index,
                    10.5 + index,
                    10_000 + index,
                )
                for index in range(20)
            ]
            with KLineStorage(database_path) as storage:
                storage.replace_securities([security])
                favorite_id = storage.save_favorite_pattern(
                    "贵州茅台测试收藏",
                    security,
                    bars,
                    5,
                    15,
                    "2026-08-13T12:00:00+08:00",
                )
                storage.replace_securities([])

            with KLineStorage(database_path) as storage:
                favorite = storage.load_favorite_pattern(favorite_id)
                self.assertIsNotNone(favorite)
                assert favorite is not None
                self.assertEqual(favorite.security, security)
                self.assertEqual(favorite.context_bars, tuple(bars))
                self.assertEqual(favorite.selected_bars, tuple(bars[5:20]))
                self.assertEqual(
                    storage.load_favorite_patterns(),
                    [favorite],
                )
                self.assertTrue(
                    storage.delete_favorite_pattern(favorite_id)
                )
                self.assertIsNone(
                    storage.load_favorite_pattern(favorite_id)
                )
                self.assertEqual(storage.load_favorite_patterns(), [])

    def test_loads_recent_bars_with_both_query_strategies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = KLineStorage(Path(directory) / "test.db")
            complete = Security(1, "600519", "贵州茅台", "SH")
            shorter = Security(0, "000001", "平安银行", "SZ")

            def build_bars(count: int) -> list[KLine]:
                # 使用连续日期，验证两种查询策略都保持时间正序。
                return [
                    KLine(
                        (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
                        10 + index,
                        11 + index,
                        9 + index,
                        10.5 + index,
                        10_000 + index,
                    )
                    for index in range(count)
                ]

            complete_bars = build_bars(80)
            shorter_bars = build_bars(50)
            storage.replace_securities([complete, shorter])
            storage.save_bar_batch(
                [
                    (complete, complete_bars),
                    (shorter, shorter_bars),
                ]
            )

            recent = storage.load_universe(period=20)
            long_range = storage.load_universe(period=60)

            self.assertEqual(
                recent,
                [
                    (shorter, shorter_bars[-20:]),
                    (complete, complete_bars[-20:]),
                ],
            )
            self.assertEqual(long_range, [(complete, complete_bars[-60:])])
            storage.close()

    def test_saves_and_loads_complete_universe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = KLineStorage(Path(directory) / "test.db")
            security = Security(1, "600519", "贵州茅台", "SH")
            bars = [
                KLine(
                    f"2026-01-{index:02d}",
                    10,
                    11,
                    9,
                    10.5,
                    index * 10_000,
                )
                for index in range(1, 4)
            ]

            storage.replace_securities([security])
            storage.save_bar_batch([(security, bars)])
            storage.set_metadata("data_date", "2026-01-03")
            universe = storage.load_universe(period=3)
            latest = storage.load_security_bars("600519", period=2)

            self.assertEqual(storage.security_count(), 1)
            self.assertEqual(storage.get_metadata("data_date"), "2026-01-03")
            self.assertEqual(universe, [(security, bars)])
            self.assertEqual(latest, (security, bars[-2:]))
            storage.close()

    def test_migrates_old_database_and_saves_volume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "old.db"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                """
                CREATE TABLE securities (
                    market INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    PRIMARY KEY (market, code)
                );
                CREATE TABLE bars (
                    market INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    PRIMARY KEY (market, code, trade_date)
                );
                INSERT INTO securities
                    (market, code, name, exchange)
                VALUES
                    (1, '600519', '贵州茅台', 'SH');
                INSERT INTO bars
                    (market, code, trade_date, open, high, low, close)
                VALUES
                    (1, '600519', '2026-01-01', 10, 11, 9, 10.5);
                """
            )
            connection.commit()
            connection.close()

            security = Security(1, "600519", "贵州茅台", "SH")
            with KLineStorage(database_path) as storage:
                migrated = storage.load_security_bars("600519", 1)
                self.assertIsNotNone(migrated)
                assert migrated is not None
                self.assertEqual(migrated[1][0].volume, 0.0)

                updated = KLine(
                    "2026-01-02",
                    10,
                    11,
                    9,
                    10.5,
                    88_000,
                )
                storage.save_bar_batch([(security, [updated])])
                loaded = storage.load_security_bars("600519", 1)

            self.assertEqual(loaded, (security, [updated]))

    def test_live_snapshot_does_not_replace_historical_bars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = KLineStorage(Path(directory) / "test.db")
            security = Security(1, "600519", "贵州茅台", "SH")
            historical_bar = KLine(
                "2026-06-30",
                10,
                11,
                9,
                10.5,
                80_000,
            )
            live_bar = KLine(
                "2026-07-01",
                10.6,
                11.2,
                10.3,
                11,
                90_000,
            )
            storage.replace_securities([security])
            storage.save_bar_batch([(security, [historical_bar])])

            storage.replace_live_snapshot(
                [(security, live_bar)],
                live_bar.trade_date,
                "2026-07-01T10:30:00+08:00",
            )

            self.assertEqual(
                storage.load_security_bars(security.code, 1),
                (security, [historical_bar]),
            )
            self.assertEqual(storage.load_live_bar(security.code), live_bar)
            self.assertEqual(
                storage.load_live_bars(),
                {(security.market, security.code): live_bar},
            )
            self.assertEqual(
                storage.get_metadata("live_market_date"),
                live_bar.trade_date,
            )
            storage.close()


if __name__ == "__main__":
    unittest.main()
