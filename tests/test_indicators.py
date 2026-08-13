import unittest

from kline_agent.indicators import calculate_indicators
from kline_agent.models import KLine


def make_bars(closes: list[float]) -> list[KLine]:
    """根据收盘价构造指标测试数据。"""
    return [
        KLine(
            f"2026-01-{index:02d}",
            close,
            close,
            close,
            close,
            10_000 + index * 100,
        )
        for index, close in enumerate(closes, start=1)
    ]


class IndicatorTests(unittest.TestCase):
    def test_calculates_moving_averages(self) -> None:
        points = calculate_indicators(
            make_bars([float(value) for value in range(1, 131)])
        )

        self.assertIsNone(points[3].ma5)
        self.assertAlmostEqual(points[4].ma5 or 0, 3.0)
        self.assertAlmostEqual(points[9].ma10 or 0, 5.5)
        self.assertAlmostEqual(points[19].ma20 or 0, 10.5)
        self.assertAlmostEqual(points[29].ma20 or 0, 20.5)
        self.assertAlmostEqual(points[29].ma30 or 0, 15.5)
        self.assertAlmostEqual(points[59].ma60 or 0, 30.5)
        self.assertIsNone(points[118].ma120)
        self.assertAlmostEqual(points[119].ma120 or 0, 60.5)
        self.assertAlmostEqual(points[129].ma120 or 0, 70.5)

    def test_calculates_standard_macd_parameters(self) -> None:
        points = calculate_indicators(make_bars([10.0, 11.0]))
        expected_dif = (10 + 2 / 13) - (10 + 2 / 27)
        expected_dea = expected_dif * 2 / 10

        self.assertAlmostEqual(points[0].dif, 0.0)
        self.assertAlmostEqual(points[1].dif, expected_dif)
        self.assertAlmostEqual(points[1].dea, expected_dea)
        self.assertAlmostEqual(
            points[1].macd,
            (expected_dif - expected_dea) * 2,
        )

    def test_rejects_invalid_close(self) -> None:
        with self.assertRaises(ValueError):
            calculate_indicators(make_bars([10.0, 0.0]))


if __name__ == "__main__":
    unittest.main()
