from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite

from .models import KLine


@dataclass(frozen=True, slots=True)
class IndicatorPoint:
    """单个交易日对应的均线和 MACD 指标。"""

    ma5: float | None
    ma10: float | None
    ma20: float | None
    ma30: float | None
    ma60: float | None
    ma120: float | None
    dif: float
    dea: float
    macd: float


def calculate_indicators(bars: Sequence[KLine]) -> list[IndicatorPoint]:
    """按六条常用均线和 MACD(12,26,9) 计算指标序列。"""
    if not bars:
        return []
    closes = [float(bar.close) for bar in bars]
    if any(not isfinite(value) or value <= 0 for value in closes):
        raise ValueError("K 线包含无效收盘价")

    prefix_sum = [0.0]
    for close in closes:
        prefix_sum.append(prefix_sum[-1] + close)

    def moving_average(index: int, period: int) -> float | None:
        if index + 1 < period:
            return None
        total = prefix_sum[index + 1] - prefix_sum[index + 1 - period]
        return total / period

    ema12 = closes[0]
    ema26 = closes[0]
    dea = 0.0
    points: list[IndicatorPoint] = []
    for index, close in enumerate(closes):
        if index > 0:
            ema12 += (close - ema12) * 2 / 13
            ema26 += (close - ema26) * 2 / 27
        dif = ema12 - ema26
        dea += (dif - dea) * 2 / 10
        macd = (dif - dea) * 2
        points.append(
            IndicatorPoint(
                moving_average(index, 5),
                moving_average(index, 10),
                moving_average(index, 20),
                moving_average(index, 30),
                moving_average(index, 60),
                moving_average(index, 120),
                dif,
                dea,
                macd,
            )
        )
    return points
