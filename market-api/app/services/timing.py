"""风格择时（对齐原站 /v3/api/timing/market/style 与 style2）。

原站接口已下线，规格从 chunk-52b22ba5 逆向：
- style:  逐日 {date, score, score2, timing}; score>600 截断; timing=-1 区间起点,
          timing=0 区间终点(牛市 markArea), 前端按 MA10 与牛市线100 划分。
- style2: 逐日 {date, trade_money_total, pe_ratio, market_cap_total,
          market_cap_circ_total, up_count, up_gt5_count, up_gt5_erver_count,
          turnover_ratio, timing}; 牛市线 2000(亿, 两市成交额)。

数据源设计（全免费）:
- 涨停/首板/连板高度: sentiment_agg kv (日K批量回算, 已有 248 个交易日)
- 涨跌家数/涨>5%家数: minute_samples 收盘采样 (已有 126 个交易日)
- 上证/深证指数日K(成交额): 腾讯行情 kline (任意历史)
- score 模型: 涨停家数(0-40) + 连板高度(0-25) + 赚钱效应(0-20) + 情绪强度(0-15)
  scale 到 0-100 区间, 与原站牛市线100 对齐: >100 牛市, 50-100 震荡, <50 熊市。
"""

from __future__ import annotations

import asyncio

from app.core.cache import cache
from app.core.store import store
from app.datasources import tencent
from app.services.market import trade_days


def _market_score(zt: int, lbgd: int, up_gt5: int, up_ratio: float, zb_rate: float) -> float:
    """市场得分: 0-100 区间。

    - 涨停家数(0-80): 80 家满 40 分
    - 连板高度(0-10板): 10 板满 25 分
    - 赚钱效应: 涨>5% 家数(0-800) 满 20 分
    - 情绪强度: (自然涨停占比 - 炸板率) 归一满 15 分
    """
    p1 = min(zt / 80.0, 1.0) * 40.0
    p2 = min(lbgd / 10.0, 1.0) * 25.0
    p3 = min(up_gt5 / 800.0, 1.0) * 20.0
    strength = max(0.0, min(1.0, up_ratio / 0.5)) - zb_rate
    p4 = max(0.0, strength) * 15.0
    return round(p1 + p2 + p3 + p4, 2)


def _timing_flags(scores: list[float], bull_line: float = 100.0) -> list[int]:
    """timing: MA10 >= bull_line 的连续区间标 1, 区间起点前一日标 -1, 否则 0。

    前端 splitData: timing=-1 开区间, timing=0 收区间(markArea), 与此约定一致。
    MA10 口径与前端一致: 前 9 个点无值(标准 10 日均线), 不用部分均值。
    """
    out = [0] * len(scores)
    ma: list[float | None] = [None] * len(scores)
    for i in range(9, len(scores)):
        ma[i] = sum(scores[i - 9 : i + 1]) / 10.0
    in_bull = False
    for i, m in enumerate(ma):
        bull = m is not None and m >= bull_line
        if bull and not in_bull:
            out[i] = -1
            in_bull = True
        elif not bull and in_bull:
            out[i] = 0
            in_bull = False
        elif bull:
            out[i] = 1
    return out


async def _index_volumes(days: list[str]) -> dict[str, float]:
    """成交量只使用已发布分钟样本的全市场成交额/量，不再现场拉指数日K。"""
    out: dict[str, float] = {}
    for d in days:
        hyphen = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        rows = store.minute_all(hyphen)
        if not rows:
            continue
        last = rows[-1]
        amount = last.get('amount')
        if amount is None:
            continue
        out[d] = round(float(amount) / 1e8, 1)
    return out


def _daily_close_samples(days: list[str]) -> dict[str, dict]:
    """每日收盘采样(minute_samples 最后一条): up_num/up_gt5/amount。"""
    out: dict[str, dict] = {}
    for d in days:
        hyphen = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        rows = store.minute_all(hyphen)
        if not rows:
            continue
        last = rows[-1]
        if last.get("up_num") is None:
            continue
        out[d] = {
            "up_count": int(last.get("up_num") or 0),
            "up_gt5_count": int(
                (last.get("gt_5_7_num") or 0)
                + (last.get("gt_7_10_num") or 0)
                + (last.get("gt_10_20_num") or 0)
            ),
        }
    return out


async def market_style(date1: str | None = None, days_back: int = 250) -> list[dict]:
    """风格择时主序列: [{date, score, score2, timing}]。"""
    key = f"timing_style:{date1 or 'latest'}:{days_back}"
    hit = cache.get(key)
    if hit:
        return hit

    cutoff = date1.replace("-", "") if date1 else None
    trading = await trade_days(days_back + 10)
    if cutoff:
        trading = [d for d in trading if d >= cutoff]
    trading = trading[-days_back:]

    samples = _daily_close_samples(trading)
    rows: list[dict] = []
    for d in trading:
        from app.services import pools as _pools
        agg = store.kv_get("sentiment_agg:" + d) or {}
        s = samples.get(d) or {}
        iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        up = _pools.published('up', iso)
        if up is not None:
            pool = [x for x in up.get('pool') or [] if str(x.get('c','')).startswith(('0','3','6'))]
            zt = len(pool)
            first = sum(1 for x in pool if int(x.get('lbc') or 1) == 1)
            lbgd = max((int(x.get('lbc') or 0) for x in pool), default=0)
        else:
            zt = int(agg.get("zt") or 0)
            first = int(agg.get("first") or 0)
            lbc = agg.get("lbc") or []
            lbgd = max((int(x) for x in lbc), default=0)
        up_gt5 = s.get("up_gt5_count", 0)
        up_count = s.get("up_count", 0)
        up_ratio = (first / zt) if zt else 0.0
        broken = _pools.published('broken', iso)
        if broken is not None:
            zb = len([x for x in broken.get('pool') or [] if str(x.get('c') or x.get('code') or '').startswith(('0','3','6'))])
        else:
            zb = store.kv_get("zb_num:" + d)
        zb_rate = (zb / (zb + zt)) if isinstance(zb, (int, float)) and (zb + zt) > 0 else 0.0
        score = _market_score(zt, lbgd, up_gt5, up_ratio, zb_rate)
        score2 = _market_score(zt, lbgd, up_count, up_ratio, zb_rate)
        rows.append({
            "date": f"{d[:4]}-{d[4:6]}-{d[6:]}",
            "score": score,
            "score2": score2,
            "zt": zt,
            "lbgd": lbgd,
        })

    flags = _timing_flags([r["score"] for r in rows])
    for r, f in zip(rows, flags):
        r["timing"] = f

    cache.set(key, rows, 6 * 3600)
    return rows


async def market_style2(date1: str | None = None, days_back: int = 250) -> list[dict]:
    """市场结构周期2: 成交额/涨跌家数序列。"""
    key = f"timing_style2:{date1 or 'latest'}:{days_back}"
    hit = cache.get(key)
    if hit:
        return hit

    cutoff = date1.replace("-", "") if date1 else None
    trading = await trade_days(days_back + 10)
    if cutoff:
        trading = [d for d in trading if d >= cutoff]
    trading = trading[-days_back:]

    samples = _daily_close_samples(trading)
    volumes = await _index_volumes(trading)

    rows: list[dict] = []
    for d in trading:
        s = samples.get(d) or {}
        rows.append({
            "date": f"{d[:4]}-{d[4:6]}-{d[6:]}",
            # 口径: 两市成交量(亿股)。原站为成交额(亿), push2his 历史成交额接口
            # 在当前网络环境不可达, 用量能替代(趋势高度一致), 牛市线相应调整。
            "trade_money_total": volumes.get(d, 0),
            "pe_ratio": None,
            "market_cap_total": None,
            "market_cap_circ_total": None,
            "up_count": s.get("up_count", 0),
            "up_gt5_count": s.get("up_gt5_count", 0),
            "up_gt5_erver_count": None,
            "turnover_ratio": None,
        })

    flags = _timing_flags([r["trade_money_total"] or 0 for r in rows], bull_line=1600)
    for r, f in zip(rows, flags):
        r["timing"] = f

    cache.set(key, rows, 6 * 3600)
    return rows
