from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Iterable

from .models import FavoritePattern, KLine, Security


class KLineStorage:
    """使用 SQLite 保存证券列表和最近日 K 缓存。"""

    def __init__(self, database_path: str | Path) -> None:
        path = Path(database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS securities (
                market INTEGER NOT NULL,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                exchange TEXT NOT NULL,
                PRIMARY KEY (market, code)
            );

            CREATE TABLE IF NOT EXISTS bars (
                market INTEGER NOT NULL,
                code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                open REAL NOT NULL CHECK (open > 0),
                high REAL NOT NULL CHECK (high > 0),
                low REAL NOT NULL CHECK (low > 0),
                close REAL NOT NULL CHECK (close > 0),
                volume REAL NOT NULL CHECK (volume >= 0),
                PRIMARY KEY (market, code, trade_date),
                FOREIGN KEY (market, code)
                    REFERENCES securities (market, code) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS live_bars (
                market INTEGER NOT NULL,
                code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                open REAL NOT NULL CHECK (open > 0),
                high REAL NOT NULL CHECK (high > 0),
                low REAL NOT NULL CHECK (low > 0),
                close REAL NOT NULL CHECK (close > 0),
                volume REAL NOT NULL CHECK (volume >= 0),
                PRIMARY KEY (market, code),
                FOREIGN KEY (market, code)
                    REFERENCES securities (market, code) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS favorite_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL CHECK (
                    length(name) BETWEEN 1 AND 120
                ),
                market INTEGER NOT NULL,
                code TEXT NOT NULL,
                security_name TEXT NOT NULL,
                exchange TEXT NOT NULL,
                selection_start INTEGER NOT NULL CHECK (
                    selection_start >= 0
                ),
                selection_count INTEGER NOT NULL CHECK (
                    selection_count > 0
                ),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS favorite_bars (
                favorite_id INTEGER NOT NULL,
                position INTEGER NOT NULL CHECK (position >= 0),
                trade_date TEXT NOT NULL,
                open REAL NOT NULL CHECK (open > 0),
                high REAL NOT NULL CHECK (high > 0),
                low REAL NOT NULL CHECK (low > 0),
                close REAL NOT NULL CHECK (close > 0),
                volume REAL NOT NULL CHECK (volume >= 0),
                PRIMARY KEY (favorite_id, position),
                FOREIGN KEY (favorite_id)
                    REFERENCES favorite_patterns (id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(bars)")
        }
        if "volume" not in columns:
            self._connection.execute(
                """
                ALTER TABLE bars
                ADD COLUMN volume REAL NOT NULL DEFAULT 0
                CHECK (volume >= 0)
                """
            )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> KLineStorage:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def replace_securities(self, securities: Iterable[Security]) -> None:
        """刷新证券基础信息，同时保留仍在列表中的 K 线。"""
        items = list(securities)
        valid_keys = {(item.market, item.code) for item in items}
        with self._connection:
            self._connection.executemany(
                """
                INSERT INTO securities (market, code, name, exchange)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (market, code) DO UPDATE SET
                    name = excluded.name,
                    exchange = excluded.exchange
                """,
                [
                    (item.market, item.code, item.name, item.exchange)
                    for item in items
                ],
            )
            old_keys = self._connection.execute(
                "SELECT market, code FROM securities"
            ).fetchall()
            removed = [
                (row["market"], row["code"])
                for row in old_keys
                if (row["market"], row["code"]) not in valid_keys
            ]
            self._connection.executemany(
                "DELETE FROM securities WHERE market = ? AND code = ?", removed
            )

    def save_bar_batch(
        self, entries: Iterable[tuple[Security, list[KLine]]]
    ) -> None:
        """原子替换一批股票的 K 线，防止留下同股票的过期记录。"""
        items = list(entries)
        if not items:
            return
        with self._connection:
            self._connection.executemany(
                "DELETE FROM bars WHERE market = ? AND code = ?",
                [(security.market, security.code) for security, _ in items],
            )
            rows = [
                (
                    security.market,
                    security.code,
                    bar.trade_date,
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                )
                for security, bars in items
                for bar in bars
            ]
            self._connection.executemany(
                """
                INSERT INTO bars (
                    market, code, trade_date, open, high, low, close, volume
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def set_metadata(self, key: str, value: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO metadata (key, value) VALUES (?, ?)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_metadata(self, key: str) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def security_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM securities"
        ).fetchone()
        return int(row["count"])

    def get_security(self, code: str) -> Security | None:
        row = self._connection.execute(
            """
            SELECT market, code, name, exchange
            FROM securities
            WHERE code = ?
            LIMIT 1
            """,
            (code,),
        ).fetchone()
        if not row:
            return None
        return Security(row["market"], row["code"], row["name"], row["exchange"])

    def load_securities(self) -> list[Security]:
        """按市场和代码顺序读取本地证券列表。"""
        rows = self._connection.execute(
            """
            SELECT market, code, name, exchange
            FROM securities
            ORDER BY market, code
            """
        ).fetchall()
        return [
            Security(
                row["market"],
                row["code"],
                row["name"],
                row["exchange"],
            )
            for row in rows
        ]

    def replace_live_snapshot(
        self,
        entries: Iterable[tuple[Security, KLine]],
        market_date: str,
        updated_at: str,
    ) -> None:
        """原子替换实时快照，实时数据不写入历史 K 线表。"""
        items = list(entries)
        if not items:
            raise ValueError("实时行情快照不能为空")
        rows = [
            (
                security.market,
                security.code,
                bar.trade_date,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
            )
            for security, bar in items
        ]
        with self._connection:
            self._connection.execute("DELETE FROM live_bars")
            self._connection.executemany(
                """
                INSERT INTO live_bars (
                    market, code, trade_date, open, high, low, close, volume
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._connection.executemany(
                """
                INSERT INTO metadata (key, value) VALUES (?, ?)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value
                """,
                (
                    ("live_market_date", market_date),
                    ("live_updated_at", updated_at),
                ),
            )

    def load_live_bar(self, code: str) -> KLine | None:
        """读取单只股票的最新实时日 K 快照。"""
        row = self._connection.execute(
            """
            SELECT trade_date, open, high, low, close, volume
            FROM live_bars
            WHERE code = ?
            LIMIT 1
            """,
            (code,),
        ).fetchone()
        if not row:
            return None
        return KLine(
            row["trade_date"],
            row["open"],
            row["high"],
            row["low"],
            row["close"],
            row["volume"],
        )

    def load_live_bars(self) -> dict[tuple[int, str], KLine]:
        """读取全市场实时快照，并按市场和代码建立索引。"""
        rows = self._connection.execute(
            """
            SELECT market, code, trade_date, open, high, low, close, volume
            FROM live_bars
            """
        ).fetchall()
        return {
            (row["market"], row["code"]): KLine(
                row["trade_date"],
                row["open"],
                row["high"],
                row["low"],
                row["close"],
                row["volume"],
            )
            for row in rows
        }

    def save_favorite_pattern(
        self,
        name: str,
        security: Security,
        context_bars: Sequence[KLine],
        selection_start: int,
        selection_count: int,
        created_at: str,
    ) -> int:
        """原子保存框选形态及其指标预热 K 线。"""
        bars = list(context_bars)
        selection_end = selection_start + selection_count
        if (
            not bars
            or selection_start < 0
            or selection_count <= 0
            or selection_end > len(bars)
        ):
            raise ValueError("收藏的 K 线区间无效")

        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO favorite_patterns (
                    name, market, code, security_name, exchange,
                    selection_start, selection_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    security.market,
                    security.code,
                    security.name,
                    security.exchange,
                    selection_start,
                    selection_count,
                    created_at,
                ),
            )
            favorite_id = cursor.lastrowid
            if favorite_id is None:
                raise sqlite3.DatabaseError("收藏记录编号生成失败")
            self._connection.executemany(
                """
                INSERT INTO favorite_bars (
                    favorite_id, position, trade_date,
                    open, high, low, close, volume
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        favorite_id,
                        position,
                        bar.trade_date,
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                    )
                    for position, bar in enumerate(bars)
                ],
            )
        return int(favorite_id)

    def load_favorite_patterns(self) -> list[FavoritePattern]:
        """按收藏时间倒序读取全部 K 线收藏。"""
        rows = self._connection.execute(
            """
            SELECT
                p.id, p.name, p.market, p.code, p.security_name,
                p.exchange, p.selection_start, p.selection_count,
                p.created_at, b.position, b.trade_date,
                b.open, b.high, b.low, b.close, b.volume
            FROM favorite_patterns AS p
            JOIN favorite_bars AS b ON b.favorite_id = p.id
            ORDER BY p.created_at DESC, p.id DESC, b.position
            """
        ).fetchall()
        grouped: dict[int, tuple[sqlite3.Row, list[KLine]]] = {}
        for row in rows:
            favorite_id = int(row["id"])
            if favorite_id not in grouped:
                grouped[favorite_id] = (row, [])
            grouped[favorite_id][1].append(
                KLine(
                    row["trade_date"],
                    row["open"],
                    row["high"],
                    row["low"],
                    row["close"],
                    row["volume"],
                )
            )
        return [
            FavoritePattern(
                favorite_id,
                str(row["name"]),
                Security(
                    row["market"],
                    str(row["code"]),
                    str(row["security_name"]),
                    str(row["exchange"]),
                ),
                tuple(bars),
                row["selection_start"],
                row["selection_count"],
                str(row["created_at"]),
            )
            for favorite_id, (row, bars) in grouped.items()
        ]

    def load_favorite_pattern(
        self,
        favorite_id: int,
    ) -> FavoritePattern | None:
        """按编号读取一个 K 线收藏。"""
        rows = self._connection.execute(
            """
            SELECT
                p.id, p.name, p.market, p.code, p.security_name,
                p.exchange, p.selection_start, p.selection_count,
                p.created_at, b.position, b.trade_date,
                b.open, b.high, b.low, b.close, b.volume
            FROM favorite_patterns AS p
            JOIN favorite_bars AS b ON b.favorite_id = p.id
            WHERE p.id = ?
            ORDER BY b.position
            """,
            (favorite_id,),
        ).fetchall()
        if not rows:
            return None
        first = rows[0]
        bars = tuple(
            KLine(
                row["trade_date"],
                row["open"],
                row["high"],
                row["low"],
                row["close"],
                row["volume"],
            )
            for row in rows
        )
        return FavoritePattern(
            int(first["id"]),
            str(first["name"]),
            Security(
                first["market"],
                str(first["code"]),
                str(first["security_name"]),
                str(first["exchange"]),
            ),
            bars,
            first["selection_start"],
            first["selection_count"],
            str(first["created_at"]),
        )

    def delete_favorite_pattern(self, favorite_id: int) -> bool:
        """删除收藏及其 K 线，返回是否确实删除了记录。"""
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM favorite_patterns WHERE id = ?",
                (favorite_id,),
            )
        return cursor.rowcount > 0

    def load_security_bars(
        self,
        code: str,
        period: int,
    ) -> tuple[Security, list[KLine]] | None:
        """按时间正序读取单只股票最近指定根数的缓存 K 线。"""
        if period <= 0:
            raise ValueError("K 线根数必须大于零")
        rows = self._connection.execute(
            """
            SELECT
                s.market, s.code, s.name, s.exchange,
                b.trade_date, b.open, b.high, b.low, b.close, b.volume
            FROM securities AS s
            JOIN bars AS b
              ON b.market = s.market AND b.code = s.code
            WHERE s.code = ?
            ORDER BY b.trade_date DESC
            LIMIT ?
            """,
            (code, period),
        ).fetchall()
        if not rows:
            return None

        security = Security(
            rows[0]["market"],
            rows[0]["code"],
            rows[0]["name"],
            rows[0]["exchange"],
        )
        bars = [
            KLine(
                row["trade_date"],
                row["open"],
                row["high"],
                row["low"],
                row["close"],
                row["volume"],
            )
            for row in reversed(rows)
        ]
        return security, bars

    def load_universe(
        self, period: int = 20
    ) -> list[tuple[Security, list[KLine]]]:
        """读取至少具有指定根数 K 线的全部股票。"""
        if period <= 0:
            raise ValueError("K 线根数必须大于零")
        # 对每只股票先通过主键索引定位区间起点，再读取最近 period 根，
        # 避免窗口函数扫描并临时排序整张 K 线表。
        rows = self._connection.execute(
            """
            SELECT
                s.market, s.code, s.name, s.exchange,
                b.trade_date, b.open, b.high, b.low, b.close, b.volume
            FROM securities AS s
            JOIN bars AS b
              ON b.market = s.market AND b.code = s.code
            WHERE b.trade_date >= (
                SELECT recent.trade_date
                FROM bars AS recent
                WHERE recent.market = s.market
                  AND recent.code = s.code
                ORDER BY recent.trade_date DESC
                LIMIT 1 OFFSET ?
            )
            ORDER BY s.market, s.code, b.trade_date
            """,
            (period - 1,),
        ).fetchall()

        grouped: dict[tuple[int, str], tuple[Security, list[KLine]]] = {}
        for row in rows:
            key = (row["market"], row["code"])
            if key not in grouped:
                grouped[key] = (
                    Security(
                        row["market"], row["code"], row["name"], row["exchange"]
                    ),
                    [],
                )
            grouped[key][1].append(
                KLine(
                    row["trade_date"],
                    row["open"],
                    row["high"],
                    row["low"],
                    row["close"],
                    row["volume"],
                )
            )
        return [
            (security, bars[-period:])
            for security, bars in grouped.values()
            if len(bars) >= period
        ]
