"""应用入口。"""

from __future__ import annotations

import logging
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import hotspot_routes, market_routes, misc_routes, open_routes, quant_routes, topic_routes, community_routes, feedback_routes, admin_routes
from app.core.scheduler import start_scheduler
from app.config import settings


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.collector_mode not in ('external','embedded','off'):
        raise ValueError('ZZQUANT_COLLECTOR_MODE must be external, embedded or off')
    task = await start_scheduler() if settings.collector_mode == 'embedded' else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        from app.datasources.http import get_client
        await get_client().aclose()


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (hotspot_routes.router, open_routes.router, market_routes.router, market_routes.legacy_router, topic_routes.router, quant_routes.router, quant_routes.legacy_api_router, misc_routes.router, community_routes.router, feedback_routes.router, admin_routes.router):
    app.include_router(r)

# 用户上传文件（帖子图片/头像）：本地磁盘存储，商业化时可替换为对象存储
import os as _os
from pathlib import Path as _Path
_uploads_dir = _Path(__file__).resolve().parent.parent / "uploads"
_uploads_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(_uploads_dir)), name="uploads")


@app.get("/")
async def root():
    return {"code": 20000, "message": "success", "data": {"name": settings.app_name, "version": settings.app_version}}


@app.get("/health")
async def health():
    from app.core.collector import status
    collector = status()
    return {"status": "ok", "collector_mode":settings.collector_mode,
            "collector_online":collector['online'], "collector_heartbeat_age_seconds":collector['heartbeat_age_seconds']}


@app.get('/v3/open/collector/status')
async def collector_status():
    from app.core.collector import status
    from app.core.errors import ok
    result = status()
    # Operational state only: no source credentials, paths or raw exception text.
    result['runtime'].pop('owner',None)
    result['runtime'].pop('pid',None)
    for row in result['recent_jobs']:
        row.pop('owner',None)
    return ok(result)


@app.get('/v3/open/collector/freshness')
async def collector_freshness():
    from app.core.freshness import evaluate
    from app.core.errors import ok
    return ok(evaluate())
