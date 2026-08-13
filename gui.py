from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

from kline_agent.indicators import IndicatorPoint, calculate_indicators
from kline_agent.live_market import LiveMarketSnapshot
from kline_agent.models import FavoritePattern, KLine, Security, SimilarityResult
from kline_agent.service import AgentError, KLineAgent


# 统一界面颜色，确保主窗口和弹窗保持一致。
APP_BACKGROUND = "#f3f6fb"
SURFACE = "#ffffff"
TEXT = "#172033"
MUTED_TEXT = "#667085"
BORDER = "#d8e0eb"
PRIMARY = "#2563eb"
PRIMARY_HOVER = "#1d4ed8"
SECONDARY_BACKGROUND = "#eaf0f8"
ROW_ALTERNATE = "#f7faff"


def database_path() -> Path:
    """返回源码运行或打包运行时对应的可写缓存路径。"""
    if getattr(sys, "frozen", False):
        application_directory = Path(sys.executable).resolve().parent
    else:
        application_directory = Path(__file__).resolve().parent
    return application_directory / "data" / "kline_cache.db"


def resource_path(relative_path: str) -> Path:
    """返回源码目录或单文件程序临时目录中的资源路径。"""
    bundle_directory = getattr(sys, "_MEIPASS", None)
    if bundle_directory:
        return Path(bundle_directory) / relative_path
    return Path(__file__).resolve().parent / relative_path


class KLineChart(tk.Canvas):
    """使用 tkinter 绘制并选择日 K 线片段。"""

    MIN_VISIBLE_BARS = 30
    LEFT_MARGIN = 68
    RIGHT_MARGIN = 20
    TOP_MARGIN = 18
    BOTTOM_MARGIN = 42
    PANEL_GAP = 20

    def __init__(
        self,
        parent: tk.Misc,
        selection_callback: Callable[[int | None, int | None], None],
        view_callback: Callable[[int, int], None],
        *,
        interactive: bool = True,
        empty_text: str = "输入股票代码并加载 K 线",
    ) -> None:
        super().__init__(
            parent,
            background=SURFACE,
            height=480,
            highlightthickness=1,
            highlightbackground=BORDER,
            cursor="crosshair" if interactive else "arrow",
        )
        self._bars: list[KLine] = []
        self._indicators: list[IndicatorPoint] = []
        self._view_start = 0
        self._view_end = 0
        self._selection: tuple[int, int] | None = None
        self._drag_anchor: int | None = None
        self._selection_callback = selection_callback
        self._view_callback = view_callback
        self._empty_text = empty_text

        self.bind("<Configure>", self._redraw)
        if interactive:
            self.bind("<ButtonPress-1>", self._start_selection)
            self.bind("<B1-Motion>", self._move_selection)
            self.bind("<ButtonRelease-1>", self._finish_selection)

    def set_bars(self, bars: list[KLine]) -> None:
        self._bars = list(bars)
        # 指标只在数据变化时计算，拖拽和缩放直接复用结果。
        self._indicators = calculate_indicators(self._bars)
        self._view_start = 0
        self._view_end = len(self._bars)
        self._selection = None
        self._drag_anchor = None
        self._selection_callback(None, None)
        self._notify_view_changed()
        self._redraw()

    def clear(self) -> None:
        self.set_bars([])

    def selected_bars(self) -> list[KLine]:
        if self._selection is None:
            return []
        start, end = self._selection
        return self._bars[start : end + 1]

    def select_range(self, start: int, end: int) -> bool:
        """按索引恢复一个已保存的 K 线框选区间。"""
        if (
            not self._bars
            or start < 0
            or end < start
            or end >= len(self._bars)
        ):
            return False
        self._selection = (start, end)
        self._drag_anchor = None
        self._selection_callback(start, end)
        self._draw_selection()
        return True

    def clear_selection(self) -> None:
        """清除用于相似匹配的主图选中片段。"""
        if self._selection is None:
            return
        self._selection = None
        self._drag_anchor = None
        self._selection_callback(None, None)
        self.delete("selection")

    def visible_range(self) -> tuple[int, int]:
        """返回当前可见区间，结束位置不包含在区间内。"""
        return self._view_start, self._view_end

    def set_visible_range(self, start: int, end: int) -> bool:
        """按时间轴选择结果设置主图可见区间。"""
        if not self._bars:
            return False
        view_start = min(max(start, 0), len(self._bars) - 1)
        view_end = min(max(end, view_start + 1), len(self._bars))
        changed = (
            view_start != self._view_start
            or view_end != self._view_end
        )
        self._view_start = view_start
        self._view_end = view_end
        self._redraw()
        self._notify_view_changed()
        return changed

    def can_zoom_in(self) -> bool:
        if not self._bars:
            return False
        minimum = self.MIN_VISIBLE_BARS
        if self._selection is not None:
            start, end = self._selection
            minimum = end - start + 1
        return self._view_end - self._view_start > minimum

    def can_zoom_out(self) -> bool:
        return bool(
            self._bars
            and self._view_end - self._view_start < len(self._bars)
        )

    def zoom_in(self) -> bool:
        """缩小可见时间范围，放大单根 K 线。"""
        if not self.can_zoom_in():
            return False
        current_count = self._view_end - self._view_start
        minimum = self.MIN_VISIBLE_BARS
        if self._selection is not None:
            start, end = self._selection
            minimum = end - start + 1
        new_count = max(minimum, (current_count + 1) // 2)
        self._apply_zoom(new_count)
        return True

    def zoom_out(self) -> bool:
        """扩大可见时间范围，缩小单根 K 线。"""
        if not self.can_zoom_out():
            return False
        current_count = self._view_end - self._view_start
        self._apply_zoom(min(len(self._bars), current_count * 2))
        return True

    def zoom_to_selection(self) -> bool:
        """放大选中区间，并保留少量左右边距。"""
        if self._selection is None:
            return False
        start, end = self._selection
        selected_count = end - start + 1
        padding = max(2, round(selected_count * 0.1))
        view_start = max(0, start - padding)
        view_end = min(len(self._bars), end + 1 + padding)
        changed = (
            view_start != self._view_start
            or view_end != self._view_end
        )
        self._view_start = view_start
        self._view_end = view_end
        self._redraw()
        self._notify_view_changed()
        return changed

    def _apply_zoom(self, new_count: int) -> None:
        if self._selection is None:
            focus = self._view_end - 1
        else:
            start, end = self._selection
            focus = (start + end) / 2
        view_start = round(focus - (new_count - 1) / 2)
        self._view_start = min(
            max(view_start, 0),
            len(self._bars) - new_count,
        )
        self._view_end = self._view_start + new_count
        self._redraw()
        self._notify_view_changed()

    def _notify_view_changed(self) -> None:
        self._view_callback(self._view_start, self._view_end)

    def _panel_bounds(
        self,
    ) -> tuple[float, float, float, float, float, float, float, float]:
        width = max(self.winfo_width(), 200)
        height = max(self.winfo_height(), 420)
        content_height = height - self.TOP_MARGIN - self.BOTTOM_MARGIN
        panel_height = content_height - self.PANEL_GAP * 2
        price_bottom = self.TOP_MARGIN + panel_height * 0.55
        volume_top = price_bottom + self.PANEL_GAP
        volume_bottom = volume_top + panel_height * 0.17
        macd_top = volume_bottom + self.PANEL_GAP
        return (
            self.LEFT_MARGIN,
            width - self.RIGHT_MARGIN,
            self.TOP_MARGIN,
            price_bottom,
            volume_top,
            volume_bottom,
            macd_top,
            height - self.BOTTOM_MARGIN,
        )

    def _index_at(self, x: float) -> int | None:
        if not self._bars:
            return None
        left, right, *_ = self._panel_bounds()
        if right <= left:
            return None
        position = (x - left) / (right - left)
        visible_count = self._view_end - self._view_start
        local_index = int(position * visible_count)
        local_index = min(max(local_index, 0), visible_count - 1)
        return self._view_start + local_index

    def _start_selection(self, event: tk.Event[tk.Misc]) -> None:
        index = self._index_at(event.x)
        if index is None:
            return
        self._drag_anchor = index
        self._selection = (index, index)
        self._selection_callback(index, index)
        self._draw_selection()

    def _move_selection(self, event: tk.Event[tk.Misc]) -> None:
        if self._drag_anchor is None:
            return
        index = self._index_at(event.x)
        if index is None:
            return
        selection = tuple(
            sorted((self._drag_anchor, index))
        )
        if selection == self._selection:
            return
        self._selection = selection
        self._selection_callback(*self._selection)
        self._draw_selection()

    def _finish_selection(self, event: tk.Event[tk.Misc]) -> None:
        self._move_selection(event)
        self._drag_anchor = None

    def _redraw(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        self.delete("all")
        if not self._bars:
            self.create_text(
                max(self.winfo_width(), 200) / 2,
                max(self.winfo_height(), 160) / 2,
                text=self._empty_text,
                fill="#737b86",
                font=("Microsoft YaHei UI", 11),
            )
            return

        (
            left,
            right,
            price_top,
            price_bottom,
            volume_top,
            volume_bottom,
            macd_top,
            macd_bottom,
        ) = self._panel_bounds()
        plot_width = right - left
        price_height = price_bottom - price_top
        # 使用明显的间隔带和边框分隔价格、成交量与 MACD。
        for upper_bottom, lower_top in (
            (price_bottom, volume_top),
            (volume_bottom, macd_top),
        ):
            separator_y = (upper_bottom + lower_top) / 2
            self.create_rectangle(
                left,
                upper_bottom + 1,
                right,
                lower_top - 1,
                outline="",
                fill="#eef1f5",
            )
            self.create_line(
                left,
                separator_y,
                right,
                separator_y,
                fill="#8d98a6",
                width=2,
            )
        for panel_top, panel_bottom in (
            (price_top, price_bottom),
            (volume_top, volume_bottom),
            (macd_top, macd_bottom),
        ):
            self.create_rectangle(
                left,
                panel_top,
                right,
                panel_bottom,
                outline="#b8c0ca",
                width=1,
            )
        visible_bars = self._bars[self._view_start : self._view_end]
        # 指标使用完整历史计算，缩放后只显示当前区间。
        indicators = self._indicators[
            self._view_start : self._view_end
        ]
        moving_average_values = [
            value
            for point in indicators
            for value in (
                point.ma5,
                point.ma10,
                point.ma20,
                point.ma30,
                point.ma60,
                point.ma120,
            )
            if value is not None
        ]
        low_price = min(
            [bar.low for bar in visible_bars] + moving_average_values
        )
        high_price = max(
            [bar.high for bar in visible_bars] + moving_average_values
        )
        price_span = high_price - low_price
        if price_span <= 0:
            price_span = max(high_price * 0.01, 0.01)
        price_padding = price_span * 0.06
        low_price -= price_padding
        high_price += price_padding
        price_span = high_price - low_price

        def price_y(price: float) -> float:
            return (
                price_top
                + (high_price - price) / price_span * price_height
            )

        for line_index in range(4):
            ratio = line_index / 3
            y = price_top + ratio * price_height
            price = high_price - ratio * price_span
            self.create_line(left, y, right, y, fill="#e7e9ed")
            self.create_text(
                left - 8,
                y,
                text=f"{price:.2f}",
                anchor="e",
                fill="#59616c",
                font=("Microsoft YaHei UI", 8),
            )

        step = plot_width / len(visible_bars)
        candle_width = max(2.0, min(8.0, step * 0.65))
        for index, bar in enumerate(visible_bars):
            x = left + (index + 0.5) * step
            color = "#d84a4a" if bar.close >= bar.open else "#239064"
            self.create_line(
                x,
                price_y(bar.high),
                x,
                price_y(bar.low),
                fill=color,
            )
            body_top = price_y(max(bar.open, bar.close))
            body_bottom = price_y(min(bar.open, bar.close))
            if body_bottom - body_top < 1:
                body_bottom = body_top + 1
            self.create_rectangle(
                x - candle_width / 2,
                body_top,
                x + candle_width / 2,
                body_bottom,
                outline=color,
                fill=color,
            )

        def draw_series(
            values: list[float | None],
            converter: Callable[[float], float],
            color: str,
            width: int = 1,
            smooth: bool = True,
        ) -> None:
            points: list[float] = []
            for index, value in enumerate(values):
                if value is None:
                    if len(points) >= 4:
                        self.create_line(
                            *points,
                            fill=color,
                            width=width,
                            smooth=smooth,
                        )
                    points = []
                    continue
                points.extend(
                    [
                        left + (index + 0.5) * step,
                        converter(value),
                    ]
                )
            if len(points) >= 4:
                self.create_line(
                    *points,
                    fill=color,
                    width=width,
                    smooth=smooth,
                )

        ma_series = (
            ("MA5", [point.ma5 for point in indicators], "#e2a600"),
            ("MA10", [point.ma10 for point in indicators], "#4e79c7"),
            ("MA20", [point.ma20 for point in indicators], "#a35fb5"),
            ("MA30", [point.ma30 for point in indicators], "#f28e2b"),
            ("MA60", [point.ma60 for point in indicators], "#2b9f78"),
            ("MA120", [point.ma120 for point in indicators], "#6b5b95"),
        )
        for _label, values, color in ma_series:
            draw_series(values, price_y, color, 2)
        for index, (label, _values, color) in enumerate(ma_series):
            self.create_text(
                left + index * 64,
                price_top + 3,
                text=label,
                anchor="nw",
                fill=color,
                font=("Microsoft YaHei UI", 8, "bold"),
            )

        max_volume = max((bar.volume for bar in visible_bars), default=0.0)
        volume_span = max(max_volume, 1.0)
        volume_height = volume_bottom - volume_top
        for index, bar in enumerate(visible_bars):
            x = left + (index + 0.5) * step
            y = volume_bottom - bar.volume / volume_span * volume_height
            color = "#d84a4a" if bar.close >= bar.open else "#239064"
            self.create_rectangle(
                x - candle_width / 2,
                y,
                x + candle_width / 2,
                volume_bottom,
                outline=color,
                fill=color,
            )
        self.create_text(
            left,
            volume_top + 2,
            text=f"成交量  最大 {max_volume:,.0f}",
            anchor="nw",
            fill="#59616c",
            font=("Microsoft YaHei UI", 8),
        )

        macd_values = [
            value
            for point in indicators
            for value in (point.dif, point.dea, point.macd)
        ]
        macd_low = min(min(macd_values), 0.0)
        macd_high = max(max(macd_values), 0.0)
        macd_span = macd_high - macd_low
        if macd_span < 1e-12:
            macd_span = 1.0
            macd_low = -0.5
            macd_high = 0.5
        macd_padding = macd_span * 0.08
        macd_low -= macd_padding
        macd_high += macd_padding
        macd_span = macd_high - macd_low
        macd_height = macd_bottom - macd_top

        def macd_y(value: float) -> float:
            y = (
                macd_top
                + (macd_high - value) / macd_span * macd_height
            )
            return min(max(y, macd_top), macd_bottom)

        zero_y = macd_y(0.0)
        self.create_line(left, zero_y, right, zero_y, fill="#b8bdc5")
        for index, point in enumerate(indicators):
            x = left + (index + 0.5) * step
            y = macd_y(point.macd)
            color = "#d84a4a" if point.macd >= 0 else "#239064"
            self.create_rectangle(
                x - candle_width / 2,
                min(y, zero_y),
                x + candle_width / 2,
                max(y, zero_y),
                outline=color,
                fill=color,
            )
        draw_series(
            [point.dif for point in indicators],
            macd_y,
            "#e2a600",
            2,
            smooth=False,
        )
        draw_series(
            [point.dea for point in indicators],
            macd_y,
            "#4e79c7",
            2,
            smooth=False,
        )
        self.create_text(
            left,
            macd_top + 2,
            text="MACD(12,26,9)  DIF  DEA",
            anchor="nw",
            fill="#59616c",
            font=("Microsoft YaHei UI", 8),
        )

        label_indexes = {
            round(index * (len(visible_bars) - 1) / 4)
            for index in range(5)
        }
        for index in sorted(label_indexes):
            x = left + (index + 0.5) * step
            self.create_text(
                x,
                macd_bottom + 18,
                text=visible_bars[index].trade_date,
                fill="#59616c",
                font=("Microsoft YaHei UI", 8),
            )

        self._draw_selection()

    def _draw_selection(self) -> None:
        """仅刷新选区覆盖层，避免拖拽时重绘全部图表。"""
        self.delete("selection")
        if self._selection is None or not self._bars:
            return
        left, right, price_top, _, _, _, _, macd_bottom = (
            self._panel_bounds()
        )
        visible_count = self._view_end - self._view_start
        if visible_count <= 0:
            return
        start, end = self._selection
        visible_start = max(start, self._view_start)
        visible_end = min(end, self._view_end - 1)
        if visible_start > visible_end:
            return
        step = (right - left) / visible_count
        selection_left = left + (visible_start - self._view_start) * step
        selection_right = (
            left + (visible_end - self._view_start + 1) * step
        )
        selected_visible_count = visible_end - visible_start + 1
        selection_options: dict[str, Any] = {}
        if selected_visible_count / visible_count < 0.5:
            selection_options = {
                "fill": "#7ea9df",
                "stipple": "gray50",
            }
        self.create_rectangle(
            selection_left,
            price_top,
            selection_right,
            macd_bottom,
            outline="#2563eb",
            width=2,
            tags=("selection",),
            **selection_options,
        )


class TimeRangeSelector(tk.Canvas):
    """显示全部历史数据，并通过拖拽控制主图时间范围。"""

    MIN_RANGE = 5
    LEFT_MARGIN = 68
    RIGHT_MARGIN = 20
    TOP_MARGIN = 22
    BOTTOM_MARGIN = 22

    def __init__(
        self,
        parent: tk.Misc,
        range_callback: Callable[[int, int], None],
    ) -> None:
        super().__init__(
            parent,
            background="#f8faff",
            height=82,
            highlightthickness=1,
            highlightbackground=BORDER,
            cursor="crosshair",
            state="disabled",
        )
        self._bars: list[KLine] = []
        self._view_start = 0
        self._view_end = 0
        self._drag_anchor: int | None = None
        self._drag_range: tuple[int, int] | None = None
        self._range_callback = range_callback

        self.bind("<Configure>", self._redraw)
        self.bind("<ButtonPress-1>", self._start_range)
        self.bind("<B1-Motion>", self._move_range)
        self.bind("<ButtonRelease-1>", self._finish_range)

    def set_bars(self, bars: list[KLine]) -> None:
        self._bars = list(bars)
        self._view_start = 0
        self._view_end = len(self._bars)
        self._drag_anchor = None
        self._drag_range = None
        self._redraw()

    def clear(self) -> None:
        self.set_bars([])

    def set_view(self, start: int, end: int) -> None:
        if not self._bars:
            self._view_start = 0
            self._view_end = 0
        else:
            self._view_start = min(
                max(start, 0),
                len(self._bars) - 1,
            )
            self._view_end = min(
                max(end, self._view_start + 1),
                len(self._bars),
            )
        self._redraw()

    def _index_at(self, x: float) -> int | None:
        if not self._bars:
            return None
        width = max(self.winfo_width(), 200)
        plot_width = width - self.LEFT_MARGIN - self.RIGHT_MARGIN
        if plot_width <= 0:
            return None
        position = (x - self.LEFT_MARGIN) / plot_width
        index = int(position * len(self._bars))
        return min(max(index, 0), len(self._bars) - 1)

    def _start_range(self, event: tk.Event[tk.Misc]) -> None:
        index = self._index_at(event.x)
        if index is None:
            return
        self._drag_anchor = index
        self._drag_range = (index, index)
        self._redraw()

    def _move_range(self, event: tk.Event[tk.Misc]) -> None:
        if self._drag_anchor is None:
            return
        index = self._index_at(event.x)
        if index is None:
            return
        self._drag_range = tuple(sorted((self._drag_anchor, index)))
        self._redraw()

    def _finish_range(self, event: tk.Event[tk.Misc]) -> None:
        self._move_range(event)
        self._drag_anchor = None
        if self._drag_range is None:
            return
        start, end = self._drag_range
        minimum = min(self.MIN_RANGE, len(self._bars))
        if end - start + 1 < minimum:
            end = min(len(self._bars) - 1, start + minimum - 1)
            start = max(0, end - minimum + 1)
        self._drag_range = None
        self._view_start = start
        self._view_end = end + 1
        self._redraw()
        self._range_callback(self._view_start, self._view_end)

    def _redraw(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 200)
        height = max(self.winfo_height(), 82)
        if not self._bars:
            self.create_text(
                width / 2,
                height / 2,
                text="加载 K 线后可在时间轴上拖拽选择放大范围",
                fill="#737b86",
                font=("Microsoft YaHei UI", 9),
            )
            return

        left = self.LEFT_MARGIN
        right = width - self.RIGHT_MARGIN
        top = self.TOP_MARGIN
        bottom = height - self.BOTTOM_MARGIN
        closes = [bar.close for bar in self._bars]
        low = min(closes)
        high = max(closes)
        span = high - low
        if span <= 0:
            span = max(high * 0.01, 0.01)
        step = (right - left) / len(self._bars)

        points: list[float] = []
        for index, close in enumerate(closes):
            x = left + (index + 0.5) * step
            y = top + (high - close) / span * (bottom - top)
            points.extend((x, y))
        if len(points) >= 4:
            self.create_line(
                *points,
                fill="#526d96",
                width=1,
            )

        if self._drag_range is None:
            range_start = self._view_start
            range_end = self._view_end
        else:
            range_start = self._drag_range[0]
            range_end = self._drag_range[1] + 1
        range_left = left + range_start * step
        range_right = left + range_end * step
        range_options: dict[str, Any] = {}
        if range_end - range_start < len(self._bars):
            range_options = {
                "fill": "#8fb8e8",
                "stipple": "gray50",
            }
        self.create_rectangle(
            range_left,
            top,
            range_right,
            bottom,
            outline="#2d6fc2",
            width=2,
            **range_options,
        )
        self.create_text(
            left,
            4,
            text="时间轴：拖拽选择要放大的日期范围",
            anchor="nw",
            fill="#59616c",
            font=("Microsoft YaHei UI", 8, "bold"),
        )
        self.create_text(
            right,
            4,
            text=(
                f"{self._bars[range_start].trade_date} 至 "
                f"{self._bars[range_end - 1].trade_date}，"
                f"{range_end - range_start} 根"
            ),
            anchor="ne",
            fill="#59616c",
            font=("Microsoft YaHei UI", 8),
        )
        self.create_text(
            left,
            height - 4,
            text=self._bars[0].trade_date,
            anchor="sw",
            fill="#737b86",
            font=("Microsoft YaHei UI", 8),
        )
        self.create_text(
            right,
            height - 4,
            text=self._bars[-1].trade_date,
            anchor="se",
            fill="#737b86",
            font=("Microsoft YaHei UI", 8),
        )


class KLineAgentWindow:
    """A 股 K 线相似股票查询窗口。"""

    RESULT_CHART_PERIOD = 150
    AUTO_REFRESH_INTERVAL = 5 * 60 * 1000
    AUTO_REFRESH_RETRY_INTERVAL = 30 * 1000

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._operation: str | None = None
        self._auto_refresh_id: str | None = None
        self._favorites_window: tk.Toplevel | None = None
        self._target: Security | None = None
        self._bars: list[KLine] = []
        self._result_securities: dict[str, Security] = {}

        self._query = tk.StringVar()
        self._status = tk.StringVar(value="请输入一只 A 股代码")
        self._market_status = tk.StringVar(value="实时行情：等待更新")
        self._summary = tk.StringVar(value="尚未加载 K 线")
        self._selection_text = tk.StringVar(
            value="加载后，在图表上按住鼠标左键拖拽选择一段 K 线"
        )
        self._progress = tk.DoubleVar(value=0)
        self._filter_kline = tk.BooleanVar(value=True)
        self._filter_volume = tk.BooleanVar(value=True)
        self._filter_macd = tk.BooleanVar(value=True)
        self._last_filters = ("kline", "volume", "macd")

        self._configure_window()
        self._build_widgets()
        self._root.after(100, self._process_messages)
        self._schedule_auto_refresh()

    def _configure_window(self) -> None:
        self._root.title("K 线相似股票查询")
        self._root.geometry("1440x1050")
        self._root.minsize(1100, 800)
        self._root.configure(background=APP_BACKGROUND)
        self._root.option_add("*Font", ("Microsoft YaHei UI", 10))
        icon_path = resource_path("assets/kline.ico")
        if icon_path.exists():
            try:
                # 设置默认图标，使主窗口和股票详情窗口保持一致。
                self._root.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass

        style = ttk.Style(self._root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(
            ".",
            background=APP_BACKGROUND,
            foreground=TEXT,
            font=("Microsoft YaHei UI", 10),
        )
        style.configure("App.TFrame", background=APP_BACKGROUND)
        style.configure(
            "Card.TFrame",
            background=SURFACE,
            bordercolor=BORDER,
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "Card.TLabel",
            background=SURFACE,
            foreground=TEXT,
        )
        style.configure(
            "CardMuted.TLabel",
            background=SURFACE,
            foreground=MUTED_TEXT,
        )
        style.configure(
            "Card.TCheckbutton",
            background=SURFACE,
            foreground=TEXT,
        )
        style.map(
            "Card.TCheckbutton",
            background=[("active", SURFACE)],
            foreground=[("disabled", "#9aa4b2")],
        )
        style.configure(
            "Summary.TLabel",
            background=APP_BACKGROUND,
            foreground=TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.configure(
            "Muted.TLabel",
            background=APP_BACKGROUND,
            foreground=MUTED_TEXT,
        )
        style.configure(
            "PopupTitle.TLabel",
            background=APP_BACKGROUND,
            foreground=TEXT,
            font=("Microsoft YaHei UI", 15, "bold"),
        )
        style.configure(
            "TLabelframe",
            background=SURFACE,
            bordercolor=BORDER,
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "TLabelframe.Label",
            background=SURFACE,
            foreground=TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.configure(
            "TEntry",
            fieldbackground=SURFACE,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=8,
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", PRIMARY)],
            lightcolor=[("focus", PRIMARY)],
            darkcolor=[("focus", PRIMARY)],
        )
        style.configure(
            "Primary.TButton",
            background=PRIMARY,
            foreground=SURFACE,
            borderwidth=0,
            focuscolor=PRIMARY,
            padding=(16, 8),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[
                ("disabled", "#b8c6da"),
                ("pressed", PRIMARY_HOVER),
                ("active", PRIMARY_HOVER),
            ],
            foreground=[("disabled", "#eef2f7")],
        )
        style.configure(
            "Secondary.TButton",
            background=SECONDARY_BACKGROUND,
            foreground=TEXT,
            bordercolor=BORDER,
            borderwidth=1,
            focuscolor=SECONDARY_BACKGROUND,
            padding=(14, 7),
        )
        style.map(
            "Secondary.TButton",
            background=[
                ("disabled", "#eef1f5"),
                ("pressed", "#dce6f3"),
                ("active", "#dce6f3"),
            ],
            foreground=[("disabled", "#9aa4b2")],
        )
        style.configure(
            "Treeview",
            background=SURFACE,
            fieldbackground=SURFACE,
            foreground=TEXT,
            borderwidth=0,
            relief="flat",
            rowheight=27,
        )
        style.map(
            "Treeview",
            background=[("selected", "#dbeafe")],
            foreground=[("selected", "#123a73")],
        )
        style.configure(
            "Treeview.Heading",
            background="#eaf0f8",
            foreground=TEXT,
            bordercolor=BORDER,
            borderwidth=1,
            relief="flat",
            padding=(8, 6),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map(
            "Treeview.Heading",
            background=[("active", "#dce6f3")],
        )
        style.configure(
            "TProgressbar",
            background=PRIMARY,
            troughcolor="#e4eaf2",
            bordercolor="#e4eaf2",
            lightcolor=PRIMARY,
            darkcolor=PRIMARY,
        )

    def _build_widgets(self) -> None:
        container = ttk.Frame(
            self._root,
            padding=14,
            style="App.TFrame",
        )
        container.grid(row=0, column=0, sticky="nsew")
        self._root.rowconfigure(0, weight=1)
        self._root.columnconfigure(0, weight=1)
        container.rowconfigure(2, weight=1)
        container.columnconfigure(0, weight=1)

        query_frame = ttk.Frame(
            container,
            padding=(16, 12),
            style="Card.TFrame",
        )
        query_frame.grid(row=0, column=0, sticky="ew")
        query_frame.columnconfigure(1, weight=1)

        ttk.Label(
            query_frame,
            text="股票代码",
            style="Card.TLabel",
        ).grid(
            row=0,
            column=0,
            padx=(0, 8),
        )
        self._entry = ttk.Entry(
            query_frame,
            textvariable=self._query,
            font=("Microsoft YaHei UI", 12),
        )
        self._entry.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        self._entry.bind("<Return>", self._on_enter)
        self._entry.focus_set()

        self._load_button = ttk.Button(
            query_frame,
            text="加载 K 线",
            command=self.load_chart,
            style="Primary.TButton",
        )
        self._load_button.grid(row=0, column=2)
        self._refresh_button = ttk.Button(
            query_frame,
            text="更新行情",
            command=self.refresh_live_market,
            style="Secondary.TButton",
        )
        self._refresh_button.grid(row=0, column=3, padx=(8, 0))
        self._favorites_button = ttk.Button(
            query_frame,
            text="收藏夹",
            command=self.open_favorites,
            style="Secondary.TButton",
        )
        self._favorites_button.grid(row=0, column=4, padx=(8, 0))
        ttk.Label(
            query_frame,
            textvariable=self._market_status,
            style="CardMuted.TLabel",
        ).grid(row=0, column=5, padx=(12, 0))

        ttk.Label(
            container,
            textvariable=self._summary,
            style="Summary.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(10, 6))

        chart_frame = ttk.LabelFrame(
            container,
            text=(
                "日 K、MA5/10/20/30/60/120、成交量、"
                "MACD（红涨绿跌）"
            ),
        )
        chart_frame.grid(row=2, column=0, sticky="nsew")
        chart_frame.rowconfigure(0, weight=1)
        chart_frame.columnconfigure(0, weight=1)
        self._chart = KLineChart(
            chart_frame,
            self._selection_changed,
            self._view_changed,
        )
        self._chart.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=6,
            pady=(6, 4),
        )
        self._timeline = TimeRangeSelector(
            chart_frame,
            self._timeline_range_changed,
        )
        self._timeline.grid(
            row=1,
            column=0,
            sticky="ew",
            padx=6,
            pady=(0, 6),
        )

        selection_frame = ttk.Frame(
            container,
            padding=(14, 10),
            style="Card.TFrame",
        )
        selection_frame.grid(row=3, column=0, sticky="ew", pady=(8, 6))
        selection_frame.columnconfigure(0, weight=1)
        ttk.Label(
            selection_frame,
            textvariable=self._selection_text,
            style="Card.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self._zoom_in_button = ttk.Button(
            selection_frame,
            text="放大图表",
            command=self.zoom_in_chart,
            state="disabled",
            style="Secondary.TButton",
        )
        self._zoom_in_button.grid(row=0, column=1, padx=(10, 0))
        self._zoom_out_button = ttk.Button(
            selection_frame,
            text="缩小图表",
            command=self.zoom_out_chart,
            state="disabled",
            style="Secondary.TButton",
        )
        self._zoom_out_button.grid(row=0, column=2, padx=(8, 0))
        self._compare_button = ttk.Button(
            selection_frame,
            text="寻找最新相似走势",
            command=self.search_selection,
            state="disabled",
            style="Primary.TButton",
        )
        self._compare_button.grid(row=0, column=3, padx=(10, 0))
        self._save_favorite_button = ttk.Button(
            selection_frame,
            text="收藏框选",
            command=self.save_selection_to_favorites,
            state="disabled",
            style="Secondary.TButton",
        )
        self._save_favorite_button.grid(row=0, column=4, padx=(8, 0))

        filter_frame = ttk.Frame(
            selection_frame,
            style="Card.TFrame",
        )
        filter_frame.grid(
            row=1,
            column=0,
            columnspan=5,
            sticky="w",
            pady=(10, 0),
        )
        ttk.Label(
            filter_frame,
            text="相似度筛选（可多选）：",
            style="Card.TLabel",
        ).grid(row=0, column=0, padx=(0, 8))
        self._filter_buttons = [
            ttk.Checkbutton(
                filter_frame,
                text="K 线",
                variable=self._filter_kline,
                command=self._filter_changed,
                style="Card.TCheckbutton",
            ),
            ttk.Checkbutton(
                filter_frame,
                text="成交量",
                variable=self._filter_volume,
                command=self._filter_changed,
                style="Card.TCheckbutton",
            ),
            ttk.Checkbutton(
                filter_frame,
                text="MACD",
                variable=self._filter_macd,
                command=self._filter_changed,
                style="Card.TCheckbutton",
            ),
        ]
        for index, button in enumerate(self._filter_buttons, start=1):
            button.grid(row=0, column=index, padx=(0, 12))
        ttk.Label(
            filter_frame,
            text="按所选指标的综合相似度从高到低排序",
            style="CardMuted.TLabel",
        ).grid(row=0, column=4)

        progress_frame = ttk.Frame(
            container,
            padding=(14, 10),
            style="Card.TFrame",
        )
        progress_frame.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        progress_frame.columnconfigure(0, weight=1)
        ttk.Progressbar(
            progress_frame,
            variable=self._progress,
            maximum=100,
        ).grid(row=0, column=0, sticky="ew")
        ttk.Label(
            progress_frame,
            textvariable=self._status,
            style="CardMuted.TLabel",
        ).grid(
            row=1,
            column=0,
            sticky="w",
            pady=(5, 0),
        )

        table_frame = ttk.LabelFrame(
            container,
            text="相似股票结果（双击股票可查看详细走势）",
            padding=6,
        )
        table_frame.grid(row=5, column=0, sticky="nsew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        columns = (
            "rank",
            "name",
            "code",
            "exchange",
            "score",
            "kline_score",
            "moving_average_score",
            "volume_score",
            "macd_score",
        )
        self._table = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=10,
        )
        headings = {
            "rank": "排名",
            "name": "股票名称",
            "code": "股票代码",
            "exchange": "交易所",
            "score": "综合相似度",
            "kline_score": "K 线",
            "moving_average_score": "均线",
            "volume_score": "成交量",
            "macd_score": "MACD",
        }
        for column, heading in headings.items():
            self._table.heading(column, text=heading)

        self._table.column("rank", width=55, anchor="center", stretch=False)
        self._table.column("name", width=160, anchor="w")
        self._table.column("code", width=90, anchor="center")
        self._table.column("exchange", width=70, anchor="center")
        self._table.column("score", width=110, anchor="e")
        self._table.column("kline_score", width=90, anchor="e")
        self._table.column(
            "moving_average_score",
            width=90,
            anchor="e",
        )
        self._table.column("volume_score", width=90, anchor="e")
        self._table.column("macd_score", width=90, anchor="e")
        self._table.grid(row=0, column=0, sticky="nsew")
        self._table.bind("<Double-1>", self._open_result_chart)
        self._table.tag_configure("odd", background=SURFACE)
        self._table.tag_configure("even", background=ROW_ALTERNATE)

        ttk.Label(
            container,
            text=(
                "综合相似度按勾选的 K 线、成交量和 MACD "
                "等权评分；均线分仅供参考，不构成投资建议。"
            ),
            style="Muted.TLabel",
        ).grid(row=6, column=0, sticky="w", pady=(8, 0))

    def _selected_filters(self) -> tuple[str, ...]:
        """按界面顺序返回当前选中的相似度指标。"""
        filters: list[str] = []
        if self._filter_kline.get():
            filters.append("kline")
        if self._filter_volume.get():
            filters.append("volume")
        if self._filter_macd.get():
            filters.append("macd")
        return tuple(filters)

    def _filter_changed(self) -> None:
        """保证至少保留一项筛选条件。"""
        filters = self._selected_filters()
        if not filters:
            self._filter_kline.set("kline" in self._last_filters)
            self._filter_volume.set("volume" in self._last_filters)
            self._filter_macd.set("macd" in self._last_filters)
            self._status.set("至少需要选择一项相似度指标")
        else:
            self._last_filters = filters
            self._clear_results()
        self._update_compare_button()

    def save_selection_to_favorites(self) -> None:
        """把主图当前框选形态保存到本地收藏夹。"""
        if self._operation is not None or self._target is None:
            return
        selected_bars = self._chart.selected_bars()
        if not (
            KLineAgent.MIN_SELECTION
            <= len(selected_bars)
            <= KLineAgent.MAX_SELECTION
        ):
            messagebox.showwarning(
                "选择范围无效",
                f"请在图上选择 {KLineAgent.MIN_SELECTION} 至 "
                f"{KLineAgent.MAX_SELECTION} 根连续日 K",
            )
            return
        try:
            with KLineAgent(database_path=database_path()) as agent:
                favorite = agent.save_favorite_pattern(
                    self._target,
                    selected_bars,
                    self._bars,
                )
        except AgentError as exc:
            messagebox.showerror("收藏失败", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("收藏失败", f"程序运行异常：{exc}")
            return

        self._status.set(
            f"已收藏：{favorite.name}，可从收藏夹直接搜索"
        )
        messagebox.showinfo(
            "收藏成功",
            f"已保存 {favorite.selection_count} 根 K 线：\n"
            f"{favorite.start_date} 至 {favorite.end_date}",
        )

    def open_favorites(self) -> None:
        """打开收藏夹并提供直接搜索和删除操作。"""
        if self._operation is not None:
            return
        if (
            self._favorites_window is not None
            and self._favorites_window.winfo_exists()
        ):
            self._favorites_window.lift()
            self._favorites_window.focus_set()
            return
        try:
            with KLineAgent(database_path=database_path()) as agent:
                favorites = agent.list_favorite_patterns()
        except Exception as exc:
            messagebox.showerror(
                "打开收藏夹失败",
                f"无法读取收藏夹：{exc}",
            )
            return

        window = tk.Toplevel(self._root)
        self._favorites_window = window
        window.title("K 线收藏夹")
        dialog_width = max(
            820,
            min(1200, window.winfo_screenwidth() - 80),
        )
        dialog_height = max(
            560,
            min(720, window.winfo_screenheight() - 80),
        )
        window.geometry(f"{dialog_width}x{dialog_height}")
        window.minsize(820, 560)
        window.configure(background=APP_BACKGROUND)
        window.transient(self._root)
        window.grab_set()
        window.rowconfigure(1, weight=1)
        window.columnconfigure(0, weight=1)

        ttk.Label(
            window,
            text="收藏的 K 线形态",
            style="PopupTitle.TLabel",
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 8))

        content_frame = ttk.Frame(window, style="App.TFrame")
        content_frame.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=16,
            pady=(0, 10),
        )
        content_frame.rowconfigure(0, weight=1)
        content_frame.columnconfigure(0, weight=1, minsize=320)
        content_frame.columnconfigure(1, weight=2, minsize=480)

        table_frame = ttk.Frame(
            content_frame,
            padding=6,
            style="Card.TFrame",
        )
        table_frame.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=(0, 10),
        )
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        columns = ("name", "code", "count")
        table = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        table.heading("name", text="收藏名称")
        table.heading("code", text="股票代码")
        table.heading("count", text="K 线根数")
        table.column("name", width=210, anchor="w")
        table.column("code", width=82, anchor="center", stretch=False)
        table.column("count", width=72, anchor="center", stretch=False)
        table.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=table.yview,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        table.configure(yscrollcommand=scrollbar.set)

        preview_frame = ttk.LabelFrame(
            content_frame,
            text="K 线走势预览",
            padding=6,
        )
        preview_frame.grid(row=0, column=1, sticky="nsew")
        preview_frame.rowconfigure(1, weight=1)
        preview_frame.columnconfigure(0, weight=1)
        preview_info = tk.StringVar(
            value="选择左侧收藏以预览保存的 K 线走势"
        )
        ttk.Label(
            preview_frame,
            textvariable=preview_info,
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w", padx=4, pady=(0, 4))
        preview_chart = KLineChart(
            preview_frame,
            lambda _start, _end: None,
            lambda _start, _end: None,
            interactive=False,
            empty_text="选择左侧收藏以预览 K 线走势",
        )
        preview_chart.grid(row=1, column=0, sticky="nsew")

        favorite_items: dict[str, FavoritePattern] = {}
        for index, favorite in enumerate(favorites):
            item_id = table.insert(
                "",
                "end",
                values=(
                    favorite.name,
                    favorite.security.code,
                    favorite.selection_count,
                ),
                tags=("even" if index % 2 else "odd",),
            )
            favorite_items[item_id] = favorite
        table.tag_configure("odd", background=SURFACE)
        table.tag_configure("even", background=ROW_ALTERNATE)

        status = tk.StringVar(
            value=(
                f"共 {len(favorites)} 条收藏，"
                "单击预览走势，双击直接搜索相似 K 线"
                if favorites
                else "收藏夹为空，请先在主图框选并收藏 K 线"
            )
        )
        ttk.Label(
            window,
            textvariable=status,
            style="Muted.TLabel",
        ).grid(row=2, column=0, sticky="w", padx=16)

        button_frame = ttk.Frame(window, style="App.TFrame")
        button_frame.grid(
            row=3,
            column=0,
            sticky="e",
            padx=16,
            pady=(10, 14),
        )

        def close_window() -> None:
            self._favorites_window = None
            window.destroy()

        def selected_favorite() -> FavoritePattern | None:
            selection = table.selection()
            if not selection:
                messagebox.showwarning(
                    "请选择收藏",
                    "请先选择一条 K 线收藏",
                    parent=window,
                )
                return None
            return favorite_items.get(selection[0])

        def update_preview(
            _event: tk.Event[tk.Misc] | None = None,
        ) -> None:
            """按当前收藏恢复只读 K 线图和原框选范围。"""
            selection = table.selection()
            favorite = (
                favorite_items.get(selection[0])
                if selection
                else None
            )
            if favorite is None:
                preview_info.set("选择左侧收藏以预览保存的 K 线走势")
                preview_chart.clear()
                return
            preview_info.set(
                f"{favorite.security.name} "
                f"({favorite.security.code})  |  "
                f"{favorite.start_date} 至 {favorite.end_date}  |  "
                f"收藏时间：{favorite.created_at.replace('T', ' ')[:19]}"
            )
            preview_chart.set_bars(list(favorite.context_bars))
            selection_end = (
                favorite.selection_start
                + favorite.selection_count
                - 1
            )
            if preview_chart.select_range(
                favorite.selection_start,
                selection_end,
            ):
                preview_chart.zoom_to_selection()

        def search_favorite() -> None:
            favorite = selected_favorite()
            if favorite is None:
                return
            if self._operation is not None:
                messagebox.showwarning(
                    "操作进行中",
                    "请等待当前行情操作完成后再搜索收藏",
                    parent=window,
                )
                return
            close_window()
            self._start_favorite_search(favorite.favorite_id)

        def delete_favorite() -> None:
            favorite = selected_favorite()
            if favorite is None:
                return
            confirmed = messagebox.askyesno(
                "删除收藏",
                f"确定删除“{favorite.name}”吗？",
                parent=window,
            )
            if not confirmed:
                return
            try:
                with KLineAgent(database_path=database_path()) as agent:
                    agent.delete_favorite_pattern(favorite.favorite_id)
            except AgentError as exc:
                messagebox.showerror(
                    "删除失败",
                    str(exc),
                    parent=window,
                )
                return
            item_id = table.selection()[0]
            table.delete(item_id)
            favorite_items.pop(item_id, None)
            status.set(f"删除完成，剩余 {len(favorite_items)} 条收藏")
            remaining_items = table.get_children()
            if remaining_items:
                first_item = remaining_items[0]
                table.selection_set(first_item)
                table.focus(first_item)
            update_preview()

        search_button = ttk.Button(
            button_frame,
            text="搜索相似 K 线",
            command=search_favorite,
            style="Primary.TButton",
        )
        search_button.grid(row=0, column=0)
        ttk.Button(
            button_frame,
            text="删除收藏",
            command=delete_favorite,
            style="Secondary.TButton",
        ).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(
            button_frame,
            text="关闭",
            command=close_window,
            style="Secondary.TButton",
        ).grid(row=0, column=2, padx=(8, 0))

        table.bind("<Double-1>", lambda _event: search_favorite())
        table.bind("<<TreeviewSelect>>", update_preview)
        window.protocol("WM_DELETE_WINDOW", close_window)
        if favorites:
            first_item = table.get_children()[0]
            table.selection_set(first_item)
            table.focus(first_item)
            update_preview()
        window.focus_set()

    def _start_favorite_search(self, favorite_id: int) -> None:
        """从收藏编号启动后台相似度搜索。"""
        selected_filters = self._selected_filters()
        if not selected_filters:
            messagebox.showwarning(
                "筛选条件无效",
                "请至少选择一项相似度指标",
            )
            return
        self._set_operation("search")
        self._progress.set(0)
        self._status.set("正在读取收藏并检查全市场最新日 K 数据...")
        self._clear_results()
        threading.Thread(
            target=self._search_favorite_worker,
            args=(favorite_id, selected_filters),
            daemon=True,
        ).start()

    def _search_favorite_worker(
        self,
        favorite_id: int,
        selected_filters: tuple[str, ...],
    ) -> None:
        try:
            with KLineAgent(database_path=database_path()) as agent:
                result = agent.search_favorite_pattern(
                    favorite_id,
                    self._report_progress,
                    selected_filters,
                )
            self._messages.put(
                ("favorite_search_success", (*result, selected_filters))
            )
        except AgentError as exc:
            self._messages.put(("error", ("search", str(exc))))
        except Exception as exc:
            self._messages.put(
                ("error", ("search", f"程序运行异常：{exc}"))
            )

    def _schedule_auto_refresh(self, delay: int | None = None) -> None:
        """安排下一次五分钟自动行情更新。"""
        if self._auto_refresh_id is not None:
            self._root.after_cancel(self._auto_refresh_id)
        self._auto_refresh_id = self._root.after(
            delay or self.AUTO_REFRESH_INTERVAL,
            self._auto_refresh_live_market,
        )

    def _auto_refresh_live_market(self) -> None:
        self._auto_refresh_id = None
        if self._operation is not None:
            self._schedule_auto_refresh(self.AUTO_REFRESH_RETRY_INTERVAL)
            return
        self._start_live_refresh(manual=False)

    def refresh_live_market(self) -> None:
        """手动刷新全市场实时行情。"""
        if self._operation is not None:
            return
        self._start_live_refresh(manual=True)

    def _start_live_refresh(self, manual: bool) -> None:
        if self._auto_refresh_id is not None:
            self._root.after_cancel(self._auto_refresh_id)
            self._auto_refresh_id = None
        self._set_operation("refresh")
        self._progress.set(0)
        self._status.set("正在更新全市场实时行情...")
        target_code = self._target.code if self._target is not None else None
        threading.Thread(
            target=self._refresh_live_market_worker,
            args=(manual, target_code),
            daemon=True,
        ).start()

    def _refresh_live_market_worker(
        self,
        manual: bool,
        target_code: str | None,
    ) -> None:
        try:
            with KLineAgent(database_path=database_path()) as agent:
                snapshot = agent.refresh_live_market(self._report_progress)
                chart_result = (
                    agent.load_chart(target_code)
                    if target_code is not None
                    else None
                )
            self._messages.put(
                ("refresh_success", (snapshot, chart_result, manual))
            )
        except AgentError as exc:
            self._messages.put(("refresh_error", (str(exc), manual)))
        except Exception as exc:
            self._messages.put(
                ("refresh_error", (f"程序运行异常：{exc}", manual))
            )

    def _on_enter(self, _event: tk.Event[tk.Misc]) -> None:
        self.load_chart()

    def _open_result_chart(self, event: tk.Event[tk.Misc]) -> None:
        """双击结果行时打开该股票的近 150 日图表。"""
        item_id = self._table.identify_row(event.y)
        security = self._result_securities.get(item_id)
        if security is None:
            return
        self._table.selection_set(item_id)
        self._table.focus(item_id)
        self._open_security_chart(security)

    def _open_security_chart(self, security: Security) -> tk.Toplevel:
        """创建股票图表窗口并在后台读取行情。"""
        window = tk.Toplevel(self._root)
        window.title(
            f"{security.name}（{security.symbol}）近 "
            f"{self.RESULT_CHART_PERIOD} 日走势"
        )
        window.geometry("1280x900")
        window.minsize(900, 700)
        window.configure(background=APP_BACKGROUND)
        window.rowconfigure(1, weight=1)
        window.columnconfigure(0, weight=1)

        ttk.Label(
            window,
            text=(
                f"{security.name}（{security.symbol}）近 "
                f"{self.RESULT_CHART_PERIOD} 个交易日"
            ),
            style="PopupTitle.TLabel",
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 8))

        chart_frame = ttk.LabelFrame(
            window,
            text=(
                "日 K、MA5/10/20/30/60/120、成交量、"
                "MACD（拖拽下方时间轴缩放）"
            ),
        )
        chart_frame.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=16,
            pady=(0, 8),
        )
        chart_frame.rowconfigure(0, weight=1)
        chart_frame.columnconfigure(0, weight=1)

        timeline: TimeRangeSelector | None = None

        def view_changed(start: int, end: int) -> None:
            if timeline is not None:
                timeline.set_view(start, end)

        chart = KLineChart(
            chart_frame,
            lambda _start, _end: None,
            view_changed,
        )
        chart.grid(
            row=0,
            column=0,
            sticky="nsew",
        )
        # 弹窗仅用于查看，不启用主图的片段选择操作。
        chart.configure(cursor="arrow")
        chart.unbind("<ButtonPress-1>")
        chart.unbind("<B1-Motion>")
        chart.unbind("<ButtonRelease-1>")

        def timeline_range_changed(start: int, end: int) -> None:
            chart.set_visible_range(start, end)

        timeline = TimeRangeSelector(
            chart_frame,
            timeline_range_changed,
        )
        timeline.grid(
            row=1,
            column=0,
            sticky="ew",
            pady=(8, 0),
        )

        status = tk.StringVar(value="正在加载最新日 K 数据...")
        ttk.Label(
            window,
            textvariable=status,
            style="Muted.TLabel",
        ).grid(row=2, column=0, sticky="w", padx=16, pady=(0, 14))

        messages: queue.Queue[tuple[str, Any]] = queue.Queue()

        def load_worker() -> None:
            try:
                with KLineAgent(database_path=database_path()) as agent:
                    result = agent.load_chart(security.code)
                messages.put(("success", result))
            except AgentError as exc:
                messages.put(("error", str(exc)))
            except Exception as exc:
                messages.put(("error", f"程序运行异常：{exc}"))

        def process_result() -> None:
            try:
                kind, payload = messages.get_nowait()
            except queue.Empty:
                window.after(100, process_result)
                return
            if kind == "error":
                status.set(f"加载失败：{payload}")
                return

            resolved, bars, offline = payload
            visible_start = max(
                0,
                len(bars) - self.RESULT_CHART_PERIOD,
            )
            timeline.configure(state="normal")
            timeline.set_bars(bars)
            chart.set_bars(bars)
            chart.set_visible_range(visible_start, len(bars))
            visible_bars = bars[visible_start:]
            source_text = "本地缓存" if offline else "最新行情"
            status.set(
                f"{resolved.name}（{resolved.symbol}）    "
                f"{visible_bars[0].trade_date} 至 "
                f"{visible_bars[-1].trade_date}    "
                f"共 {len(visible_bars)} 根    来源：{source_text}"
            )

        threading.Thread(target=load_worker, daemon=True).start()
        window.after(100, process_result)
        window.focus_set()
        return window

    def zoom_in_chart(self) -> None:
        """放大当前图表时间区间。"""
        if self._operation is not None:
            return
        if self._chart.zoom_in():
            self._show_zoom_status()
        self._update_zoom_buttons()

    def zoom_out_chart(self) -> None:
        """缩小当前图表时间区间。"""
        if self._operation is not None:
            return
        if self._chart.zoom_out():
            self._show_zoom_status()
        self._update_zoom_buttons()

    def _view_changed(self, start: int, end: int) -> None:
        """将主图可见范围同步到时间轴。"""
        self._timeline.set_view(start, end)

    def _timeline_range_changed(self, start: int, end: int) -> None:
        """按时间轴拖拽结果放大主图。"""
        if self._operation is not None or self._target is None:
            return
        self._chart.clear_selection()
        self._chart.set_visible_range(start, end)
        self._show_zoom_status()
        self._update_zoom_buttons()

    def _show_zoom_status(self) -> None:
        view_start, view_end = self._chart.visible_range()
        if not self._bars or view_end <= view_start:
            return
        self._status.set(
            f"当前显示：{self._bars[view_start].trade_date} 至 "
            f"{self._bars[view_end - 1].trade_date}，"
            f"共 {view_end - view_start} 根"
        )

    def load_chart(self) -> None:
        """校验代码并在后台加载目标股票日 K。"""
        if self._operation is not None:
            return

        query = self._query.get().strip()
        if not query:
            messagebox.showwarning("请输入代码", "请输入六位 A 股代码，例如：600519")
            self._entry.focus_set()
            return

        self._set_operation("chart")
        self._target = None
        self._bars = []
        self._timeline.clear()
        self._chart.clear()
        self._progress.set(0)
        self._status.set("正在读取目标股票历史日 K...")
        self._summary.set("正在加载 K 线")
        self._clear_results()

        worker = threading.Thread(
            target=self._load_chart_worker,
            args=(query,),
            daemon=True,
        )
        worker.start()

    def _load_chart_worker(self, query: str) -> None:
        try:
            with KLineAgent(database_path=database_path()) as agent:
                result = agent.load_chart(query)
            self._messages.put(("chart_success", result))
        except AgentError as exc:
            self._messages.put(("error", ("chart", str(exc))))
        except Exception as exc:
            self._messages.put(
                ("error", ("chart", f"程序运行异常：{exc}"))
            )

    def search_selection(self) -> None:
        """将图上选择的历史片段与全市场最新走势比较。"""
        if self._operation is not None or self._target is None:
            return
        selected_bars = self._chart.selected_bars()
        if not (
            KLineAgent.MIN_SELECTION
            <= len(selected_bars)
            <= KLineAgent.MAX_SELECTION
        ):
            messagebox.showwarning(
                "选择范围无效",
                f"请在图上选择 {KLineAgent.MIN_SELECTION} 至 "
                f"{KLineAgent.MAX_SELECTION} 根连续日 K",
            )
            return

        selected_filters = self._selected_filters()
        if not selected_filters:
            messagebox.showwarning(
                "筛选条件无效",
                "请至少选择一项相似度指标",
            )
            return

        # 开始匹配前同步放大价格、成交量和 MACD 的选中区间。
        self._chart.zoom_to_selection()
        self._set_operation("search")
        self._progress.set(0)
        self._status.set("正在检查全市场最新日 K 数据...")
        self._clear_results()
        context_bars = list(self._bars)
        worker = threading.Thread(
            target=self._search_selection_worker,
            args=(
                self._target,
                selected_bars,
                context_bars,
                selected_filters,
            ),
            daemon=True,
        )
        worker.start()

    def _search_selection_worker(
        self,
        target: Security,
        selected_bars: list[KLine],
        context_bars: list[KLine],
        selected_filters: tuple[str, ...],
    ) -> None:
        try:
            with KLineAgent(database_path=database_path()) as agent:
                result = agent.search_segment(
                    target,
                    selected_bars,
                    self._report_progress,
                    context_bars=context_bars,
                    selected_filters=selected_filters,
                )
            self._messages.put(
                (
                    "search_success",
                    (
                        *result,
                        selected_bars[0].trade_date,
                        selected_bars[-1].trade_date,
                        len(selected_bars),
                        selected_filters,
                    ),
                )
            )
        except AgentError as exc:
            self._messages.put(("error", ("search", str(exc))))
        except Exception as exc:
            self._messages.put(
                ("error", ("search", f"程序运行异常：{exc}"))
            )

    def _report_progress(
        self,
        completed: int,
        total: int,
        succeeded: int,
    ) -> None:
        self._messages.put(
            ("progress", (completed, total, succeeded)),
        )

    def _process_messages(self) -> None:
        """在界面线程中处理后台消息，保证 tkinter 线程安全。"""
        try:
            while True:
                kind, payload = self._messages.get_nowait()
                if kind == "progress":
                    self._show_progress(*payload)
                elif kind == "chart_success":
                    self._show_chart(*payload)
                elif kind == "search_success":
                    self._show_results(*payload)
                elif kind == "favorite_search_success":
                    self._show_favorite_results(*payload)
                elif kind == "refresh_success":
                    self._show_live_refresh(*payload)
                elif kind == "refresh_error":
                    self._show_live_refresh_error(*payload)
                elif kind == "error":
                    self._show_error(*payload)
        except queue.Empty:
            pass
        self._root.after(100, self._process_messages)

    def _show_progress(
        self,
        completed: int,
        total: int,
        succeeded: int,
    ) -> None:
        percent = completed * 100 / total if total else 100
        self._progress.set(percent)
        if self._operation == "refresh":
            self._status.set(
                f"正在更新实时行情：{completed}/{total} 个分片"
            )
        else:
            self._status.set(
                f"正在同步全市场日 K：{completed}/{total}，"
                f"成功 {succeeded}"
            )

    def _show_live_refresh(
        self,
        snapshot: LiveMarketSnapshot,
        chart_result: tuple[Security, list[KLine], bool] | None,
        manual: bool,
    ) -> None:
        """显示实时更新结果，并在合适时刷新当前主图。"""
        updated_time = snapshot.updated_at.astimezone().strftime("%H:%M:%S")
        self._market_status.set(
            f"实时行情：{snapshot.market_date} {updated_time}，"
            f"{snapshot.stock_count} 只"
        )

        should_update_chart = (
            chart_result is not None
            and (manual or not self._chart.selected_bars())
        )
        if should_update_chart and chart_result is not None:
            old_start, old_end = self._chart.visible_range()
            old_count = len(self._bars)
            old_at_end = old_count > 0 and old_end >= old_count
            target, bars, _offline = chart_result
            self._target = target
            self._bars = bars
            self._timeline.set_bars(bars)
            self._chart.set_bars(bars)

            # 更新时间后尽量保持用户原来的时间轴可见区间。
            if old_count and old_end > old_start:
                visible_count = min(old_end - old_start, len(bars))
                if old_at_end:
                    view_end = len(bars)
                    view_start = max(0, view_end - visible_count)
                else:
                    view_start = min(old_start, len(bars) - visible_count)
                    view_end = view_start + visible_count
                self._chart.set_visible_range(view_start, view_end)
            source_text = (
                "实时行情"
                if bars[-1].trade_date == snapshot.market_date
                else "最新历史行情"
            )
            self._summary.set(
                f"目标：{target.name}（{target.symbol}）    "
                f"区间：{bars[0].trade_date} 至 {bars[-1].trade_date}    "
                f"共 {len(bars)} 根    来源：{source_text}"
            )

        update_mode = "手动" if manual else "自动"
        self._progress.set(100)
        self._status.set(
            f"{update_mode}更新完成：已刷新 "
            f"{snapshot.stock_count} 只股票实时行情"
        )
        self._set_operation(None)
        self._schedule_auto_refresh()

    def _show_live_refresh_error(self, error: str, manual: bool) -> None:
        """处理实时更新失败，保留原有图表和历史缓存。"""
        self._progress.set(0)
        self._market_status.set("实时行情：更新失败")
        self._status.set(f"实时行情更新失败：{error}")
        self._set_operation(None)
        self._schedule_auto_refresh()
        if manual:
            messagebox.showerror("更新行情失败", error)

    def _show_chart(
        self,
        target: Security,
        bars: list[KLine],
        offline: bool,
    ) -> None:
        self._target = target
        self._bars = bars
        source_text = "本地缓存" if offline else "最新行情"
        self._summary.set(
            f"目标：{target.name}（{target.symbol}）    "
            f"区间：{bars[0].trade_date} 至 {bars[-1].trade_date}    "
            f"共 {len(bars)} 根    来源：{source_text}"
        )
        self._timeline.set_bars(bars)
        self._chart.set_bars(bars)
        self._progress.set(100)
        self._status.set(
            "加载完成：先在时间轴选择放大范围，再在主图选择匹配片段"
        )
        self._set_operation(None)

    def _show_results(
        self,
        target: Security,
        data_date: str,
        results: list[SimilarityResult],
        offline: bool,
        selection_start: str,
        selection_end: str,
        selection_count: int,
        selected_filters: tuple[str, ...],
    ) -> None:
        self._target = target
        self._clear_results()
        source_text = "本地缓存" if offline else "最新行情"
        self._summary.set(
            f"目标：{target.name}（{target.symbol}）    "
            f"所选：{selection_start} 至 {selection_end}，"
            f"{selection_count} 根    候选截止：{data_date}    "
            f"来源：{source_text}"
        )
        for rank, item in enumerate(results, start=1):
            item_id = self._table.insert(
                "",
                "end",
                values=(
                    rank,
                    item.security.name,
                    item.security.code,
                    item.security.exchange,
                    f"{item.score:.2f}%",
                    (
                        f"{item.kline_score:.2f}%"
                        if item.kline_score is not None
                        else "-"
                    ),
                    (
                        f"{item.moving_average_score:.2f}%"
                        if item.moving_average_score is not None
                        else "-"
                    ),
                    (
                        f"{item.volume_score:.2f}%"
                        if item.volume_score is not None
                        else "-"
                    ),
                    (
                        f"{item.macd_score:.2f}%"
                        if item.macd_score is not None
                        else "-"
                    ),
                ),
                tags=("even" if rank % 2 == 0 else "odd",),
            )
            self._result_securities[item_id] = item.security
        self._progress.set(100)
        filter_names = {
            "kline": "K 线",
            "volume": "成交量",
            "macd": "MACD",
        }
        filter_text = "、".join(
            filter_names[name] for name in selected_filters
        )
        self._status.set(
            f"查询完成，共找到 {len(results)} 只相似股票；"
            f"评分指标：{filter_text}；双击结果可查看近 "
            f"{self.RESULT_CHART_PERIOD} 日走势"
        )
        self._set_operation(None)

    def _show_favorite_results(
        self,
        favorite: FavoritePattern,
        target: Security,
        data_date: str,
        results: list[SimilarityResult],
        offline: bool,
        selected_filters: tuple[str, ...],
    ) -> None:
        """恢复收藏图形和框选范围，然后显示搜索结果。"""
        self._target = target
        self._bars = list(favorite.context_bars)
        self._timeline.set_bars(self._bars)
        self._chart.set_bars(self._bars)
        selection_end = (
            favorite.selection_start + favorite.selection_count - 1
        )
        restored = self._chart.select_range(
            favorite.selection_start,
            selection_end,
        )
        if not restored:
            self._show_error("search", "收藏的 K 线框选范围无效")
            return
        self._chart.zoom_to_selection()
        self._show_results(
            target,
            data_date,
            results,
            offline,
            favorite.start_date,
            favorite.end_date,
            favorite.selection_count,
            selected_filters,
        )
        self._status.set(
            f"已从收藏“{favorite.name}”找到 {len(results)} 只相似股票"
        )

    def _show_error(self, operation: str, error: str) -> None:
        self._progress.set(0)
        self._status.set("加载失败" if operation == "chart" else "查询失败")
        if operation == "chart":
            self._summary.set("尚未加载 K 线")
        self._set_operation(None)
        messagebox.showerror("查询失败", error)

    def _selection_changed(
        self,
        start: int | None,
        end: int | None,
    ) -> None:
        self._clear_results()
        if start is None or end is None or not self._bars:
            self._selection_text.set(
                f"在图表上按住鼠标左键拖拽选择 "
                f"{KLineAgent.MIN_SELECTION} 至 "
                f"{KLineAgent.MAX_SELECTION} 根连续日 K"
            )
        else:
            count = end - start + 1
            start_date = self._bars[start].trade_date
            end_date = self._bars[end].trade_date
            if count < KLineAgent.MIN_SELECTION:
                suffix = f"至少需要 {KLineAgent.MIN_SELECTION} 根"
            elif count > KLineAgent.MAX_SELECTION:
                suffix = f"最多允许 {KLineAgent.MAX_SELECTION} 根"
            else:
                suffix = "可以开始匹配"
            self._selection_text.set(
                f"已选择：{start_date} 至 {end_date}，"
                f"共 {count} 根（{suffix}）"
            )
        self._update_compare_button()
        self._update_zoom_buttons()

    def _set_operation(self, operation: str | None) -> None:
        self._operation = operation
        self._load_button.configure(
            state="disabled" if operation is not None else "normal"
        )
        self._refresh_button.configure(
            state="disabled" if operation is not None else "normal"
        )
        self._favorites_button.configure(
            state="disabled" if operation is not None else "normal"
        )
        for button in self._filter_buttons:
            button.configure(
                state="disabled" if operation is not None else "normal"
            )
        timeline_enabled = operation is None and self._target is not None
        self._timeline.configure(
            state="normal" if timeline_enabled else "disabled"
        )
        self._update_compare_button()
        self._update_zoom_buttons()
        if operation is None:
            self._entry.focus_set()

    def _update_compare_button(self) -> None:
        selected_count = len(self._chart.selected_bars())
        valid = (
            self._operation is None
            and self._target is not None
            and KLineAgent.MIN_SELECTION
            <= selected_count
            <= KLineAgent.MAX_SELECTION
            and bool(self._selected_filters())
        )
        self._compare_button.configure(
            state="normal" if valid else "disabled"
        )
        self._save_favorite_button.configure(
            state="normal" if valid else "disabled"
        )

    def _update_zoom_buttons(self) -> None:
        enabled = self._operation is None and self._target is not None
        self._zoom_in_button.configure(
            state=(
                "normal"
                if enabled and self._chart.can_zoom_in()
                else "disabled"
            )
        )
        self._zoom_out_button.configure(
            state=(
                "normal"
                if enabled and self._chart.can_zoom_out()
                else "disabled"
            )
        )

    def _clear_results(self) -> None:
        self._result_securities.clear()
        for item in self._table.get_children():
            self._table.delete(item)


def main() -> None:
    root = tk.Tk()
    KLineAgentWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
