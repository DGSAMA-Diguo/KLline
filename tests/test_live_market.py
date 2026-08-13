import unittest
from datetime import datetime, timedelta

from kline_agent.live_market import (
    EastmoneyLiveMarketSource,
    LiveMarketError,
)
from kline_agent.models import Security


def make_record(
    security: Security,
    timestamp: int,
) -> dict[str, object]:
    """构造一条符合实时接口字段格式的行情记录。"""
    return {
        "f12": security.code,
        "f13": security.market,
        "f2": 10.5,
        "f5": 100_000,
        "f15": 11,
        "f16": 9,
        "f17": 10,
        "f18": 9.8,
        "f124": timestamp,
    }


class LiveMarketTests(unittest.TestCase):
    def test_fetches_and_validates_paginated_snapshot(self) -> None:
        securities = [
            Security(0, "000001", "甲", "SZ"),
            Security(0, "000002", "乙", "SZ"),
            Security(1, "600001", "丙", "SH"),
        ]
        source = EastmoneyLiveMarketSource()
        source.PAGE_SIZE = 2
        current_time = datetime.now(source.CHINA_TIMEZONE)
        timestamp = int(current_time.timestamp())
        pages = {
            1: {
                "total": 3,
                "diff": [
                    make_record(securities[0], timestamp),
                    make_record(securities[1], timestamp),
                ],
            },
            2: {
                "total": 3,
                "diff": [make_record(securities[2], timestamp)],
            },
        }
        progress: list[tuple[int, int, int]] = []
        source._fetch_page = lambda page: pages[page]

        snapshot = source.fetch(
            securities,
            lambda completed, total, succeeded: progress.append(
                (completed, total, succeeded)
            ),
        )

        self.assertEqual(snapshot.market_date, current_time.date().isoformat())
        self.assertEqual(snapshot.stock_count, 3)
        self.assertEqual(
            [entry[0].code for entry in snapshot.entries],
            ["000001", "000002", "600001"],
        )
        self.assertEqual(progress[-1], (2, 2, 2))

    def test_rejects_snapshot_with_insufficient_coverage(self) -> None:
        securities = [
            Security(0, "000001", "甲", "SZ"),
            Security(0, "000002", "乙", "SZ"),
        ]
        source = EastmoneyLiveMarketSource()
        source.PAGE_SIZE = 2
        timestamp = int(datetime.now(source.CHINA_TIMEZONE).timestamp())
        source._fetch_page = lambda _page: {
            "total": 2,
            "diff": [make_record(securities[0], timestamp)],
        }

        with self.assertRaises(LiveMarketError):
            source.fetch(securities)

    def test_rejects_invalid_price_relationship(self) -> None:
        security = Security(1, "600001", "甲", "SH")
        timestamp = int(
            datetime.now(EastmoneyLiveMarketSource.CHINA_TIMEZONE).timestamp()
        )
        record = make_record(security, timestamp)
        record["f15"] = 9.5

        parsed = EastmoneyLiveMarketSource._parse_bar(
            record,
            {security.code: security},
        )

        self.assertIsNone(parsed)

    def test_rejects_future_market_date(self) -> None:
        security = Security(1, "600001", "甲", "SH")
        source = EastmoneyLiveMarketSource()
        source.PAGE_SIZE = 1
        future_time = datetime.now(source.CHINA_TIMEZONE) + timedelta(days=2)
        source._fetch_page = lambda _page: {
            "total": 1,
            "diff": [make_record(security, int(future_time.timestamp()))],
        }

        with self.assertRaises(LiveMarketError):
            source.fetch([security])


if __name__ == "__main__":
    unittest.main()
