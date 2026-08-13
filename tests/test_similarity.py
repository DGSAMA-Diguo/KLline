import unittest

from kline_agent.models import KLine, Security
from kline_agent.similarity import (
    SimilarityError,
    build_indicator_features,
    find_similar,
    find_similar_segment,
    find_similar_with_indicators,
    normalize_kline,
)


def make_bars(
    closes: list[float],
    scale: float = 1.0,
    volume_scale: float = 1.0,
) -> list[KLine]:
    """根据收盘价构造具备实体和影线的测试 K 线。"""
    bars = []
    for index, close in enumerate(closes, start=1):
        value = close * scale
        bars.append(
            KLine(
                f"2026-01-{index:02d}",
                value * 0.99,
                value * 1.02,
                value * 0.98,
                value,
                (100_000 + index * 731 + index % 4 * 2_000)
                * volume_scale,
            )
        )
    return bars


class SimilarityTests(unittest.TestCase):
    def test_normalization_ignores_absolute_price(self) -> None:
        original = normalize_kline(make_bars([10, 11, 10.5, 12]))
        scaled = normalize_kline(make_bars([10, 11, 10.5, 12], scale=30))
        self.assertTrue((abs(original - scaled) < 1e-10).all())

    def test_identical_shape_ranks_first(self) -> None:
        target_security = Security(1, "600001", "目标", "SH")
        same_security = Security(0, "000001", "同形", "SZ")
        other_security = Security(0, "000002", "异形", "SZ")
        target_bars = make_bars([10, 11, 10.5, 12])
        universe = [
            (target_security, target_bars),
            (same_security, make_bars([10, 11, 10.5, 12], scale=5)),
            (other_security, make_bars([12, 10.5, 11, 10])),
        ]

        results = find_similar((target_security, target_bars), universe)

        self.assertEqual(results[0].security.code, "000001")
        self.assertAlmostEqual(results[0].score, 100.0, places=8)

    def test_compares_historical_segment_with_latest_candidate(self) -> None:
        target_security = Security(1, "600001", "目标", "SH")
        current_security = Security(0, "000001", "当前同形", "SZ")
        stale_security = Security(0, "000002", "过期同形", "SZ")
        target_bars = make_bars([10, 11, 10.5, 12])
        current_bars = [
            KLine(
                f"2026-02-{index:02d}",
                bar.open * 5,
                bar.high * 5,
                bar.low * 5,
                bar.close * 5,
            )
            for index, bar in enumerate(target_bars, start=1)
        ]
        universe = [
            (current_security, current_bars),
            (stale_security, target_bars),
        ]

        results = find_similar_segment(
            target_security,
            target_bars,
            universe,
            "2026-02-04",
        )

        self.assertEqual([item.security.code for item in results], ["000001"])
        self.assertAlmostEqual(results[0].score, 100.0, places=8)

    def test_indicator_features_ignore_price_and_volume_scale(self) -> None:
        closes = [10 + index * 0.2 + index % 4 * 0.1 for index in range(30)]
        original = build_indicator_features(make_bars(closes), 10, 29)
        scaled = build_indicator_features(
            make_bars(closes, scale=30, volume_scale=20),
            10,
            29,
        )

        self.assertTrue((abs(original.kline - scaled.kline) < 1e-10).all())
        self.assertTrue(
            (
                abs(original.moving_average - scaled.moving_average)
                < 1e-10
            ).all()
        )
        self.assertTrue((abs(original.macd - scaled.macd) < 1e-10).all())
        volume_correlation = float(
            original.volume @ scaled.volume / original.volume.size
        )
        self.assertGreater(volume_correlation, 0.999999)

    def test_combined_match_reports_all_scores(self) -> None:
        target_security = Security(1, "600001", "目标", "SH")
        same_security = Security(0, "000001", "四项同形", "SZ")
        other_security = Security(0, "000002", "其他走势", "SZ")
        closes = [10 + index * 0.2 + index % 4 * 0.1 for index in range(30)]
        target_bars = make_bars(closes)
        same_bars = make_bars(closes, scale=5)
        other_bars = make_bars(list(reversed(closes)))

        results = find_similar_with_indicators(
            target_security,
            target_bars,
            10,
            29,
            [
                (same_security, same_bars),
                (other_security, other_bars),
            ],
            same_bars[-1].trade_date,
        )

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

    def test_combined_score_uses_only_selected_filters(self) -> None:
        target_security = Security(1, "600001", "目标", "SH")
        candidate_security = Security(0, "000001", "候选", "SZ")
        closes = [10 + index * 0.2 + index % 4 * 0.1 for index in range(30)]
        target_bars = make_bars(closes)
        candidate_bars = make_bars(closes, scale=5)
        reversed_volumes = [
            bar.volume for bar in reversed(candidate_bars)
        ]
        candidate_bars = [
            KLine(
                bar.trade_date,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                reversed_volumes[index],
            )
            for index, bar in enumerate(candidate_bars)
        ]

        kline_results = find_similar_with_indicators(
            target_security,
            target_bars,
            10,
            29,
            [(candidate_security, candidate_bars)],
            candidate_bars[-1].trade_date,
            selected_filters=("kline",),
        )
        volume_results = find_similar_with_indicators(
            target_security,
            target_bars,
            10,
            29,
            [(candidate_security, candidate_bars)],
            candidate_bars[-1].trade_date,
            selected_filters=("volume",),
        )

        self.assertAlmostEqual(kline_results[0].score, 100.0, places=8)
        self.assertAlmostEqual(
            volume_results[0].score,
            volume_results[0].volume_score or 0,
            places=8,
        )
        self.assertLess(volume_results[0].score, 100.0)

    def test_rejects_empty_similarity_filters(self) -> None:
        security = Security(1, "600001", "目标", "SH")
        bars = make_bars([10 + index * 0.1 for index in range(20)])

        with self.assertRaises(SimilarityError):
            find_similar_with_indicators(
                security,
                bars,
                0,
                19,
                [],
                bars[-1].trade_date,
                selected_filters=(),
            )


if __name__ == "__main__":
    unittest.main()
