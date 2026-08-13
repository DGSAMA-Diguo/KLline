from __future__ import annotations

import argparse
import sys

from kline_agent.service import AgentError, KLineAgent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="查找最近 20 根日 K 线形态最相似的 10 只 A 股"
    )
    parser.add_argument(
        "query",
        nargs="?",
        help='股票代码或中文问题，例如："查找和 600519 K 线相似的股票"',
    )
    return parser


def show_progress(completed: int, total: int, succeeded: int) -> None:
    """在同一行显示全市场 K 线同步进度。"""
    percent = completed * 100 / total if total else 100
    print(
        f"\r正在同步全市场日 K：{completed}/{total} "
        f"({percent:5.1f}%)，成功 {succeeded}",
        end="",
        flush=True,
    )
    if completed == total:
        print()


def main() -> int:
    args = build_parser().parse_args()
    query = args.query
    if not query:
        try:
            query = input("请输入股票代码或问题：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return 130

    try:
        with KLineAgent() as agent:
            target, data_date, results, offline = agent.search(query, show_progress)
    except AgentError as exc:
        print(f"查询失败：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消")
        return 130

    source_text = "本地缓存" if offline else "最新行情"
    print(
        f"\n目标：{target.name}（{target.symbol}）"
        f"  数据日期：{data_date}  数据来源：{source_text}"
    )
    print(
        "排名  股票                         综合      K线"
        "      均线    成交量     MACD"
    )
    print("-" * 91)
    for rank, item in enumerate(results, start=1):
        label = f"{item.security.name}（{item.security.symbol}）"
        print(
            f"{rank:>2}    {label:<26} {item.score:>6.2f}%"
            f"  {item.kline_score or 0:>6.2f}%"
            f"  {item.moving_average_score or 0:>6.2f}%"
            f"  {item.volume_score or 0:>6.2f}%"
            f"  {item.macd_score or 0:>6.2f}%"
        )
    print(
        "\n综合相似度默认按 K 线、成交量和 MACD "
        "三项等权评分，均线分仅供参考，不构成投资建议。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
