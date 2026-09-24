"""A-share code -> THS industry (881xxx) mapping.

Data source: eastmoney industry board tree (level 1/2/3 board names per stock,
~5600 codes). EM names are aligned to the THS 98-industry taxonomy via a
direct-name match first, then an explicit alias table.

The EM tree refreshes slowly (industry reclassifications are rare); the
snapshot is cached on disk and can be rebuilt in ~5s.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("zzquant.industry_map")

CACHE_PATH = Path(__file__).with_name("em_industry_map_cache.json")
TZ = ZoneInfo("Asia/Shanghai")

# eastmoney board name -> THS 14-type industry name.
# Built by aligning EM's 3-level industry tree with THS's 881xxx taxonomy.
# Direct matches (identical names) do not need an entry here.
EM_TO_THS = {
    # EM level-2 board names
    "种植业": "种植业与林业", "林业Ⅱ": "种植业与林业", "林业Ⅲ": "种植业与林业",
    "渔业": "养殖业", "农业综合Ⅱ": "农业服务",
    "一般零售": "零售", "旅游零售Ⅱ": "零售", "商贸零售": "零售",
    "地面兵装Ⅱ": "军工装备", "航空装备Ⅱ": "军工装备", "航海装备Ⅱ": "军工装备", "航天装备Ⅱ": "军工装备",
    "地面兵装Ⅲ": "军工装备", "航空装备Ⅲ": "军工装备", "航海装备Ⅲ": "军工装备", "航天装备Ⅲ": "军工装备",
    "旅游及景区": "旅游及酒店", "自然景区": "旅游及酒店", "人工景区": "旅游及酒店",
    "酒店": "酒店及餐饮", "餐饮": "酒店及餐饮", "酒店餐饮": "酒店及餐饮",
    "IT服务Ⅱ": "IT服务", "IT服务Ⅲ": "IT服务",
    "工程咨询服务Ⅱ": "建筑装饰", "装修装饰Ⅱ": "建筑装饰", "房屋建设Ⅱ": "建筑装饰",
    "房屋建设Ⅲ": "建筑装饰", "装修装饰Ⅲ": "建筑装饰", "基础建设": "建筑装饰",
    "焦炭Ⅱ": "煤炭开采加工", "煤炭开采": "煤炭开采加工", "焦炭Ⅲ": "煤炭开采加工", "煤化工": "煤炭开采加工",
    "玻璃玻纤": "建筑材料", "装修建材": "建筑材料", "水泥": "建筑材料",
    "玻纤制造": "建筑材料", "玻璃制造": "建筑材料", "水泥制造": "建筑材料", "耐火材料": "建筑材料",
    "综合Ⅱ": "综合", "综合Ⅲ": "综合", "塑料": "塑料制品", "膜材料": "塑料制品", "改性塑料": "塑料制品",
    "调味发酵品Ⅱ": "食品加工制造", "食品加工": "食品加工制造", "预加工食品": "食品加工制造",
    "休闲食品": "食品加工制造", "保健品": "食品加工制造",
    "专业工程": "建筑装饰", "燃气Ⅱ": "燃气", "橡胶": "橡胶制品",
    "炼化及贸易": "石油加工贸易", "油服工程": "油气开采及服务", "油气开采Ⅱ": "油气开采及服务",
    "证券Ⅱ": "证券", "银行Ⅱ": "银行", "保险Ⅱ": "保险", "国有大型银行Ⅱ": "银行",
    "股份制银行": "银行", "城商行": "银行", "农商行": "银行", "保险Ⅲ": "保险", "证券Ⅲ": "证券",
    "白酒Ⅱ": "白酒", "白酒Ⅲ": "白酒", "非白酒": "饮料制造", "饮料乳品": "饮料制造",
    "啤酒": "饮料制造", "其他酒类": "饮料制造", "软饮料": "饮料制造", "乳品": "饮料制造",
    "照明设备Ⅱ": "小家电", "其他家电Ⅱ": "小家电", "个护小家电": "小家电", "清洁电器": "小家电",
    "化妆品": "美容护理", "医疗美容": "美容护理", "个护用品": "美容护理",
    "文娱用品": "其他社会服务", "体育Ⅱ": "其他社会服务", "专业服务": "其他社会服务", "人力资源服务": "其他社会服务",
    "房地产开发": "房地产", "产业地产": "房地产", "住宅开发": "房地产", "物业管理": "房地产服务",
    "动物保健Ⅱ": "医药商业",
    "贸易Ⅱ": "贸易",
    "乘用车": "汽车整车", "商用车": "汽车整车",
    "铁路公路": "公路铁路运输", "铁路运输": "公路铁路运输", "公路": "公路铁路运输",
    "航空机场": "机场航运", "航运港口": "港口航运",
    "环保设备Ⅱ": "环保设备", "电子化学品Ⅱ": "电子化学品", "电机Ⅱ": "电机", "轨交设备Ⅱ": "轨交设备",
    "彩电": "黑色家电",
    "中药Ⅱ": "中药", "中药Ⅲ": "中药",
    "非金属材料Ⅱ": "非金属材料", "冶钢原料": "钢铁", "普钢": "钢铁", "特钢Ⅱ": "钢铁",
    "其他电子Ⅱ": "其他电子",
    "医疗研发外包": "医疗服务", "医院诊疗": "医疗服务",
    "环境治理": "环境治理",
    "电子商务服务": "互联网电商",
    "计算机": "计算机应用",
    # EM level-3 board names
    "印制电路板": "元件", "被动元件": "元件",
    "通信线缆及配套": "通信设备", "通信网络设备及器件": "通信设备", "通信终端及配件": "通信设备",
    "数字芯片设计": "半导体", "集成电路制造": "半导体", "半导体材料": "半导体", "分立器件": "半导体",
    "消费电子零部件及组装": "消费电子", "光学元件": "光学光电子", "LED": "光学光电子", "面板": "光学光电子",
    "种子": "种植业与林业", "其他种植业": "种植业与林业", "粮食种植": "种植业与林业",
    "粮油加工": "农产品加工", "其他农产品加工": "农产品加工",
    "水产养殖": "养殖业", "禽畜养殖": "养殖业", "饲料": "养殖业",
    "百货": "零售", "超市": "零售", "多业态零售": "零售", "专业连锁": "零售",
    "铜": "有色冶炼加工", "铝": "有色冶炼加工", "铅锌": "有色冶炼加工", "镍钴锡锑": "有色冶炼加工",
    "黄金": "贵金属", "稀土": "小金属",
    "军工电子": "军工电子",
    "火力发电": "电力", "水力发电": "电力", "风力发电": "电力", "光伏发电": "电力", "核力发电": "电力",
    "电能综合服务": "电力",
    "电网自动化设备": "电网设备", "输变电设备": "电网设备", "线缆部件及其他": "电网设备",
    "垂直应用软件": "软件开发", "横向通用软件": "软件开发", "安防设备": "计算机设备",
    "数字媒体": "文化传媒", "广告营销": "文化传媒", "营销代理": "文化传媒", "其他数字媒体": "文化传媒",
    "文字媒体": "文化传媒", "出版": "文化传媒",
    "影视动漫": "影视院线", "院线": "影视院线",
    "其他化学原料": "化学原料", "磨具磨料": "非金属材料",
    "机器人": "自动化设备", "工控设备": "自动化设备", "激光设备": "自动化设备",
    "汽车电子电气系统": "汽车零部件",
    "实验仪器": "仪器仪表",
}


def _ths_catalog() -> set[str]:
    from app.datasources import ths
    return set(ths.plate_catalog().get("14", {}).keys())


def _load_cache() -> dict[str, list[str]] | None:
    if not CACHE_PATH.exists():
        return None
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        stamp = date.fromisoformat(str(payload.get("built_at", ""))[:10])
        if (date.today() - stamp).days > 14:
            return None
        return payload.get("em_boards") or None
    except Exception:
        return None


def _save_cache(em_boards: dict[str, list[str]]) -> None:
    payload = {"built_at": datetime.now(TZ).isoformat(), "em_boards": em_boards}
    CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


async def build_em_tree() -> dict[str, list[str]]:
    """Fetch all eastmoney industry boards and their members.

    Returns code -> [em board names at every level] (order not guaranteed).
    ~500 boards / ~5600 stocks; takes about 5s.
    """
    import httpx
    from app.datasources import eastmoney

    all_boards: list[dict] = []
    for page in (1, 2, 3, 4, 5, 6):
        data = await eastmoney.board_list(board_type=2, page=page, size=100)
        rows = data.get("boards") or []
        all_boards.extend(rows)
        if len(rows) < 100:
            break
    if len(all_boards) < 80:
        raise ValueError(f"eastmoney industry board list too small: {len(all_boards)}")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Referer": "https://quote.eastmoney.com/",
        "Connection": "close",
    }
    mapping: dict[str, list[str]] = {}
    async with httpx.AsyncClient(headers=headers, timeout=10) as client:
        sem = asyncio.Semaphore(4)

        async def one(board: dict) -> tuple[str, list[str]]:
            code, members = board["plate_code"], []
            page = 1
            while True:
                try:
                    async with sem:
                        r = await client.get(
                            "https://push2delay.eastmoney.com/api/qt/clist/get",
                            params={
                                "pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                                "fid": "f12", "fs": f"b:{code}", "fields": "f12,f14",
                            },
                        )
                    d = (r.json() or {}).get("data") or {}
                    rows = d.get("diff") or []
                    if not rows:
                        break
                    members.extend(str(x.get("f12") or "").zfill(6) for x in rows)
                    if len(members) >= int(d.get("total") or 0):
                        break
                    page += 1
                except Exception:
                    break
            return board["plate_name"], members

        results = await asyncio.gather(*(one(b) for b in all_boards))
    for name, members in results:
        for m in members:
            mapping.setdefault(m, []).append(name)
    if len(mapping) < 4000:
        raise ValueError(f"eastmoney industry membership too small: {len(mapping)}")
    return mapping


async def em_tree(refresh: bool = False) -> dict[str, list[str]]:
    if not refresh:
        cached = _load_cache()
        if cached:
            return cached
    tree = await build_em_tree()
    _save_cache(tree)
    return tree


def ths_industry_of(code: str, em_boards: list[str] | None, catalog: set[str] | None = None) -> str | None:
    """Map one stock to a THS 14-type industry name.

    Priority: any board name that IS a THS industry name (exact) first, then
    the alias table (scanning most-specific names last-to-first).
    """
    if not em_boards:
        return None
    cat = catalog if catalog is not None else _ths_catalog()
    for b in em_boards:
        if b in cat:
            return b
    for b in reversed(em_boards):
        cand = EM_TO_THS.get(b)
        if cand and cand in cat:
            return cand
    return None


_F10_CACHE_PATH = Path(__file__).with_name("ths_f10_industry_cache.json")


def _load_f10_cache() -> dict[str, str]:
    if not _F10_CACHE_PATH.exists():
        return {}
    try:
        payload = json.loads(_F10_CACHE_PATH.read_text(encoding="utf-8"))
        stamp = date.fromisoformat(str(payload.get("built_at", ""))[:10])
        if (date.today() - stamp).days > 30:
            return {}
        return payload.get("fields") or {}
    except Exception:
        return {}


def _save_f10_cache(fields: dict[str, str]) -> None:
    merged = _load_f10_cache()
    merged.update(fields)
    payload = {"built_at": datetime.now(TZ).isoformat(), "fields": merged}
    _F10_CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


async def fetch_f10_industries(codes: list[str]) -> dict[str, str]:
    """Fetch authoritative THS industry per stock from basic.10jqka F10 field.html.

    The page embeds index.10jqka.com.cn/list/field/<0+881xxx>/ which is the
    stock's official THS industry code. One request per stock; callers should
    only pass small lists (e.g. the daily hot-100).
    """
    import re
    import httpx

    cached = _load_f10_cache()
    out: dict[str, str] = {}
    todo = [c for c in codes if c not in cached]
    if todo:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            timeout=10,
            follow_redirects=True,
        ) as client:
            sem = asyncio.Semaphore(6)

            async def one(code: str) -> None:
                try:
                    async with sem:
                        r = await client.get(f"https://basic.10jqka.com.cn/{code}/field.html")
                    if r.status_code != 200:
                        return
                    m = re.search(r"index\.10jqka\.com\.cn/list/field/(\d+)/", r.text)
                    if not m:
                        return
                    fid = m.group(1).lstrip("0")
                    if fid.startswith("88") and len(fid) == 6:
                        out[code] = fid
                except Exception:
                    return

            await asyncio.gather(*(one(c) for c in todo))
        if out:
            _save_f10_cache(out)
            cached.update(out)
    return {c: cached[c] for c in codes if c in cached}
