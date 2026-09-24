"""量化策略接口。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.core.errors import ok
from app.services import quant


router = APIRouter(prefix="/v3/quant", tags=["quant"])
legacy_api_router = APIRouter(prefix="/v3/api/quant", tags=["quant"])


@router.get("/strategy/performance")
async def strategy_performance():
    return ok(await quant.strategy_performance())


@router.get("/stock/pool/jyzj/{quant_code}")
async def jyjy_pool(quant_code: int, date: str | None = None, limit: int = 80):
    from app.services import jyjy
    try:
        data = await jyjy.pool(quant_code, date, limit)
    except ValueError as exc:
        from fastapi import HTTPException
        raise HTTPException(422, str(exc)) from exc
    return ok(data)


@legacy_api_router.get("/stock/pool/jyzj/{quant_code}")
async def jyjy_pool_legacy(quant_code: int, date: str | None = None, limit: int = 80):
    return await jyjy_pool(quant_code, date, limit)


@router.get("/strategy/equity")
async def strategy_equity():
    return ok(await quant.strategy_equity())


@router.get("/strategy/daily-details")
async def strategy_daily_details(date: str | None = None, account_id: str | None = None):
    return ok(await quant.strategy_daily_details(date))


@router.get("/strategy/sell")
async def strategy_sell():
    return ok([])


@router.get("/strategy/share-token")
async def strategy_share_token():
    return ok({"token": uuid.uuid4().hex})


# ---- 量化编辑器（协议对齐原站 /quant/backtest/*） ----

from fastapi import HTTPException
from pydantic import BaseModel

from app.services import quant_backtest


class BacktestRunReq(BaseModel):
    strategy_id: int | None = None
    code: str
    start_date: str = "2024-01-01"
    end_date: str = "2025-12-31"
    initial_capital: float = 1_000_000.0
    frequency: str = "day"


class StrategySaveReq(BaseModel):
    id: int | None = None
    name: str
    code: str


class PaperStartReq(BaseModel):
    strategy_id: int
    account_id: int | None = None


@router.post("/backtest/run")
async def backtest_run(req: BacktestRunReq):
    try:
        result = quant_backtest.run_backtest(
            req.code, req.start_date, req.end_date, req.initial_capital, req.frequency
        )
    except ValueError as exc:
        return {"code": 50000, "message": str(exc), "data": None}
    except Exception as exc:  # 引擎内部错误也不透出堆栈
        return {"code": 50000, "message": "Backtest engine error", "data": None}
    return ok(result)


@router.post("/strategy/save")
async def strategy_save(req: StrategySaveReq):
    try:
        sid = quant_backtest.strategy_save(req.id, req.name, req.code)
    except ValueError as exc:
        return {"code": 50000, "message": str(exc), "data": None}
    return ok({"id": sid})


@router.get("/strategy/list")
async def strategy_list_all():
    return ok(quant_backtest.strategy_list())


@router.post("/paper/start")
async def paper_start(req: PaperStartReq):
    try:
        result = quant_backtest.paper_start(req.strategy_id, req.account_id)
    except ValueError as exc:
        return {"code": 50000, "message": str(exc), "data": None}
    return ok(result)
