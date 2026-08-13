from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .indicators import calculate_indicators
from .models import KLine, Security, SimilarityResult


class SimilarityError(ValueError):
    """K 线无法参与相似度计算。"""


SIMILARITY_FILTERS = ("kline", "volume", "macd")


@dataclass(frozen=True, slots=True)
class IndicatorFeatures:
    """用于综合比较的四组标准化指标。"""

    kline: np.ndarray
    moving_average: np.ndarray
    volume: np.ndarray
    macd: np.ndarray


def normalize_kline(bars: Sequence[KLine]) -> np.ndarray:
    """消除绝对价格和波动幅度，仅保留 OHLC 走势形态。"""
    if len(bars) < 2:
        raise SimilarityError("至少需要两根 K 线")

    prices = np.asarray(
        [[bar.open, bar.high, bar.low, bar.close] for bar in bars],
        dtype=np.float64,
    )
    if not np.isfinite(prices).all() or np.any(prices <= 0):
        raise SimilarityError("K 线包含无效价格")

    # 对数相对价格保留累计走势，同时消除股票之间的价格量级差异。
    feature = np.log(prices / prices[0, 3]).reshape(-1)
    standard_deviation = float(feature.std())
    if standard_deviation < 1e-12:
        raise SimilarityError("K 线没有足够的价格变化")
    return (feature - feature.mean()) / standard_deviation


def _standardize(values: np.ndarray) -> np.ndarray:
    if not np.isfinite(values).all():
        raise SimilarityError("指标包含无效数值")
    standard_deviation = float(values.std())
    if standard_deviation < 1e-12:
        return np.zeros_like(values)
    return (values - values.mean()) / standard_deviation


def build_indicator_features(
    context_bars: Sequence[KLine],
    start: int,
    end: int,
) -> IndicatorFeatures:
    """使用完整上下文计算指定闭区间的四类匹配特征。"""
    if start < 0 or end < start or end >= len(context_bars):
        raise SimilarityError("指标区间无效")
    selected_bars = list(context_bars[start : end + 1])
    if len(selected_bars) < 2:
        raise SimilarityError("至少需要两根 K 线")
    try:
        indicators = calculate_indicators(context_bars)[start : end + 1]
    except ValueError as exc:
        raise SimilarityError(str(exc)) from exc

    moving_averages = np.asarray(
        [
            [
                point.ma5 if point.ma5 is not None else bar.close,
                point.ma10 if point.ma10 is not None else bar.close,
                point.ma20 if point.ma20 is not None else bar.close,
            ]
            for bar, point in zip(selected_bars, indicators)
        ],
        dtype=np.float64,
    )
    reference_close = selected_bars[0].close
    moving_average_feature = _standardize(
        np.log(moving_averages / reference_close).reshape(-1)
    )

    volumes = np.asarray(
        [bar.volume for bar in selected_bars],
        dtype=np.float64,
    )
    if np.any(volumes < 0):
        raise SimilarityError("K 线包含无效成交量")
    volume_feature = _standardize(np.log1p(volumes))

    macd_values = np.asarray(
        [
            [
                point.dif / bar.close,
                point.dea / bar.close,
                point.macd / bar.close,
            ]
            for bar, point in zip(selected_bars, indicators)
        ],
        dtype=np.float64,
    )
    return IndicatorFeatures(
        normalize_kline(selected_bars),
        moving_average_feature,
        volume_feature,
        _standardize(macd_values.reshape(-1)),
    )


def _feature_score(target: np.ndarray, candidate: np.ndarray) -> float:
    target_length = float(np.linalg.norm(target))
    candidate_length = float(np.linalg.norm(candidate))
    if target_length < 1e-12 and candidate_length < 1e-12:
        correlation = 1.0
    elif target_length < 1e-12 or candidate_length < 1e-12:
        correlation = 0.0
    else:
        correlation = float(
            np.dot(target, candidate) / (target_length * candidate_length)
        )
    return (float(np.clip(correlation, -1.0, 1.0)) + 1.0) * 50.0


def find_similar_with_indicators(
    target_security: Security,
    target_context: Sequence[KLine],
    target_start: int,
    target_end: int,
    universe: Sequence[tuple[Security, list[KLine]]],
    candidate_date: str,
    top_n: int = 10,
    selected_filters: Sequence[str] | None = None,
) -> list[SimilarityResult]:
    """按选中的 K 线、成交量和 MACD 指标匹配当前市场走势。"""
    filters = (
        SIMILARITY_FILTERS
        if selected_filters is None
        else tuple(dict.fromkeys(selected_filters))
    )
    if not filters:
        raise SimilarityError("请至少选择一项相似度指标")
    unknown_filters = set(filters) - set(SIMILARITY_FILTERS)
    if unknown_filters:
        raise SimilarityError("相似度筛选项无效")

    target_features = build_indicator_features(
        target_context,
        target_start,
        target_end,
    )
    selected_count = target_end - target_start + 1
    results: list[SimilarityResult] = []

    for security, bars in universe:
        if (security.market, security.code) == (
            target_security.market,
            target_security.code,
        ):
            continue
        if len(bars) < selected_count or bars[-1].trade_date != candidate_date:
            continue
        candidate_start = len(bars) - selected_count
        try:
            candidate_features = build_indicator_features(
                bars,
                candidate_start,
                len(bars) - 1,
            )
        except (SimilarityError, ValueError):
            continue

        kline_score = _feature_score(
            target_features.kline,
            candidate_features.kline,
        )
        moving_average_score = _feature_score(
            target_features.moving_average,
            candidate_features.moving_average,
        )
        volume_score = _feature_score(
            target_features.volume,
            candidate_features.volume,
        )
        macd_score = _feature_score(
            target_features.macd,
            candidate_features.macd,
        )
        filter_scores = {
            "kline": kline_score,
            "volume": volume_score,
            "macd": macd_score,
        }
        score = sum(filter_scores[name] for name in filters) / len(filters)
        results.append(
            SimilarityResult(
                security,
                score,
                kline_score,
                moving_average_score,
                volume_score,
                macd_score,
            )
        )

    results.sort(key=lambda item: item.score, reverse=True)
    return results[:top_n]


def find_similar(
    target: tuple[Security, list[KLine]],
    universe: Sequence[tuple[Security, list[KLine]]],
    top_n: int = 10,
) -> list[SimilarityResult]:
    """按归一化 OHLC 相关性返回同一交易日结束的相似股票。"""
    target_security, target_bars = target
    if not target_bars:
        raise SimilarityError("至少需要两根 K 线")
    return find_similar_segment(
        target_security,
        target_bars,
        universe,
        target_bars[-1].trade_date,
        top_n,
    )


def find_similar_segment(
    target_security: Security,
    target_bars: Sequence[KLine],
    universe: Sequence[tuple[Security, list[KLine]]],
    candidate_date: str,
    top_n: int = 10,
) -> list[SimilarityResult]:
    """将历史片段与全市场截至指定日期的最新同长度 K 线比较。"""
    target_feature = normalize_kline(target_bars)
    results: list[SimilarityResult] = []

    for security, bars in universe:
        if (security.market, security.code) == (
            target_security.market,
            target_security.code,
        ):
            continue
        if (
            len(bars) != len(target_bars)
            or bars[-1].trade_date != candidate_date
        ):
            continue
        try:
            candidate = normalize_kline(bars)
        except SimilarityError:
            continue
        correlation = float(np.dot(target_feature, candidate) / target_feature.size)
        correlation = float(np.clip(correlation, -1.0, 1.0))
        results.append(SimilarityResult(security, (correlation + 1.0) * 50.0))

    results.sort(key=lambda item: item.score, reverse=True)
    return results[:top_n]
