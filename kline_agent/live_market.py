from __future__ import annotations

import json
import math
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import KLine, Security


class LiveMarketError(RuntimeError):
    """实时全市场行情读取失败。"""


LiveProgressCallback = Callable[[int, int, int], None]


@dataclass(frozen=True, slots=True)
class LiveMarketSnapshot:
    """经过校验的单次全市场实时行情。"""

    market_date: str
    updated_at: datetime
    entries: tuple[tuple[Security, KLine], ...]

    @property
    def stock_count(self) -> int:
        return len(self.entries)


class EastmoneyLiveMarketSource:
    """从固定的东方财富公开接口读取 A 股实时日 K 快照。"""

    ENDPOINT = "https://push2delay.eastmoney.com/api/qt/clist/get"
    PAGE_SIZE = 100
    WORKERS = 6
    REQUEST_TIMEOUT = 15
    MAX_RESPONSE_SIZE = 4 * 1024 * 1024
    MAX_TOTAL = 20_000
    MINIMUM_COVERAGE = 0.8
    MAX_STALE_DAYS = 20
    CHINA_TIMEZONE = timezone(timedelta(hours=8))

    def _build_url(self, page: int) -> str:
        parameters = {
            "pn": str(page),
            "pz": str(self.PAGE_SIZE),
            "po": "1",
            "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
            "fields": "f12,f13,f2,f5,f15,f16,f17,f18,f124",
            "_": f"{time.time_ns()}_{page}",
        }
        return f"{self.ENDPOINT}?{urlencode(parameters)}"

    def _read_json(self, url: str) -> dict[str, Any]:
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "User-Agent": "KLineAgent/1.0",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.REQUEST_TIMEOUT) as response:
                status = int(getattr(response, "status", 200))
                if status != 200:
                    raise LiveMarketError(f"实时行情服务返回状态 {status}")
                content = response.read(self.MAX_RESPONSE_SIZE + 1)
        except LiveMarketError:
            raise
        except OSError as exc:
            raise LiveMarketError("实时行情网络请求失败") from exc
        if len(content) > self.MAX_RESPONSE_SIZE:
            raise LiveMarketError("实时行情响应体积异常")
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LiveMarketError("实时行情响应格式无效") from exc
        if not isinstance(payload, dict):
            raise LiveMarketError("实时行情响应格式无效")
        return payload

    @staticmethod
    def _validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        if payload.get("rc") != 0 or not isinstance(data, dict):
            raise LiveMarketError("实时行情响应格式无效")
        return data

    def _fetch_page(self, page: int) -> dict[str, Any]:
        last_error: LiveMarketError | None = None
        for attempt in range(2):
            try:
                return self._validate_payload(
                    self._read_json(self._build_url(page))
                )
            except LiveMarketError as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.35)
        raise last_error or LiveMarketError(
            f"第 {page} 页实时行情读取失败"
        )

    @classmethod
    def _parse_date(cls, value: object) -> str | None:
        try:
            seconds = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if seconds <= 0:
            return None
        try:
            return datetime.fromtimestamp(
                seconds,
                cls.CHINA_TIMEZONE,
            ).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return None

    @classmethod
    def _parse_bar(
        cls,
        record: object,
        securities: dict[str, Security],
    ) -> tuple[Security, KLine] | None:
        if not isinstance(record, dict):
            return None
        code = str(record.get("f12") or "")
        security = securities.get(code)
        if security is None:
            return None
        try:
            market = int(record.get("f13"))
            prices = tuple(
                float(record.get(field))
                for field in ("f17", "f15", "f16", "f2")
            )
            volume = float(record.get("f5"))
        except (TypeError, ValueError, OverflowError):
            return None
        open_price, high, low, close = prices
        trade_date = cls._parse_date(record.get("f124"))
        if (
            market != security.market
            or trade_date is None
            or not all(math.isfinite(value) and value > 0 for value in prices)
            or not math.isfinite(volume)
            or volume < 0
            or high < low
            or high < max(open_price, close)
            or low > min(open_price, close)
        ):
            return None
        return security, KLine(
            trade_date,
            open_price,
            high,
            low,
            close,
            volume,
        )

    def fetch(
        self,
        securities: Sequence[Security],
        progress: LiveProgressCallback | None = None,
    ) -> LiveMarketSnapshot:
        """并发下载、校验并返回覆盖当前证券列表的实时快照。"""
        expected = {security.code: security for security in securities}
        if not expected:
            raise LiveMarketError("本地没有可更新的 A 股列表")

        first_page = self._fetch_page(1)
        try:
            total = int(first_page.get("total"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise LiveMarketError("实时行情总数无效") from exc
        first_records = first_page.get("diff")
        if (
            total <= 0
            or total > self.MAX_TOTAL
            or not isinstance(first_records, list)
        ):
            raise LiveMarketError("实时行情总数无效")

        page_count = math.ceil(total / self.PAGE_SIZE)
        pages: list[list[object] | None] = [None] * page_count
        pages[0] = first_records
        completed = 1
        if progress:
            progress(completed, page_count, completed)

        progress_lock = threading.Lock()
        with ThreadPoolExecutor(
            max_workers=min(self.WORKERS, max(1, page_count - 1))
        ) as executor:
            futures = {
                executor.submit(self._fetch_page, page): page
                for page in range(2, page_count + 1)
            }
            for future in as_completed(futures):
                page = futures[future]
                data = future.result()
                records = data.get("diff")
                if not isinstance(records, list):
                    raise LiveMarketError(
                        f"第 {page} 页实时行情格式无效"
                    )
                pages[page - 1] = records
                with progress_lock:
                    completed += 1
                    if progress:
                        progress(completed, page_count, completed)

        parsed: list[tuple[Security, KLine]] = []
        date_counts: Counter[str] = Counter()
        for page in pages:
            if page is None:
                raise LiveMarketError("实时行情分页不完整")
            for record in page:
                item = self._parse_bar(record, expected)
                if item is None:
                    continue
                parsed.append(item)
                date_counts[item[1].trade_date] += 1
        if not date_counts:
            raise LiveMarketError("实时行情中没有有效 A 股数据")

        market_date = max(
            date_counts,
            key=lambda value: (date_counts[value], value),
        )
        market_day = datetime.fromisoformat(market_date).date()
        china_today = datetime.now(self.CHINA_TIMEZONE).date()
        if (
            market_day > china_today
            or (china_today - market_day).days > self.MAX_STALE_DAYS
        ):
            raise LiveMarketError("实时行情日期异常")
        entries_by_code = {
            security.code: (security, bar)
            for security, bar in parsed
            if bar.trade_date == market_date
        }
        required = max(1, math.ceil(len(expected) * self.MINIMUM_COVERAGE))
        if len(entries_by_code) < required:
            raise LiveMarketError(
                "实时行情覆盖不足："
                f"{len(entries_by_code)}/{len(expected)}"
            )
        entries = tuple(
            sorted(
                entries_by_code.values(),
                key=lambda item: (item[0].market, item[0].code),
            )
        )
        return LiveMarketSnapshot(
            market_date,
            datetime.now(self.CHINA_TIMEZONE),
            entries,
        )
