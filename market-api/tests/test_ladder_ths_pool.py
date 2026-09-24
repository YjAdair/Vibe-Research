"""Minimal checks for THS→EM ladder pool mapping."""
from app.services.pools import ths_row_as_em, _parse_high_days, ladder_pool_rows


def demo():
    assert _parse_high_days("6天4板") == (6, 4)
    assert _parse_high_days("首板") == (0, 0)
    row = ths_row_as_em({
        "code": "000001", "name": "平安银行", "lbc": 2,
        "high_days": "2天2板", "first_limit_up_time": "09:35",
        "open_num": 1, "limit_up_type": "换手板", "amount": 1e8,
    })
    assert row["c"] == "000001" and row["lbc"] == 2 and row["fbt"] == 93500
    assert row["zbc"] == 0 and row["zttj"] == {"days": 2, "ct": 2}
    br = ths_row_as_em({"code": "000002", "name": "万科A", "lbc": 1,
                       "high_days": "首板", "first_limit_up_time": "10:00"}, broken=True)
    assert br["lbc"] == 0
    # no crash when nothing published for far date
    up, broken, src = ladder_pool_rows("1999-01-01")
    assert src is None and up == [] and broken == []
    print("ok")


if __name__ == "__main__":
    demo()
