import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from kline_agent.live_market import LiveMarketSnapshot
from kline_agent.models import KLine, Security
from kline_agent.service import AgentError, KLineAgent


def make_bars(
    closes: list[float],
    end_date: date,
) -> list[KLine]:
    """构造在指定日期结束的连续测试 K 线。"""
    first_date = end_date - timedelta(days=len(closes) - 1)
    return [
        KLine(
            (first_date + timedelta(days=index)).isoformat(),
            close * 0.99,
            close * 1.02,
            close * 0.98,
            close,
            10_000 + index * 137 + index % 3 * 50,
        )
        for index, close in enumerate(closes)
    ]


class FakeMarketDataSource:
    """为服务层测试提供确定的目标图表和全市场最新 K 线。"""

    def __init__(self) -> None:
        self.target = Security(1, "600001", "目标股票", "SH")
        self.same = Security(0, "000001", "当前同形", "SZ")
        self.other = Security(0, "000002", "其他走势", "SZ")
        self.end_date = date(2026, 6, 30)
        selected_shape = [
            10,
            11,
            10.5,
            12,
            11.5,
            13,
            12.5,
            14,
            13.5,
            15,
            14.5,
            16,
            15.5,
            17,
            16.5,
        ]
        target_closes = (
            [20 + index * 0.03 for index in range(20)]
            + selected_shape
            + [30 + index * 0.02 for index in range(1265)]
        )
        self.target_bars = make_bars(target_closes, self.end_date)
        self.market_bars = {
            self.target.code: self.target_bars,
            self.same.code: make_bars(
                [value * 5 for value in target_closes[:35]],
                self.end_date,
            ),
            self.other.code: make_bars(
                [70 - index * 0.01 for index in range(1300)],
                self.end_date,
            ),
        }

    def get_bars(self, security: Security, period: int) -> list[KLine]:
        if security.code == self.target.code:
            return self.target_bars[-period:]
        return self.market_bars[security.code][-period:]

    def get_securities(self) -> list[Security]:
        return [self.target, self.same, self.other]

    def iter_bars(
        self,
        securities: list[Security],
        period: int,
        progress: object = None,
    ):
        for completed, security in enumerate(securities, start=1):
            bars = self.market_bars[security.code][-period:]
            if callable(progress):
                progress(completed, len(securities), completed)
            yield security, bars


class FakeLiveMarketSource:
    """返回比历史缓存晚一个交易日的可控实时快照。"""

    def __init__(self, source: FakeMarketDataSource) -> None:
        self._source = source

    def fetch(
        self,
        securities: list[Security],
        progress: object = None,
    ) -> LiveMarketSnapshot:
        entries = tuple(
            (
                security,
                KLine(
                    "2026-07-01",
                    self._source.market_bars[security.code][-1].close,
                    self._source.market_bars[security.code][-1].close * 1.02,
                    self._source.market_bars[security.code][-1].close * 0.98,
                    self._source.market_bars[security.code][-1].close * 1.01,
                    123_456,
                ),
            )
            for security in securities
        )
        if callable(progress):
            progress(1, 1, 1)
        return LiveMarketSnapshot(
            "2026-07-01",
            datetime(2026, 7, 1, 10, 30, tzinfo=timezone.utc),
            entries,
        )


class ServiceTests(unittest.TestCase):
    def test_extracts_code_from_chinese_query(self) -> None:
        self.assertEqual(
            KLineAgent.extract_code("查找和 600519 K线相似的股票"),
            "600519",
        )

    def test_rejects_missing_code(self) -> None:
        with self.assertRaises(AgentError):
            KLineAgent.extract_code("查找贵州茅台")

    def test_rejects_non_stock_code(self) -> None:
        with self.assertRaises(AgentError):
            KLineAgent.extract_code("查找 510300")

    def test_searches_latest_market_with_selected_history(self) -> None:
        source = FakeMarketDataSource()
        with tempfile.TemporaryDirectory() as directory:
            with KLineAgent(
                Path(directory) / "test.db",
                source=source,
            ) as agent:
                target, chart_bars, offline = agent.load_chart("600001")
                self.assertEqual(len(chart_bars), KLineAgent.CHART_PERIOD)
                selected_bars = chart_bars[20:35]
                resolved, data_date, results, search_offline = (
                    agent.search_segment(
                        target,
                        selected_bars,
                        context_bars=chart_bars,
                    )
                )

        self.assertFalse(offline)
        self.assertFalse(search_offline)
        self.assertEqual(resolved.name, "目标股票")
        self.assertEqual(data_date, "2026-06-30")
        self.assertEqual(results[0].security.code, "000001")
        self.assertAlmostEqual(results[0].score, 100.0, places=8)
        self.assertAlmostEqual(results[0].kline_score or 0, 100.0, places=8)
        self.assertAlmostEqual(
            results[0].moving_average_score or 0,
            100.0,
            places=8,
        )
        self.assertAlmostEqual(results[0].volume_score or 0, 100.0, places=8)
        self.assertAlmostEqual(results[0].macd_score or 0, 100.0, places=8)

    def test_searches_saved_pattern_after_reopening(self) -> None:
        """框选收藏应在重新打开服务后仍能直接搜索。"""
        source = FakeMarketDataSource()
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with KLineAgent(database_path, source=source) as agent:
                target, chart_bars, _offline = agent.load_chart("600001")
                favorite = agent.save_favorite_pattern(
                    target,
                    chart_bars[20:35],
                    chart_bars,
                )
                favorite_id = favorite.favorite_id
                self.assertEqual(favorite.selection_start, 20)
                self.assertEqual(
                    favorite.selected_bars,
                    tuple(chart_bars[20:35]),
                )

            with KLineAgent(database_path, source=source) as agent:
                favorites = agent.list_favorite_patterns()
                (
                    loaded_favorite,
                    resolved,
                    data_date,
                    results,
                    offline,
                ) = agent.search_favorite_pattern(
                    favorite_id,
                    selected_filters=("kline", "volume", "macd"),
                )

            self.assertEqual(favorites, [loaded_favorite])
            self.assertEqual(loaded_favorite.favorite_id, favorite_id)
            self.assertEqual(resolved, source.target)
            self.assertEqual(data_date, "2026-06-30")
            self.assertFalse(offline)
            self.assertEqual(results[0].security.code, "000001")
            self.assertAlmostEqual(results[0].score, 100.0, places=8)

    def test_favorite_preserves_ma120_preview_context(self) -> None:
        """新收藏应保留足够数据绘制 MA120 预览。"""
        source = FakeMarketDataSource()
        with tempfile.TemporaryDirectory() as directory:
            with KLineAgent(
                Path(directory) / "test.db",
                source=source,
            ) as agent:
                selected_bars = source.target_bars[150:165]
                favorite = agent.save_favorite_pattern(
                    source.target,
                    selected_bars,
                    source.target_bars,
                )

        self.assertEqual(
            favorite.selection_start,
            KLineAgent.FAVORITE_PREVIEW_WARMUP,
        )
        self.assertEqual(
            len(favorite.context_bars),
            KLineAgent.FAVORITE_PREVIEW_WARMUP + len(selected_bars),
        )
        self.assertEqual(favorite.selected_bars, tuple(selected_bars))

    def test_rejects_selection_outside_allowed_range(self) -> None:
        source = FakeMarketDataSource()
        with tempfile.TemporaryDirectory() as directory:
            with KLineAgent(
                Path(directory) / "test.db",
                source=source,
            ) as agent:
                with self.assertRaises(AgentError):
                    agent.search_segment(
                        source.target,
                        source.target_bars[: KLineAgent.MIN_SELECTION - 1],
                    )

    def test_refreshes_live_bars_without_replacing_history(self) -> None:
        source = FakeMarketDataSource()
        with tempfile.TemporaryDirectory() as directory:
            with KLineAgent(
                Path(directory) / "test.db",
                source=source,
                live_source=FakeLiveMarketSource(source),
            ) as agent:
                agent._storage.replace_securities(source.get_securities())
                agent._storage.save_bar_batch(
                    [
                        (security, source.market_bars[security.code])
                        for security in source.get_securities()
                    ]
                )
                snapshot = agent.refresh_live_market()
                target, chart_bars, offline = agent.load_chart(
                    source.target.code
                )
                historical = agent._storage.load_security_bars(
                    source.target.code,
                    KLineAgent.CHART_PERIOD,
                )

        self.assertEqual(snapshot.market_date, "2026-07-01")
        self.assertEqual(target, source.target)
        self.assertFalse(offline)
        self.assertEqual(chart_bars[-1].trade_date, "2026-07-01")
        self.assertIsNotNone(historical)
        assert historical is not None
        self.assertEqual(historical[1][-1].trade_date, "2026-06-30")


if __name__ == "__main__":
    unittest.main()
