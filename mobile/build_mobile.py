"""将桌面行情缓存打包成无需依赖的移动端单文件应用。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import sqlite3
import struct
from pathlib import Path


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
PROJECT_DIRECTORY = SCRIPT_DIRECTORY.parent
DEFAULT_DATABASE = PROJECT_DIRECTORY / "data" / "kline_cache.db"
DEFAULT_OUTPUT = SCRIPT_DIRECTORY / "KLineMobile.html"
APPLICATION_ICON = (
    SCRIPT_DIRECTORY
    / "android-app"
    / "res"
    / "mipmap-xxxhdpi"
    / "ic_launcher.png"
)
RECORD = struct.Struct("<Hfffff")


def parse_arguments() -> argparse.Namespace:
    """读取可选的缓存库和输出路径。"""
    parser = argparse.ArgumentParser(
        description="生成单文件离线移动端 K 线应用"
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help="桌面版 SQLite 行情缓存路径",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="生成的移动端 HTML 路径",
    )
    return parser.parse_args()


def load_market_data(
    database_path: Path,
) -> tuple[str, list[str], list[list[object]], bytes]:
    """读取数据库并压缩为适合浏览器随机访问的定长记录。"""
    if not database_path.is_file():
        raise FileNotFoundError(f"行情缓存不存在：{database_path}")

    connection = sqlite3.connect(database_path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError("行情缓存完整性检查失败")

        date_rows = connection.execute(
            "SELECT DISTINCT trade_date FROM bars ORDER BY trade_date"
        ).fetchall()
        dates = [str(row[0]) for row in date_rows]
        if not dates:
            raise RuntimeError("行情缓存中没有 K 线日期")
        if len(dates) > 65535:
            raise RuntimeError("交易日期数量超过移动数据格式上限")
        date_indexes = {trade_date: index for index, trade_date in enumerate(dates)}

        metadata_row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'data_date'"
        ).fetchone()
        market_date = str(metadata_row[0]) if metadata_row else dates[-1]

        rows = connection.execute(
            """
            SELECT
                s.market,
                s.code,
                s.name,
                s.exchange,
                b.trade_date,
                b.open,
                b.high,
                b.low,
                b.close,
                b.volume
            FROM securities AS s
            JOIN bars AS b
              ON b.market = s.market AND b.code = s.code
            ORDER BY s.market, s.code, b.trade_date
            """
        )

        stocks: list[list[object]] = []
        records = bytearray()
        current_key: tuple[int, str] | None = None
        current_identity: tuple[int, str, str, str] | None = None
        current_offset = 0
        current_count = 0
        record_count = 0

        def finish_stock() -> None:
            nonlocal current_identity, current_count
            if current_identity is None or current_count == 0:
                return
            market, code, name, exchange = current_identity
            stocks.append(
                [
                    market,
                    code,
                    name,
                    exchange,
                    current_offset,
                    current_count,
                ]
            )

        for row in rows:
            market = int(row[0])
            code = str(row[1])
            key = (market, code)
            if key != current_key:
                finish_stock()
                current_key = key
                current_identity = (
                    market,
                    code,
                    str(row[2] or code),
                    str(row[3] or ""),
                )
                current_offset = record_count
                current_count = 0

            trade_date = str(row[4])
            values = [float(value) for value in row[5:10]]
            if (
                trade_date not in date_indexes
                or any(not math.isfinite(value) for value in values)
                or min(values[:4]) <= 0
                or values[4] < 0
            ):
                continue
            records.extend(
                RECORD.pack(
                    date_indexes[trade_date],
                    values[0],
                    values[1],
                    values[2],
                    values[3],
                    values[4],
                )
            )
            current_count += 1
            record_count += 1
        finish_stock()

        if not stocks or not records:
            raise RuntimeError("没有可导出的有效股票行情")
        if len(records) != record_count * RECORD.size:
            raise RuntimeError("移动行情记录大小校验失败")
        return market_date, dates, stocks, bytes(records)
    finally:
        connection.close()


def safe_json(value: object) -> str:
    """生成可安全放入脚本标签的紧凑 JSON。"""
    output = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        output.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def build_application(database_path: Path, output_path: Path) -> None:
    """合并页面、样式、逻辑与行情数据并写出最终单文件。"""
    market_date, dates, stocks, records = load_market_data(database_path)
    payload = {
        "version": 1,
        "date": market_date,
        "dates": dates,
        "stocks": stocks,
        "bars": base64.b64encode(records).decode("ascii"),
    }

    template = (SCRIPT_DIRECTORY / "index.template.html").read_text(
        encoding="utf-8"
    )
    styles = (SCRIPT_DIRECTORY / "styles.css").read_text(encoding="utf-8")
    application = (SCRIPT_DIRECTORY / "app.js").read_text(encoding="utf-8")
    if not APPLICATION_ICON.is_file():
        raise FileNotFoundError(f"移动端图标不存在：{APPLICATION_ICON}")
    application_icon = base64.b64encode(APPLICATION_ICON.read_bytes()).decode(
        "ascii"
    )
    output = (
        template.replace("/*__STYLES__*/", styles)
        .replace("__MARKET_DATA__", safe_json(payload))
        .replace("__APP_ICON__", application_icon)
        # 将交互逻辑内嵌到单文件页面，保证离线环境可直接运行。
        .replace("/*__APP_SCRIPT__*/", application)
        .replace("/*__APP_SCRIPT__*/", application)
        .replace("/*__APP_SCRIPT__*/", application)
    )
    if "__MARKET_DATA__" in output or "__APP_ICON__" in output or "/*__" in output:
        raise RuntimeError("移动端模板仍有未替换内容")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output, encoding="utf-8", newline="\n")
    digest = hashlib.sha256(output_path.read_bytes()).hexdigest().upper()
    total_bars = sum(int(stock[5]) for stock in stocks)
    print(f"移动端文件：{output_path.resolve()}")
    print(f"证券数量：{len(stocks)}")
    print(f"K 线数量：{total_bars}")
    print(f"数据日期：{market_date}")
    print(f"文件大小：{output_path.stat().st_size} 字节")
    print(f"SHA256：{digest}")


def main() -> None:
    """执行移动端单文件构建。"""
    arguments = parse_arguments()
    build_application(
        arguments.database.resolve(),
        arguments.output.resolve(),
    )


if __name__ == "__main__":
    main()
