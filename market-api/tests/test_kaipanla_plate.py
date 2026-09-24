import unittest

from app.datasources.kaipanla_plate import parse_rank_row


class KaipanlaPlateTests(unittest.TestCase):
    def test_parse_rank_row_keeps_score_rate_and_net_money(self):
        row = parse_rank_row([
            "801045", "医药", 12646, 3.601, 0.642, 209988067039,
            5607782087, 37829868513, -32222086426,
        ])
        self.assertEqual(row["plate_code"], "801045")
        self.assertEqual(row["score"], 12646)
        self.assertEqual(row["rate"], 3.601)
        self.assertEqual(row["money_leader"], 5607782087)
        self.assertEqual(row["trade_money"], 209988067039)
        self.assertEqual(row["money_leader_buy"] + row["money_leader_sell"], row["money_leader"])
        self.assertIsNone(row["source_as_of"])

    def test_parse_rank_row_rejects_short_item(self):
        self.assertIsNone(parse_rank_row(["801045", "医药", 1]))

    def test_members_snapshot_has_top_plate(self):
        from app.services import plate_flow
        plates = plate_flow.load_members()
        self.assertIn("803023", plates)
        members = plate_flow.plate_members("803023", "2026-09-22")
        self.assertGreater(len(members), 20)


if __name__ == "__main__":
    unittest.main()
