from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Callable

from .live_market import (
    EastmoneyLiveMarketSource,
    LiveMarketError,
    LiveMarketSnapshot,
)
from .market_data import (
    MarketDataError,
    TdxMarketDataSource,
    security_from_code,
)
from .models import FavoritePattern, KLine, Security, SimilarityResult
from .similarity import SimilarityError, find_similar_with_indicators
from .storage import KLineStorage


class AgentError(RuntimeError):
    """Agent 无法完成查询。"""


ProgressCallback = Callable[[int, int, int], None]


class KLineAgent:
    """协调行情同步、缓存和 K 线相似度检索。"""

    PERIOD = 20
    CHART_PERIOD = 1300
    # 至少选择 15 根日 K，保证比较区间不少于半个月。
    MIN_SELECTION = 15
    # 允许框选最多 400 根日 K，满足长周期形态匹配需求。
    MAX_SELECTION = 400
    INDICATOR_WARMUP = 35
    FAVORITE_PREVIEW_WARMUP = 120
    # 1300 个交易日约覆盖最近五年行情。
    SYNC_PERIOD = 1300
    RESULT_COUNT = 10
    UNIVERSE_VERSION = "6"

    def __init__(
        self,
        database_path: str | Path = "data/kline_cache.db",
        source: TdxMarketDataSource | None = None,
        live_source: EastmoneyLiveMarketSource | None = None,
    ) -> None:
        self._storage = KLineStorage(database_path)
        self._source = source or TdxMarketDataSource()
        self._live_source = live_source or EastmoneyLiveMarketSource()

    def close(self) -> None:
        self._storage.close()

    def __enter__(self) -> KLineAgent:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def extract_code(query: str) -> str:
        """从中文问题或纯代码中提取六位股票代码。"""
        match = re.search(r"(?<!\d)(\d{6})(?!\d)", query.strip())
        if not match:
            raise AgentError("请输入六位 A 股代码，例如：查找 600519 的相似股票")
        code = match.group(1)
        try:
            security_from_code(code)
        except ValueError as exc:
            raise AgentError(str(exc)) from exc
        return code

    def _sync_all(
        self,
        data_date: str,
        progress: ProgressCallback | None,
    ) -> None:
        securities = self._source.get_securities()
        if not securities:
            raise AgentError("行情接口没有返回 A 股列表")
        self._storage.replace_securities(securities)

        completed_responses = 0
        batch: list[tuple[Security, list[KLine]]] = []
        for security, bars in self._source.iter_bars(
            securities, self.SYNC_PERIOD, progress
        ):
            if bars is None:
                continue
            completed_responses += 1
            if len(bars) >= self.MIN_SELECTION:
                batch.append((security, bars))
            if len(batch) >= 100:
                self._storage.save_bar_batch(batch)
                batch.clear()
        self._storage.save_bar_batch(batch)

        # 大面积请求失败时不标记同步完成，下一次查询会自动继续刷新。
        required = max(1, int(len(securities) * 0.8))
        if completed_responses < required:
            raise AgentError(
                f"行情同步不完整：成功 {completed_responses}/{len(securities)}，"
                "请检查网络后重试"
            )
        self._storage.set_metadata("data_date", data_date)
        self._storage.set_metadata("universe_version", self.UNIVERSE_VERSION)
        self._storage.set_metadata("sync_period", str(self.SYNC_PERIOD))

    @staticmethod
    def _merge_live_bar(
        bars: Sequence[KLine],
        live_bar: KLine | None,
        period: int,
    ) -> tuple[list[KLine], bool]:
        """用实时快照覆盖同日 K 线，且不修改历史缓存。"""
        merged = list(bars)
        if live_bar is None:
            return merged[-period:], False
        if merged and live_bar.trade_date < merged[-1].trade_date:
            return merged[-period:], False
        if merged and live_bar.trade_date == merged[-1].trade_date:
            merged[-1] = live_bar
        else:
            merged.append(live_bar)
        return merged[-period:], True

    def refresh_live_market(
        self,
        progress: ProgressCallback | None = None,
    ) -> LiveMarketSnapshot:
        """刷新并单独保存全市场实时日 K 快照。"""
        securities = self._storage.load_securities()
        if not securities:
            try:
                securities = self._source.get_securities()
            except MarketDataError as exc:
                raise AgentError(str(exc)) from exc
            if not securities:
                raise AgentError("行情接口没有返回 A 股列表")
            self._storage.replace_securities(securities)

        try:
            snapshot = self._live_source.fetch(securities, progress)
        except LiveMarketError as exc:
            raise AgentError(str(exc)) from exc
        self._storage.replace_live_snapshot(
            snapshot.entries,
            snapshot.market_date,
            snapshot.updated_at.isoformat(timespec="seconds"),
        )
        return snapshot

    def _overlay_live_universe(
        self,
        universe: Sequence[tuple[Security, list[KLine]]],
        period: int,
        historical_date: str,
    ) -> tuple[list[tuple[Security, list[KLine]]], str, bool]:
        """在内存中把有效实时快照覆盖到候选 K 线末端。"""
        live_date = self._storage.get_metadata("live_market_date") or ""
        live_bars = self._storage.load_live_bars()
        if not live_bars or live_date < historical_date:
            return list(universe), historical_date, False

        overlaid: list[tuple[Security, list[KLine]]] = []
        applied = False
        for security, bars in universe:
            merged, merged_live = self._merge_live_bar(
                bars,
                live_bars.get((security.market, security.code)),
                period,
            )
            overlaid.append((security, merged))
            applied = applied or merged_live
        return overlaid, live_date, applied

    def load_chart(
        self,
        query: str,
    ) -> tuple[Security, list[KLine], bool]:
        """读取用于图表展示的目标股票历史日 K。"""
        code = self.extract_code(query)
        requested = security_from_code(code)
        try:
            bars = self._source.get_bars(requested, self.CHART_PERIOD)
        except MarketDataError as exc:
            cached = self._storage.load_security_bars(code, self.CHART_PERIOD)
            if cached is None:
                raise AgentError(str(exc)) from exc
            merged_bars, used_live = self._merge_live_bar(
                cached[1],
                self._storage.load_live_bar(code),
                self.CHART_PERIOD,
            )
            if len(merged_bars) < self.MIN_SELECTION:
                raise AgentError(str(exc)) from exc
            return cached[0], merged_bars, not used_live

        merged_bars, _used_live = self._merge_live_bar(
            bars,
            self._storage.load_live_bar(code),
            self.CHART_PERIOD,
        )
        if len(merged_bars) < self.MIN_SELECTION:
            cached = self._storage.load_security_bars(code, self.CHART_PERIOD)
            if cached is not None:
                cached_bars, cached_live = self._merge_live_bar(
                    cached[1],
                    self._storage.load_live_bar(code),
                    self.CHART_PERIOD,
                )
                if len(cached_bars) >= self.MIN_SELECTION:
                    return cached[0], cached_bars, not cached_live
            raise AgentError(
                f"{code} 不足 {self.MIN_SELECTION} 根有效日 K 线"
            )
        security = self._storage.get_security(code) or requested
        return security, merged_bars, False

    def _prepare_selected_context(
        self,
        selected_bars: Sequence[KLine],
        context_bars: Sequence[KLine] | None,
        warmup_limit: int | None = None,
    ) -> tuple[list[KLine], int, int]:
        """校验框选区间并保留计算指标所需的前置 K 线。"""
        target_bars = list(selected_bars)
        period = len(target_bars)
        if not self.MIN_SELECTION <= period <= self.MAX_SELECTION:
            raise AgentError(
                f"请选择 {self.MIN_SELECTION} 至 "
                f"{self.MAX_SELECTION} 根连续日 K"
            )
        if any(
            first.trade_date >= second.trade_date
            for first, second in zip(target_bars, target_bars[1:])
        ):
            raise AgentError("选择的 K 线日期顺序无效")

        full_target_context = list(context_bars or target_bars)
        selected_dates = [bar.trade_date for bar in target_bars]
        context_dates = [bar.trade_date for bar in full_target_context]
        try:
            selected_start = context_dates.index(selected_dates[0])
        except ValueError as exc:
            raise AgentError("选择的 K 线不在目标图表中") from exc
        selected_end = selected_start + period - 1
        if context_dates[selected_start : selected_end + 1] != selected_dates:
            raise AgentError("选择的 K 线不是连续图表区间")

        # 搜索保留算法预热数据，收藏额外保留 MA120 预览数据。
        resolved_warmup_limit = (
            self.INDICATOR_WARMUP
            if warmup_limit is None
            else warmup_limit
        )
        warmup_count = min(resolved_warmup_limit, selected_start)
        target_context = full_target_context[
            selected_start - warmup_count : selected_end + 1
        ]
        target_start = warmup_count
        target_end = target_start + period - 1
        return target_context, target_start, target_end

    def save_favorite_pattern(
        self,
        target_security: Security,
        selected_bars: Sequence[KLine],
        context_bars: Sequence[KLine] | None = None,
    ) -> FavoritePattern:
        """保存当前框选形态，供以后直接发起相似搜索。"""
        target_context, target_start, target_end = (
            self._prepare_selected_context(
                selected_bars,
                context_bars,
                self.FAVORITE_PREVIEW_WARMUP,
            )
        )
        selected = target_context[target_start : target_end + 1]
        name = (
            f"{target_security.name} "
            f"{selected[0].trade_date} 至 {selected[-1].trade_date}"
        )
        favorite_id = self._storage.save_favorite_pattern(
            name,
            target_security,
            target_context,
            target_start,
            len(selected),
            datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        favorite = self._storage.load_favorite_pattern(favorite_id)
        if favorite is None:
            raise AgentError("收藏保存后无法读取")
        return favorite

    def list_favorite_patterns(self) -> list[FavoritePattern]:
        """读取收藏夹中的全部 K 线形态。"""
        return self._storage.load_favorite_patterns()

    def delete_favorite_pattern(self, favorite_id: int) -> None:
        """删除指定 K 线收藏。"""
        if favorite_id <= 0:
            raise AgentError("收藏编号无效")
        if not self._storage.delete_favorite_pattern(favorite_id):
            raise AgentError("该收藏不存在或已经删除")

    def search_favorite_pattern(
        self,
        favorite_id: int,
        progress: ProgressCallback | None = None,
        selected_filters: Sequence[str] | None = None,
    ) -> tuple[
        FavoritePattern,
        Security,
        str,
        list[SimilarityResult],
        bool,
    ]:
        """读取收藏形态并直接与全市场最新走势比较。"""
        favorite = self._storage.load_favorite_pattern(favorite_id)
        if favorite is None:
            raise AgentError("该收藏不存在或已经删除")
        if len(favorite.selected_bars) != favorite.selection_count:
            raise AgentError("收藏的 K 线数据不完整")
        result = self.search_segment(
            favorite.security,
            favorite.selected_bars,
            progress,
            context_bars=favorite.context_bars,
            selected_filters=selected_filters,
        )
        return favorite, *result

    def search_segment(
        self,
        target_security: Security,
        selected_bars: Sequence[KLine],
        progress: ProgressCallback | None = None,
        context_bars: Sequence[KLine] | None = None,
        selected_filters: Sequence[str] | None = None,
    ) -> tuple[Security, str, list[SimilarityResult], bool]:
        """按所选指标比较历史片段与全市场当前走势。"""
        target_context, target_start, target_end = (
            self._prepare_selected_context(selected_bars, context_bars)
        )
        period = len(selected_bars)
        warmup_count = target_start

        used_offline_cache = False
        try:
            live_bars = self._source.get_bars(target_security, 1)
            if not live_bars:
                raise MarketDataError("行情接口没有返回目标股票最新日 K")
            live_date = live_bars[-1].trade_date
            try:
                cached_period = int(
                    self._storage.get_metadata("sync_period") or "0"
                )
            except ValueError:
                cached_period = 0
            if (
                self._storage.security_count() == 0
                or self._storage.get_metadata("data_date") != live_date
                or self._storage.get_metadata("universe_version")
                != self.UNIVERSE_VERSION
                or cached_period < self.SYNC_PERIOD
            ):
                self._sync_all(live_date, progress)
        except MarketDataError as exc:
            live_date = self._storage.get_metadata("data_date") or ""
            if (
                self._storage.security_count() == 0
                or not live_date
                or self._storage.get_metadata("universe_version")
                != self.UNIVERSE_VERSION
            ):
                raise AgentError(str(exc)) from exc
            used_offline_cache = True

        context_period = period + warmup_count
        universe = self._storage.load_universe(context_period)
        universe, live_date, used_live = self._overlay_live_universe(
            universe,
            context_period,
            live_date,
        )
        if used_live:
            used_offline_cache = False
        resolved_target = (
            self._storage.get_security(target_security.code)
            or target_security
        )
        try:
            results = find_similar_with_indicators(
                resolved_target,
                target_context,
                target_start,
                target_end,
                universe,
                live_date,
                self.RESULT_COUNT,
                selected_filters=selected_filters,
            )
        except SimilarityError as exc:
            raise AgentError(
                f"{target_security.code} 无法计算相似度：{exc}"
            ) from exc
        if not results:
            raise AgentError("没有找到最新日期一致且数据完整的候选股票")
        return resolved_target, live_date, results, used_offline_cache

    def search(
        self,
        query: str,
        progress: ProgressCallback | None = None,
    ) -> tuple[Security, str, list[SimilarityResult], bool]:
        """保留命令行查询：默认比较目标股票最新 20 根日 K。"""
        target, bars, chart_offline = self.load_chart(query)
        if len(bars) < self.PERIOD:
            raise AgentError(f"{target.code} 不足 {self.PERIOD} 根有效日 K 线")
        resolved, data_date, results, search_offline = self.search_segment(
            target,
            bars[-self.PERIOD :],
            progress,
            context_bars=bars,
        )
        return (
            resolved,
            data_date,
            results,
            chart_offline or search_offline,
        )
