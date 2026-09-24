"""A 股代码归一化工具。"""

from __future__ import annotations


def normalize_code(code: str) -> str:
    """把各种写法归一成 6 位纯数字。"""
    c = code.strip().lower()
    for prefix in ("sh", "sz", "bj", "sh.", "sz.", "bj."):
        if c.startswith(prefix):
            c = c[len(prefix):]
            break
    c = c.split(".")[0]
    return c.zfill(6)


def market_of(code: str) -> str:
    """返回 sh / sz / bj。"""
    c = normalize_code(code)
    if c.startswith(("60", "68", "90", "51", "58", "56", "11", "50")):
        return "sh"
    if c.startswith(("00", "30", "12", "15", "16", "18", "39")):
        return "sz"
    if c.startswith(("43", "83", "87", "88", "92")):
        return "bj"
    return "sz"


def to_tencent_symbol(code: str) -> str:
    """转腾讯 symbol，如 sh600000 / sz000001 / bj920895。"""
    return f"{market_of(code)}{normalize_code(code)}"


def to_sina_symbol(code: str) -> str:
    """转新浪 symbol。"""
    return to_tencent_symbol(code)


def to_eastmoney_secid(code: str) -> str:
    """转东财 secid，如 1.600000 / 0.000001（北交所走 0）。"""
    m = market_of(code)
    prefix = "1" if m == "sh" else "0"
    return f"{prefix}.{normalize_code(code)}"


def to_eastmoney_rank_sc(code: str) -> str:
    """东财股吧人气 srcSecurityCode，如 SH600000 / SZ000001 / BJ920895。"""
    return f"{market_of(code).upper()}{normalize_code(code)}"
