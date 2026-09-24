# -*- coding: utf-8 -*-
"""题材监控 WebSocket（原站 wss://api.zizizaizai.com/v3/ws/topic_monitor）。

原站消息结构（bundle chunk-ea8f7f04 逆向）：
  {type: "topic_alert", topic: "题材名", leader: "...",
   trigger: {time, name, code, reason, pnl_pct?, open_pct?, pct, score, score_str}}
客户端仅认 type=topic_alert 与 type=error，断线 5s 重连，只保留最新 20 条。

本地触发口径（盘中每 60s 全市场快照 vs 上一次轮询）：
- 成分股均涨变动 >= 1.0pp
- 涨停家数变化（pct>=9.8 主板 / >=19.8 创科，近似）
- 领涨股换人且新领涨 pct >= 3
- 题材首破策略池条件（ratio>=70 且 pct>=1）
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from app.core.store import store

TZ = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)

POLL_SECONDS = 60
SESSIONS = ((dtime(9, 25), dtime(11, 35)), (dtime(12, 55), dtime(15, 5)))

# ---------- 连接管理 ----------

class _Hub:
    def __init__(self):
        self.clients: set = set()

    async def join(self, ws):
        self.clients.add(ws)

    def leave(self, ws):
        self.clients.discard(ws)

    async def broadcast(self, message: dict):
        if not self.clients:
            return
        dead = []
        data = message
        for ws in list(self.clients):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.leave(ws)


hub = _Hub()


def _in_session(now: datetime | None = None) -> bool:
    now = now or datetime.now(TZ)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return any(a <= t <= b for a, b in SESSIONS)


def _limit_up_threshold(code: str) -> float:
    """近似涨停阈值：主板 9.8，创业/科创 19.8，北交所 29.8。"""
    c = str(code)
    if c.startswith(("30", "68")):
        return 19.8
    if c.startswith(("8", "4", "92")):
        return 29.8
    return 9.8


def _topic_stats(members: list[dict], quotes: dict[str, dict]) -> dict:
    """quotes: code -> {pct, name}"""
    pcts, lu, leader = [], 0, None
    for m in members:
        code = str(m.get("code") or "")
        q = quotes.get(code)
        if not q or q.get("pct") is None:
            continue
        pct = float(q["pct"])
        pcts.append(pct)
        if pct >= _limit_up_threshold(code):
            lu += 1
        if leader is None or pct > leader[1]:
            leader = (m.get("name") or q.get("name") or code, pct, code)
    if not pcts:
        return {"avg": None, "up": 0, "total": 0, "ratio": 0.0, "lu": 0, "leader": None}
    up = sum(1 for p in pcts if p > 0)
    return {
        "avg": round(sum(pcts) / len(pcts), 2),
        "up": up,
        "total": len(pcts),
        "ratio": round(up / len(pcts) * 100, 1),
        "lu": lu,
        "leader": leader,
    }


def _score_str(score: int) -> str:
    return {0: "0 星", 1: "1 星", 2: "2 星", 3: "3 星"}.get(score, f"{score} 星")


def _trigger_reason(stat: dict, prev: dict | None) -> str:
    reasons = []
    if prev and prev.get("avg") is not None and stat.get("avg") is not None:
        d = round(float(stat["avg"]) - float(prev["avg"]), 2)
        if abs(d) >= 1.0:
            reasons.append(f"均涨{'回升' if d > 0 else '回落'} {abs(d):.1f}pct 至 {stat['avg']}%")
    if prev is not None and stat.get("lu") != prev.get("lu"):
        reasons.append(f"涨停 {prev.get('lu', 0)} 家 → {stat.get('lu', 0)} 家")
    if prev and prev.get("leader") and stat.get("leader") and prev["leader"][0] != stat["leader"][0] and stat["leader"][1] >= 3:
        reasons.append(f"领涨股切换为 {stat['leader'][0]}")
    if prev is None:
        reasons.append(f"进入监控：均涨 {stat.get('avg')}%，涨停 {stat.get('lu')} 家")
    return "；".join(reasons) if reasons else ""


def _score_of(stat: dict) -> int:
    """0-3 星：粗粒度确定性评分（本地骨架，未对齐原站专有算法）。"""
    avg = stat.get("avg") or 0
    lu = stat.get("lu") or 0
    ratio = stat.get("ratio") or 0
    if avg >= 3 and lu >= 3 and ratio >= 80:
        return 3
    if avg >= 1.5 and (lu >= 1 or ratio >= 70):
        return 2
    if avg >= 0.5 and ratio >= 60:
        return 1
    return 0


async def _snapshot_quotes() -> dict[str, dict] | None:
    """全市场快照 -> {code: {pct, name}}；失败返回 None。"""
    from app.datasources import eastmoney
    try:
        snap = await eastmoney.market_snapshot()
        if not snap.get("complete"):
            return None
        out = {}
        for s in snap.get("stocks") or []:
            code = str(s.get("code") or "")
            if code:
                out[code] = {"pct": s.get("pct"), "name": s.get("name")}
        return out if out else None
    except Exception as e:
        logger.warning("topic_monitor snapshot failed: %s", e)
        return None


def _monitored_topics() -> list[dict]:
    """关注题材 + 成分。"""
    from app.services import topic_tables
    out = []
    for key in store.followed_keys("local"):
        item = store.get_topic(key, include_deleted=False)
        if not item:
            continue
        members = topic_tables.member_codes(item)
        if members:
            out.append({"unique_key": key, "name": item.get("name") or key, "members": members})
    return out


def _alerts_for(topics: list[dict], quotes: dict, prev_stats: dict) -> tuple[list[dict], dict]:
    """生成提醒消息与最新 stats。"""
    alerts = []
    now_stats = {}
    for t in topics:
        stat = _topic_stats(t["members"], quotes)
        now_stats[t["unique_key"]] = stat
        prev = prev_stats.get(t["unique_key"])
        reason = _trigger_reason(stat, prev)
        if not reason:
            continue
        leader = stat.get("leader")
        score = _score_of(stat)
        alerts.append({
            "type": "topic_alert",
            "topic": t["name"],
            "trigger": {
                "time": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
                "name": leader[0] if leader else "",
                "code": leader[2] if leader else "",
                "reason": reason,
                "pct": leader[1] if leader else 0.0,
                "score": score,
                "score_str": _score_str(score),
            },
            "leader": f"领涨：{leader[0]}（{leader[2]}）+{leader[1]:.2f}%" if leader else "",
        })
    return alerts, now_stats


async def monitor_loop():
    """后台轮询循环（每个 WS 连接共用一个循环，由 hub 持有）。"""
    if getattr(monitor_loop, "_running", False):
        return
    monitor_loop._running = True
    prev_stats: dict = {}
    last_day = None
    try:
        while True:
            now = datetime.now(TZ)
            if _in_session(now):
                if now.date() != last_day:
                    prev_stats = {}
                    last_day = now.date()
                topics = _monitored_topics()
                if topics and hub.clients:
                    quotes = None
                    for attempt in range(2):
                        quotes = await _snapshot_quotes()
                        if quotes:
                            break
                        await asyncio.sleep(3)
                    if quotes:
                        alerts, prev_stats = _alerts_for(topics, quotes, prev_stats)
                        for a in alerts:
                            await hub.broadcast(a)
            else:
                prev_stats = {}
                last_day = None
            await asyncio.sleep(POLL_SECONDS)
    finally:
        monitor_loop._running = False


_loop_task = None


def ensure_loop() -> asyncio.Task:
    """启动共享监控循环（幂等）。"""
    global _loop_task
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.create_task(monitor_loop())
    return _loop_task
