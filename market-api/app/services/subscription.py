"""订阅/付费模块（业务配置，可替换为真实支付）。"""

from __future__ import annotations

from app.core.cache import cache


MODULES = [
    {
        "name": "情绪周期VIP",
        "id": 1,
        "price_per_month": "299",
        "is_visible": 1,
        "code": "sentiment_vip",
        "status": 1,
        "description": "包含完整的情绪周期分析功能，清晰可见的情绪小周期，抓主升躲大面！",
    },
    {
        "name": "题材表格VIP",
        "id": 2,
        "price_per_month": "599",
        "is_visible": 1,
        "code": "topic_kline",
        "status": 1,
        "description": "在题材表格中查看历史题材K线走势，识别核心，把握共振主升。",
    },
    {
        "name": "AI每日盘前",
        "id": 3,
        "price_per_month": "19",
        "is_visible": 1,
        "code": "ai_premarket",
        "status": 1,
        "description": "AI每日盘前报告，挖掘当日机会",
    },
    {
        "name": "涨幅区间VIP",
        "id": 5,
        "price_per_month": "18",
        "is_visible": 1,
        "code": "pct_interval_vip",
        "status": 1,
        "description": "在板块或题材中查看股票多日累计涨幅区间分布",
    },
]


def modules() -> list[dict]:
    key = "payment_modules"
    hit = cache.get(key)
    if hit:
        return hit
    cache.set(key, MODULES, 3600)
    return MODULES
