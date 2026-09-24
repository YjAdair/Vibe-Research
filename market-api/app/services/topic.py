# -*- coding: utf-8 -*-
"""题材热度：以东财概念板块行情 + 涨停股概念归属聚合。

数据结构对齐原站题材页：
- board_list(t:3) 提供全市场概念板块涨幅、上涨/下跌家数、领涨股（真实板块行情）
- 每只涨停股查 slist 概念归属（一只股归入多个概念板块），统计板块内
  limit_up_count、连板高度、代表股（热度核心是涨停强度）
- 同花顺涨停原因作为 tag_source 兜底描述
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.core.cache import cache
from app.config import settings
from app.core.store import store
from app.datasources import eastmoney, ths
from app.services import pools
from app.datasources.codes import market_of


def _split_tags(reason: str) -> list[str]:
    """'复合肥+磷化工+缓控释肥' -> ['复合肥', '磷化工', '缓控释肥']。"""
    out = []
    for t in (reason or "").replace("，", "+").replace(",", "+").split("+"):
        t = t.strip()
        if t and t not in ("其他", "涨停", "连板"):
            out.append(t)
    return out


# 统计类板块过滤：东财概念板块中约 1/4 是涨停统计/风格/指数样本/持仓类技术板块，
# 并非真实题材（如“昨日涨停_含一字”“题材股”“最近多板”），刷屏会淹没真实题材
STATISTICAL_BOARD_KEYWORDS = (
    "昨日",
    "近期",
    "百日",
    "题材股",
    "趋势股",
    "热股",
    "多板",
    "连板",
    "涨停",
    "首板",
    "打板",
    "大盘股",
    "中盘股",
    "小盘股",
    "微盘股",
    "微盘精选",
    "权重股",
    "价值股",
    "成长股",
    "周期股",
    "反转股",
    "超跌股",
    "低价股",
    "百元股",
    "微利股",
    "行业龙头",
    "超级品牌",
    "独角兽",
    "B股",
    "AB股",
    "AH股",
    "央视50",
    "HS300",
    "上证50",
    "上证180",
    "上证380",
    "深证100",
    "中证500",
    "深成500",
    "创业板综",
    "创业板成份",
    "MSCI",
    "标准普尔",
    "富时罗素",
    "GDR",
    "融资融券",
    "股通",
    "重仓",
    "证金持股",
    "养老金",
    "茅指数",
    "宁组合",
    "中字头",
    "季报",
    "年报",
    "预增",
    "预减",
    "扭亏",
    "首亏",
    "摘帽",
    "破净",
    "破发",
    "破增发价",
    "高股息",
    "股权激励",
    "股权转让",
    "举牌",
    "密集调研",
    "次新股",
    "做市商",
    "REITs",
    "转债标的",
    "策略股",
    "新股",
    "ST股",
    "风格",
    "独角兽",
    "微利",
)


def is_statistical_board(name: str) -> bool:
    """判断是否为统计/风格/指数成分类板块（非真实题材）。"""
    if not name:
        return True
    return any(kw in name for kw in STATISTICAL_BOARD_KEYWORDS)


def _reasons(pct: float, up_ratio: float, leader_count: int, limit_up_count: int) -> list[str]:
    """原站 home-topics 三条固定理由：涨幅、上涨家数占比、领涨家数。"""
    out = []
    if pct > 0:
        out.append(f"成分股平均上涨 {pct:.2f}%")
    elif pct < 0:
        out.append(f"成分股平均下跌 {abs(pct):.2f}%")
    if up_ratio:
        out.append(f"{up_ratio:.0f}% 成分股上涨")
    if leader_count:
        out.append(f"{leader_count} 家涨幅超过 5%")
    return out


def snapshot_item(unique_key: str, day: str | None = None) -> dict | None:
    """Read one published topic snapshot row. Missing stays missing."""
    from datetime import datetime as _dt
    date = day or _dt.now().strftime('%Y-%m-%d')
    if len(date) == 8:
        date = f'{date[:4]}-{date[4:6]}-{date[6:]}'
    items = store.topic_snapshot_all(date)
    return next((it for it in items if it.get('unique_key') == unique_key), None)


_snapshot_candidate_hint = 128
_MIN_HOME_MEMBERS = 3  # 成分股过少的表不参与首页三卡片（避免单股测试表刷屏）


def _table_members(topic_row: dict) -> list[str]:
    """题材表成分股（按个股名去重；同一股跨多个一级大类只算一次）。"""
    names: list[str] = []
    seen: set[str] = set()
    for r in topic_row.get("rows") or []:
        nm = str((r or {}).get("个股") or "").strip()
        if nm and nm not in seen:
            seen.add(nm)
            names.append(nm)
    return names


def _table_stats(members: list[str], closes_by_name: dict[str, dict]) -> dict | None:
    """原站口径：去重成分股收盘涨跌幅（close/prev_close-1）统计。"""
    pcts: list[float] = []
    for nm in members:
        q = closes_by_name.get(nm)
        if not q or not q.get("close") or not q.get("prev_close"):
            continue
        pcts.append((float(q["close"]) / float(q["prev_close"]) - 1) * 100)
    if not pcts:
        return None
    total = len(pcts)
    up = sum(1 for p in pcts if p > 0)
    down = sum(1 for p in pcts if p < 0)
    return {
        "today_pct": round(sum(pcts) / total, 2),
        "up_count": up,
        "down_count": down,
        "stock_count": total,
        "total_count": total,
        "up_ratio": round(up / total * 100, 1),
        "leader_count": sum(1 for p in pcts if p > 5),
        "limit_up_count": sum(1 for p in pcts if p >= 9.9),
        "limit_down_count": sum(1 for p in pcts if p <= -9.9),
    }


def _topic_score(stats: dict) -> float:
    """自研题材热度分（原站公式黑箱，见 docs/待解决问题清单.md）。

    涨幅为主、涨停/领涨/普涨度加权，min(100, ·) 封顶。
    """
    pct = float(stats.get("today_pct") or 0)
    # 负涨幅直接压分：跌的题材不应靠涨停家数上分
    if pct < 0:
        # 允许微量正分（涨停 1-2 家的下跌题材仍可见），否则贴 0
        lu = int(stats.get("limit_up_count") or 0)
        return round(min(max(8.0 * pct, 0.0) + 1.5 * min(lu, 2), 5.0), 1)
    lu = int(stats.get("limit_up_count") or 0)
    leader = int(stats.get("leader_count") or 0)
    ratio = float(stats.get("up_ratio") or 0)
    total = max(int(stats.get("total_count") or 0), 1)
    # lu/total 为题材内涨停占比，涨停 1 家在 12 股小表约 8.3 分，大表中权重自动降低
    score = 8.0 * pct + 9.0 * lu + 1.8 * leader + 0.22 * (ratio - 50.0) + 6.0 * (lu / total)
    return round(min(score, 100.0), 1)


def _iso_date(day: str) -> str:
    d = (day or "").replace("-", "")
    if len(d) != 8 or not d.isdigit():
        raise ValueError("Invalid date")
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def _home_meta(items: list[dict], trade_date: str, source: str, date_fell_back: bool = False) -> dict:
    return {
        "items": items,
        "candidate_count": _snapshot_candidate_hint,
        "strategy_candidate_count": sum(1 for i in items if i.get("selection_scope") == "strategy"),
        "selection_rule": "shared_strategy_top2_plus_all_top1",
        "trade_date": trade_date,
        "calculated_at": datetime.now().astimezone().isoformat(),
        "market_snapshot_at": "15:00",
        "source": source,
        "cache_status": "ready",
        "refresh_seconds": 60,
        "update_interval_minutes": 5,
        "is_trading": False,
        "updated_kline_count": 0,
        "date_fell_back": date_fell_back,
        "refresh_mode": settings.collector_mode,
    }


def _pick_home_items(items: list[dict], limit: int = 3) -> list[dict]:
    """三槽位选择：strategy 前 2 + all 第 1（与原站 selection_rule 一致）。"""
    strategy = [i for i in items if i.get("selection_scope") == "strategy"]
    others = [i for i in items if i.get("selection_scope") != "strategy"]
    strategy.sort(key=lambda x: (x.get("score") or 0), reverse=True)
    others.sort(key=lambda x: (x.get("score") or 0), reverse=True)
    picked = strategy[:2] + others[:1]
    return picked[:limit] if limit else picked


async def home_topics(limit: int = 50, date: str | None = None) -> dict:
    """只查询已验证派生快照；旧公式与当前成员倒推路径已停止。"""
    from app.services import ml_r1_service
    requested = _iso_date(date) if date else None
    now = datetime.now().astimezone()
    days = [d for d in ml_r1_service.calendar_days()
            if d < now.date().isoformat() or (d == now.date().isoformat() and now.hour >= 15)]
    selected = requested or (days[-1] if days else now.date().isoformat())
    items = [i for i in store.topic_snapshot_all(selected) if i.get('status') == 'final']
    items.sort(key=lambda i: (i.get('score') is None, -(i.get('score') or 0)))
    result = _home_meta(items[:limit], selected, 'verified_topic_snapshot')
    result.update(status='ok' if items else 'missing_input', market_snapshot_at=None,
                  selection_rule='verified_score_order', candidate_count=len(items),
                  reasons=[] if items else ['legacy_derived_values_quarantined'])
    return result


async def backfill_day(day: str, limit: int = 200) -> dict:
    """只从修正版日级结果发布；不回源，不重启旧派生算法。"""
    from app.services import ml_r1_service
    selected = _iso_date(day)
    snap = ml_r1_service.get_day(15, selected)
    items = []
    for row in snap.get('rows', []):
        states = row.get('metric_status', {})
        if not any(v=='final' for v in states.values()):
            continue
        full = states.get('strength') == 'final' and states.get('return') == 'final'
        items.append({'unique_key':row['plate_code'], 'name':row['plate_name'],
                      'score':row.get('strength') if states.get('strength')=='final' else None,
                      'today_pct':row['return']*100 if states.get('return')=='final' else None,
                      **{k:row.get(k) for k in ('up_count','down_count','up_ratio','limit_up_count','limit_down_count')},
                      'stock_count':row.get('coverage',{}).get('eligible'), 'leader_count':None,
                      'stocks':[{'code':code,'name':info['stock_name'],'pct':info['return']*100}
                                for code,info in row.get('member_returns',{}).items()],
                      'trade_date':selected,'formula_version':ml_r1_service.VERSION,
                      'input_snapshot_id':snap.get('snapshot_id'), 'metric_status':states,
                      'source':'ml_r1_prepared', 'source_as_of':snap.get('source_as_of'),
                      'available_at':snap.get('available_at'), 'complete':full,
                      'status':'final' if full else 'partial_preview', 'reasons':row.get('reasons',[])})
    if items:
        store.topic_snapshot_save(selected,items)
    return {'date':selected, 'count':len(items),
            'status':'final' if items and all(i['complete'] for i in items) else 'missing_input'}


if __name__ == '__main__':
    import argparse, asyncio
    parser = argparse.ArgumentParser(description='Backfill published topic snapshots from dated limit pools')
    parser.add_argument('--date', action='append', required=True, help='YYYY-MM-DD, repeatable')
    parser.add_argument('--limit', type=int, default=200)
    args = parser.parse_args()
    async def _run():
        for day in args.date:
            print(await backfill_day(day, args.limit))
    asyncio.run(_run())
