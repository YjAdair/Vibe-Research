"""龙虎榜席位明细：采集映射、发布、detail/trader history 契约。"""
import unittest
from unittest import mock

from app.services import lhb


RAW_BUY = {
    "SECURITY_CODE": "000017",
    "TRADE_DATE": "2026-09-11 00:00:00",
    "OPERATEDEPT_CODE": "10484371",
    "OPERATEDEPT_NAME": "东方财富证券股份有限公司拉萨团结路第一证券营业部",
    "EXPLANATION": "日跌幅偏离值达到7%的前5只证券",
    "BUY": 7761850,
    "SELL": 5214481.4,
    "NET": 2547368.6,
    "RISE_PROBABILITY_3DAY": 32.54,
}
RAW_SELL = {
    **RAW_BUY,
    "OPERATEDEPT_CODE": "10000673606",
    "OPERATEDEPT_NAME": "国泰海通证券股份有限公司东莞总部一路证券营业部",
    "BUY": 1200000,
    "SELL": 9800000,
    "NET": -8600000,
}


class TestLhbSeats(unittest.IsolatedAsyncioTestCase):
    async def test_seat_row_mapping(self):
        row = lhb._seat_row(RAW_BUY, 1)
        self.assertEqual(row["side"], 1)
        self.assertEqual(row["dept_code"], "10484371")
        self.assertEqual(row["trade_date"], "2026-09-11")
        self.assertEqual(row["buy_amount"], 7761850)
        self.assertEqual(row["win_rate_3d"], 32.54)

    async def test_rank_by_amount_desc(self):
        small = dict(RAW_BUY, OPERATEDEPT_CODE="1", BUY=100)
        big = dict(RAW_BUY, OPERATEDEPT_CODE="2", BUY=999)
        rows = lhb._rank_seats([lhb._seat_row(small, 1), lhb._seat_row(big, 1)])
        by_code = {r["dept_code"]: r for r in rows}
        self.assertEqual(by_code["2"]["rank"], 1)
        self.assertEqual(by_code["1"]["rank"], 2)

    async def test_collect_seats_paginates_both_sides(self):
        async def fake_details(date, side, page=1, size=200):
            self.assertEqual(date, "2026-09-11")
            if page == 1:
                data = [RAW_BUY] if side == "buy" else [RAW_SELL]
                return {"pages": 2, "data": data + [dict(RAW_BUY, OPERATEDEPT_CODE="p2" + side)]}
            return {"pages": 2, "data": []}

        with mock.patch.object(lhb.eastmoney, "lhb_seat_details", fake_details):
            rows = await lhb.collect_seats("2026-09-11")
        self.assertEqual(len(rows), 4)
        self.assertEqual({r["side"] for r in rows}, {1, 2})

    async def test_collect_seats_empty_raises(self):
        async def fake_empty(date, side, page=1, size=200):
            return {"pages": 0, "data": []}

        with mock.patch.object(lhb.eastmoney, "lhb_seat_details", fake_empty):
            with self.assertRaises(ValueError):
                await lhb.collect_seats("2026-09-11")

    async def test_detail_contract(self):
        seats = [lhb._seat_row(RAW_BUY, 1), lhb._seat_row(RAW_SELL, 2)]
        snap = {"items": [{
            "stock_code": "000017", "close_price": 7.5, "change_rate": -9.964,
            "turnover_rate": 16.76, "buy_amount": 34940718.44,
            "explanation": "日跌幅偏离值达到7%的前5只证券",
        }]}
        with mock.patch.object(lhb, "published", lambda d: snap), \
             mock.patch.object(lhb.store, "lhb_seats_get", lambda d, c=None: lhb._rank_seats(seats)):
            out = lhb.stock_detail("2026-09-11", "000017")
        self.assertIn("detail", out)
        self.assertIn("traders", out)
        self.assertEqual(out["detail"]["join_num"], 2)
        self.assertEqual(len(out["traders"]), 2)
        buy = [t for t in out["traders"] if t["type"] == 1][0]
        self.assertEqual(buy["trader_id"], "10484371")
        self.assertEqual(buy["rank"], 1)

    async def test_trader_history_contract(self):
        rows = [dict(lhb._seat_row(RAW_BUY, 1), rank=1)]
        snap = {"items": [{
            "stock_code": "000017", "stock_name": "深中华A", "close_price": 7.5,
            "change_rate": -9.964, "turnover_rate": 16.76,
            "buy_amount": 34940718.44, "sell_amount": 56411199.56,
        }]}
        with mock.patch.object(lhb.store, "lhb_seats_by_dept",
                               lambda code, page, per_page, stock=None: (rows, 1)), \
             mock.patch.object(lhb, "published", lambda d: snap):
            out = lhb.trader_history("10484371", 1, 20)
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["list"][0]["stock_code"], "000017")
        row = out["list"][0]
        self.assertEqual(row["stock_name"], "深中华A")
        self.assertEqual(row["change_rate"], -9.964)
        self.assertEqual(row["turnover_amount"], 34940718.44 + 56411199.56)

    async def test_trader_history_without_snapshot(self):
        """lhb 快照缺失时 join 字段优雅降级为 None，不抛错。"""
        rows = [dict(lhb._seat_row(RAW_BUY, 1), rank=1)]
        with mock.patch.object(lhb.store, "lhb_seats_by_dept",
                               lambda code, page, per_page, stock=None: (rows, 1)), \
             mock.patch.object(lhb, "published", lambda d: None):
            out = lhb.trader_history("10484371", 1, 20)
        self.assertEqual(out["list"][0]["stock_name"], None)
        self.assertEqual(out["list"][0]["turnover_amount"], None)


if __name__ == "__main__":
    unittest.main()
