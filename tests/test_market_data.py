import struct
import unittest

from kline_agent.market_data import (
    CsrcSecurityListParser,
    MarketDataError,
    SafeGetSecurityList,
    TdxMarketDataSource,
    is_a_share,
    security_from_code,
)


def make_csrc_page(
    security_count: int,
    page_count: int,
    rows: list[tuple[str, str]],
) -> CsrcSecurityListParser:
    """构造包含指定证券和分页信息的证监会测试页面。"""
    cells = "".join(
        f"<td>{code}</td><td>{name}</td><td>测试公司</td><td></td>"
        for code, name in rows
    )
    html = (
        f'<table class="m-table2">{cells}</table>'
        f"<div>共{security_count}条数据</div>"
        f'<a href="javascript:gotoPage({page_count})">{page_count}</a>'
    )
    parser = CsrcSecurityListParser()
    parser.feed(html)
    parser.close()
    return parser


class MixedSnapshotSource(TdxMarketDataSource):
    """模拟证监会新旧分页混用及通达信补码。"""

    def __init__(self, missing_codes: list[str]) -> None:
        super().__init__(hosts=(), workers=2)
        self._missing_codes = missing_codes
        self.probe_arguments: tuple[set[str], int] | None = None
        self._pages = {
            1: make_csrc_page(3, 2, [("920001", "纬达光电")]),
            2: make_csrc_page(2, 2, [("920002", "万达轴承")]),
        }

    def _get_csrc_page(self, page: int) -> CsrcSecurityListParser:
        return self._pages[page]

    def _find_missing_bj_codes(
        self,
        known_codes: set[str],
        missing_count: int,
    ) -> list[str]:
        self.probe_arguments = (known_codes, missing_count)
        return self._missing_codes

    def _get_missing_bj_name(self, code: str) -> str:
        return {"920003": "测试股份", "920004": "多余股份"}[code]


class MarketDataTests(unittest.TestCase):
    def test_resolves_exchange(self) -> None:
        self.assertEqual(security_from_code("600519").exchange, "SH")
        self.assertEqual(security_from_code("000001").exchange, "SZ")
        self.assertEqual(security_from_code("920001").exchange, "BJ")
        self.assertEqual(security_from_code("920001").market, 2)

    def test_filters_non_a_share_products(self) -> None:
        self.assertTrue(is_a_share(1, "688001"))
        self.assertTrue(is_a_share(0, "300001"))
        self.assertTrue(is_a_share(2, "920001"))
        self.assertFalse(is_a_share(1, "000001"))
        self.assertFalse(is_a_share(0, "920001"))
        self.assertFalse(is_a_share(0, "159001"))
        self.assertFalse(is_a_share(1, "510300"))

    def test_rejects_unknown_code(self) -> None:
        with self.assertRaises(ValueError):
            security_from_code("510300")

    def test_rejects_control_characters_in_security_name(self) -> None:
        with self.assertRaises(ValueError):
            security_from_code("920001", "正常简称\x1b[31m")

    def test_truncated_gbk_name_does_not_break_stock_list(self) -> None:
        # 最后一个 0xC1 是被 8 字节定长字段截断的半个 GBK 字符。
        name = b"\xba\xec\xc0\xfbETF\xc1"
        record = struct.pack(
            "<6sH8s4sBI4s",
            b"161907",
            100,
            name,
            b"\x00" * 4,
            2,
            0,
            b"\x00" * 4,
        )
        parser = SafeGetSecurityList(None)

        rows = parser.parseResponse(struct.pack("<H", 1) + record)

        self.assertEqual(rows[0]["name"], "红利ETF")

    def test_parses_csrc_bj_security_page(self) -> None:
        html = """
        <table class="m-table2 m-table2-1">
          <tr><th>股票代码</th><th>股票简称</th></tr>
          <td><a>920001</a></td><td>纬达光电</td>
          <td>佛山纬达光电材料股份有限公司</td><td></td>
          <tr><td>920002</td><td>万达轴承</td>
          <td>江苏万达特种轴承股份有限公司</td><td></td></tr>
        </table>
        <div>共334条数据</div>
        <a href="javascript:gotoPage(23)">23</a>
        """
        parser = CsrcSecurityListParser()

        parser.feed(html)
        securities = parser.securities()

        self.assertEqual(parser.security_count, 334)
        self.assertEqual(parser.page_count, 23)
        self.assertEqual(
            [(item.market, item.code, item.name) for item in securities],
            [
                (2, "920001", "纬达光电"),
                (2, "920002", "万达轴承"),
            ],
        )

    def test_reconciles_mixed_csrc_snapshots_with_tdx_probe(self) -> None:
        source = MixedSnapshotSource(["920003"])

        securities = source._get_bj_securities()

        self.assertEqual(
            [(item.code, item.name) for item in securities],
            [
                ("920001", "纬达光电"),
                ("920002", "万达轴承"),
                ("920003", "测试股份"),
            ],
        )
        self.assertEqual(
            source.probe_arguments,
            ({"920001", "920002"}, 1),
        )

    def test_supplements_code_when_csrc_declared_count_is_stale(self) -> None:
        source = MixedSnapshotSource(["920003"])
        source._pages = {
            1: make_csrc_page(2, 2, [("920001", "纬达光电")]),
            2: make_csrc_page(2, 2, [("920002", "万达轴承")]),
        }

        securities = source._get_bj_securities()

        self.assertEqual(
            [item.code for item in securities],
            ["920001", "920002", "920003"],
        )
        self.assertEqual(
            source.probe_arguments,
            ({"920001", "920002"}, 0),
        )

    def test_rejects_tdx_probe_count_that_exceeds_official_gap(self) -> None:
        source = MixedSnapshotSource(["920003", "920004"])

        with self.assertRaises(MarketDataError):
            source._get_bj_securities()


if __name__ == "__main__":
    unittest.main()
