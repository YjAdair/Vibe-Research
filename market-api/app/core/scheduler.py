"""Optional embedded entry; production API uses an independent collector process."""
import asyncio


async def start_scheduler() -> asyncio.Task:
    # ponytail: lazy import — collector uses fcntl (Unix); API-only start must not pull it in on Windows
    from app.core.collector import Collector
    return asyncio.create_task(Collector().run(),name='zzquant-collector')
