from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Security:
    """A 股证券基础信息。"""

    market: int
    code: str
    name: str
    exchange: str

    @property
    def symbol(self) -> str:
        return f"{self.code}.{self.exchange}"


@dataclass(frozen=True, slots=True)
class KLine:
    """单根日 K 线。"""

    trade_date: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True, slots=True)
class FavoritePattern:
    """持久化保存的 K 线框选形态。"""

    favorite_id: int
    name: str
    security: Security
    context_bars: tuple[KLine, ...]
    selection_start: int
    selection_count: int
    created_at: str

    @property
    def selected_bars(self) -> tuple[KLine, ...]:
        """返回收藏中真正参与相似度计算的框选区间。"""
        selection_end = self.selection_start + self.selection_count
        return self.context_bars[self.selection_start : selection_end]

    @property
    def start_date(self) -> str:
        return self.selected_bars[0].trade_date

    @property
    def end_date(self) -> str:
        return self.selected_bars[-1].trade_date


@dataclass(frozen=True, slots=True)
class SimilarityResult:
    """相似股票及其评分。"""

    security: Security
    score: float
    kline_score: float | None = None
    moving_average_score: float | None = None
    volume_score: float | None = None
    macd_score: float | None = None
