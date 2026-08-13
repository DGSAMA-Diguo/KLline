from __future__ import annotations

import json
import math
import struct
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from re import fullmatch, search
from typing import Callable, Iterable, Iterator
from urllib.request import Request, urlopen

from pytdx.helper import get_volume
from pytdx.hq import TdxHq_API
from pytdx.parser.get_security_list import GetSecurityList

from .models import KLine, Security


class MarketDataError(RuntimeError):
    """行情接口访问失败。"""


# 招商证券旧行情节点放在首位，其余节点用于旧节点不可用时自动切换。
DEFAULT_HOSTS: tuple[tuple[str, int], ...] = (
    ("119.147.212.81", 7709),
    ("180.153.18.170", 7709),
    ("218.75.126.9", 7709),
    ("115.238.56.198", 7709),
    ("60.191.117.167", 7709),
    ("115.238.90.165", 7709),
    ("220.178.55.86", 7709),
    ("60.12.136.250", 7709),
    ("117.34.114.13", 7709),
)

# 通达信市场 2 能读取北交所 K 线，但不能读取证券列表，因此仅从证监会官网补充代码和简称。
CSRC_BJ_LIST_URL = "http://eid.csrc.gov.cn/202610/index{suffix}.html"
CSRC_LIST_MAX_BYTES = 1_000_000
CSRC_LIST_RETRIES = 3
CSRC_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) KLineAgent/1.0"
EASTMONEY_BJ_QUOTE_URL = (
    "https://push2delay.eastmoney.com/api/qt/stock/get"
    "?secid=0.{code}&fields=f57,f58"
)
EASTMONEY_QUOTE_MAX_BYTES = 65_536


def security_from_code(code: str, name: str | None = None) -> Security:
    """根据六位代码判断 A 股所属市场。"""
    security_name = (name or code).strip()
    if (
        not security_name
        or len(security_name) > 32
        or not all(character.isprintable() for character in security_name)
    ):
        raise ValueError(f"证券 {code} 的简称无效")

    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return Security(1, code, security_name, "SH")
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return Security(0, code, security_name, "SZ")
    if code.startswith("920"):
        return Security(2, code, security_name, "BJ")
    raise ValueError(f"不支持的 A 股代码：{code}")


def is_a_share(market: int, code: str) -> bool:
    """过滤指数、基金、债券和 B 股，仅保留沪深京 A 股。"""
    try:
        security = security_from_code(code)
    except ValueError:
        return False
    return security.market == market


class SafeGetSecurityList(GetSecurityList):
    """兼容证券简称被固定长度字段截断后的不完整 GBK 字节。"""

    def parseResponse(self, body_buf: bytes) -> list[OrderedDict]:
        (count,) = struct.unpack("<H", body_buf[:2])
        stocks: list[OrderedDict] = []
        position = 2
        for _ in range(count):
            record = body_buf[position : position + 29]
            (
                code,
                volume_unit,
                name_bytes,
                _reserved1,
                decimal_point,
                pre_close_raw,
                _reserved2,
            ) = struct.unpack("<6sH8s4sBI4s", record)
            stocks.append(
                OrderedDict(
                    (
                        ("code", code.decode("ascii")),
                        ("volunit", volume_unit),
                        ("decimal_point", decimal_point),
                        ("name", name_bytes.decode("gbk", errors="ignore").rstrip("\x00")),
                        ("pre_close", get_volume(pre_close_raw)),
                    )
                )
            )
            position += 29
        return stocks


class CsrcSecurityListParser(HTMLParser):
    """解析证监会北交所主体信息表格及分页信息。"""

    def __init__(self) -> None:
        super().__init__()
        self._table_depth = 0
        self._cell_parts: list[str] | None = None
        self._cells: list[str] = []
        self.page_count = 1
        self.security_count: int | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if tag == "table":
            classes = (attributes.get("class") or "").split()
            if self._table_depth:
                self._table_depth += 1
            elif "m-table2" in classes:
                self._table_depth = 1
        elif tag == "td" and self._table_depth:
            self._cell_parts = []

        if tag == "a":
            href = attributes.get("href") or ""
            match = fullmatch(r"javascript:gotoPage\((\d+)\)", href)
            if match:
                self.page_count = max(self.page_count, int(match.group(1)))

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._cell_parts is not None:
            self._cells.append("".join(self._cell_parts).strip())
            self._cell_parts = None
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)
        match = search(r"共\s*(\d+)\s*条数据", data)
        if match:
            self.security_count = int(match.group(1))

    def securities(self) -> list[Security]:
        """返回当前页面中经过严格校验的北交所股票。"""
        if not self._cells or len(self._cells) % 4 != 0:
            raise ValueError("证监会股票列表表格格式异常")

        result: list[Security] = []
        for index in range(0, len(self._cells), 4):
            code, name = self._cells[index : index + 2]
            if not fullmatch(r"920\d{3}", code) or not name:
                raise ValueError("证监会股票列表包含无效代码或简称")
            result.append(security_from_code(code, name))
        return result


class TdxMarketDataSource:
    """通过招商证券兼容的通达信协议读取只读行情。"""

    # TDX 协议单次请求最多返回 800 根 K 线，超过此值需要分页获取。
    MAX_BARS_PER_REQUEST = 800

    def __init__(
        self,
        hosts: Iterable[tuple[str, int]] = DEFAULT_HOSTS,
        workers: int = 6,
        timeout: float = 3.0,
    ) -> None:
        self._candidate_hosts = tuple(hosts)
        self._workers = max(1, min(workers, 12))
        self._timeout = timeout
        self._available_hosts: tuple[tuple[str, int], ...] | None = None

    def _new_api(self) -> TdxHq_API:
        return TdxHq_API(heartbeat=True, auto_retry=True, raise_exception=True)

    @staticmethod
    def _safe_disconnect(api: TdxHq_API) -> None:
        """兼容 Windows 下未连接套接字断开时产生的二次异常。"""
        try:
            api.disconnect()
        except Exception:
            pass

    def _probe_host(
        self, host: tuple[str, int]
    ) -> tuple[float, tuple[str, int]] | None:
        api = self._new_api()
        started = time.perf_counter()
        try:
            connected = api.connect(*host, time_out=self._timeout)
            if connected and int(api.get_security_count(0) or 0) > 0:
                return time.perf_counter() - started, host
        except Exception:
            return None
        finally:
            self._safe_disconnect(api)
        return None

    def available_hosts(self) -> tuple[tuple[str, int], ...]:
        """并发探测候选节点，并按响应速度排序。"""
        if self._available_hosts is not None:
            return self._available_hosts

        with ThreadPoolExecutor(
            max_workers=len(self._candidate_hosts)
        ) as executor:
            results = list(executor.map(self._probe_host, self._candidate_hosts))
        successful = sorted((item for item in results if item), key=lambda item: item[0])
        if not successful:
            raise MarketDataError("无法连接行情服务器，请检查网络后重试")
        self._available_hosts = tuple(item[1] for item in successful)
        return self._available_hosts

    def _connect(self, host: tuple[str, int] | None = None) -> TdxHq_API:
        selected = host or self.available_hosts()[0]
        api = self._new_api()
        try:
            if api.connect(*selected, time_out=self._timeout):
                return api
        except Exception as exc:
            self._safe_disconnect(api)
            raise MarketDataError(f"行情服务器连接失败：{selected[0]}") from exc
        self._safe_disconnect(api)
        raise MarketDataError(f"行情服务器连接失败：{selected[0]}")

    @staticmethod
    def _get_security_list(
        api: TdxHq_API, market: int, start: int
    ) -> list[OrderedDict]:
        parser = SafeGetSecurityList(api.client, lock=api.lock)
        parser.setParams(market, start)
        return parser.call_api()

    def _get_csrc_page(self, page: int) -> CsrcSecurityListParser:
        """读取一页证监会北交所主体信息，并限制响应大小。"""
        suffix = "" if page == 1 else f"_{page}"
        request = Request(
            CSRC_BJ_LIST_URL.format(suffix=suffix),
            headers={"User-Agent": CSRC_USER_AGENT},
        )
        with urlopen(request, timeout=max(10.0, self._timeout)) as response:
            body = response.read(CSRC_LIST_MAX_BYTES + 1)
        if len(body) > CSRC_LIST_MAX_BYTES:
            raise MarketDataError("证监会股票列表响应过大")

        parser = CsrcSecurityListParser()
        parser.feed(body.decode("utf-8"))
        parser.close()
        return parser

    def _find_missing_bj_codes(
        self,
        known_codes: set[str],
        missing_count: int,
    ) -> list[str]:
        """通过日 K 探测证监会分页遗漏但仍可交易的北交所代码。"""
        probes = [
            security_from_code(f"920{number:03d}")
            for number in range(1000)
            if f"920{number:03d}" not in known_codes
        ]
        found: dict[str, str] = {}
        for security, bars in self.iter_bars(probes, period=1):
            if bars:
                found[security.code] = bars[-1].trade_date

        if len(found) == missing_count:
            return sorted(found)
        if not found:
            return []

        # 若行情服务器还保留退市代码，只接受最近交易日仍有行情的候选代码。
        latest_date = max(found.values())
        return sorted(
            code for code, trade_date in found.items()
            if trade_date == latest_date
        )

    def _get_missing_bj_name(self, code: str) -> str:
        """为证监会漏页代码读取简称，并严格校验返回证券代码。"""
        request = Request(
            EASTMONEY_BJ_QUOTE_URL.format(code=code),
            headers={
                "User-Agent": CSRC_USER_AGENT,
                "Referer": "https://quote.eastmoney.com/",
            },
        )
        with urlopen(request, timeout=max(10.0, self._timeout)) as response:
            body = response.read(EASTMONEY_QUOTE_MAX_BYTES + 1)
        if len(body) > EASTMONEY_QUOTE_MAX_BYTES:
            raise MarketDataError("北交所证券简称响应过大")

        try:
            payload = json.loads(body.decode("utf-8"))
            data = payload["data"]
            returned_code = str(data["f57"])
            name = str(data["f58"]).strip()
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise MarketDataError("北交所证券简称响应格式异常") from exc
        if (
            returned_code != code
            or not name
            or name in {"-", "--"}
            or len(name) > 32
            or any(ord(character) < 32 for character in name)
        ):
            raise MarketDataError(f"北交所证券 {code} 的简称无效")
        return name

    def _get_bj_securities(self) -> list[Security]:
        """读取完整北交所列表，并补偿官网静态分页的短暂漏项。"""
        first_page = self._get_csrc_page(1)
        expected = first_page.security_count
        if expected is None or expected <= 0:
            raise MarketDataError("证监会股票列表缺少证券总数")

        page_count = first_page.page_count
        securities: dict[str, Security] = {}
        for attempt in range(CSRC_LIST_RETRIES):
            if attempt == 0:
                parsers = [first_page]
                pages = range(2, page_count + 1)
            else:
                parsers = []
                pages = range(1, page_count + 1)

            with ThreadPoolExecutor(
                max_workers=min(self._workers, page_count)
            ) as executor:
                parsers.extend(executor.map(self._get_csrc_page, pages))

            for parser in parsers:
                if parser.security_count is None or parser.security_count <= 0:
                    raise MarketDataError("证监会股票列表缺少证券总数")
                expected = max(expected, parser.security_count)
                page_count = max(page_count, parser.page_count)
                for security in parser.securities():
                    previous = securities.get(security.code)
                    if previous is not None and previous.name != security.name:
                        raise MarketDataError(
                            f"北交所股票 {security.code} 的简称不一致"
                        )
                    securities[security.code] = security

            if len(securities) == expected:
                break
            if len(securities) > expected:
                raise MarketDataError("证监会股票列表数量超过其公布总数")

        missing_count = expected - len(securities)
        missing_codes = self._find_missing_bj_codes(
            set(securities),
            missing_count,
        )
        if missing_count > 0 and len(missing_codes) != missing_count:
            raise MarketDataError(
                f"北交所股票列表不完整：取得 {len(securities)}/{expected}"
            )
        for code in missing_codes:
            securities[code] = security_from_code(
                code,
                self._get_missing_bj_name(code),
            )
        if len(securities) >= expected:
            return sorted(securities.values(), key=lambda item: item.code)

        raise MarketDataError(
            f"北交所股票列表不完整：取得 {len(securities)}/{expected}"
        )

    def get_securities(self) -> list[Security]:
        """获取并过滤全部沪深京 A 股列表。"""
        api = self._connect()
        securities: dict[tuple[int, str], Security] = {}
        try:
            for market in (0, 1):
                count = int(api.get_security_count(market) or 0)
                for start in range(0, count, 1000):
                    rows = self._get_security_list(api, market, start) or []
                    for row in rows:
                        code = str(row.get("code", ""))
                        if not is_a_share(market, code):
                            continue
                        base = security_from_code(code, str(row.get("name", code)).strip())
                        securities[(market, code)] = base
            for base in self._get_bj_securities():
                securities[(base.market, base.code)] = base
        except Exception as exc:
            raise MarketDataError("获取 A 股列表失败") from exc
        finally:
            self._safe_disconnect(api)
        return sorted(securities.values(), key=lambda item: (item.market, item.code))

    @staticmethod
    def _convert_bars(rows: list[dict], period: int) -> list[KLine]:
        bars: list[KLine] = []
        for row in rows[-period:]:
            try:
                values = (
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                )
                volume = float(row["vol"])
                trade_date = str(row["datetime"])[:10]
            except (KeyError, TypeError, ValueError):
                continue
            if (
                min(values) <= 0
                or volume < 0
                or not math.isfinite(volume)
                or not trade_date
            ):
                continue
            bars.append(
                KLine(
                    trade_date,
                    values[0],
                    values[1],
                    values[2],
                    values[3],
                    volume,
                )
            )
        return bars

    def get_bars(self, security: Security, period: int = 20) -> list[KLine]:
        """获取单只股票最近若干根日 K 线。"""
        api = self._connect()
        try:
            rows = self._fetch_bars_paged(api, security, period)
            return self._convert_bars(rows, period)
        except Exception as exc:
            raise MarketDataError(f"获取 {security.code} 的 K 线失败") from exc
        finally:
            self._safe_disconnect(api)

    def _fetch_bars_paged(
        self, api: TdxHq_API, security: Security, period: int
    ) -> list[dict]:
        """分页获取 K 线，规避 TDX 协议单次 800 根的上限。"""
        if period <= self.MAX_BARS_PER_REQUEST:
            return api.get_security_bars(
                9, security.market, security.code, 0, period
            ) or []
        rows: list[dict] = []
        for start in range(0, period, self.MAX_BARS_PER_REQUEST):
            count = min(self.MAX_BARS_PER_REQUEST, period - start)
            batch = api.get_security_bars(
                9, security.market, security.code, start, count
            ) or []
            if not batch:
                break
            rows.extend(batch)
        return rows

    def iter_bars(
        self,
        securities: Iterable[Security],
        period: int = 20,
        progress: Callable[[int, int, int], None] | None = None,
    ) -> Iterator[tuple[Security, list[KLine] | None]]:
        """使用独立线程连接批量获取日 K，避免在线程间共享连接。"""
        items = list(securities)
        hosts = self.available_hosts()
        local = threading.local()
        api_lock = threading.Lock()
        opened_apis: list[TdxHq_API] = []
        host_cursor = 0

        def thread_api() -> TdxHq_API:
            nonlocal host_cursor
            api = getattr(local, "api", None)
            if api is not None:
                return api
            with api_lock:
                host = hosts[host_cursor % len(hosts)]
                host_cursor += 1
            api = self._connect(host)
            local.api = api
            with api_lock:
                opened_apis.append(api)
            return api

        def fetch(security: Security) -> list[KLine] | None:
            for _ in range(2):
                try:
                    rows = self._fetch_bars_paged(
                        thread_api(), security, period
                    )
                    return self._convert_bars(rows, period)
                except Exception:
                    api = getattr(local, "api", None)
                    if api is not None:
                        self._safe_disconnect(api)
                        local.api = None
            return None

        completed = 0
        succeeded = 0
        try:
            with ThreadPoolExecutor(max_workers=self._workers) as executor:
                futures: dict[Future[list[KLine] | None], Security] = {
                    executor.submit(fetch, item): item for item in items
                }
                for future in as_completed(futures):
                    security = futures[future]
                    bars = future.result()
                    completed += 1
                    if bars is not None:
                        succeeded += 1
                    if progress:
                        progress(completed, len(items), succeeded)
                    yield security, bars
        finally:
            for api in opened_apis:
                self._safe_disconnect(api)
