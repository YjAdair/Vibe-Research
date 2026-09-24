"""Run `python -m app.worker` beside the API. Ctrl-C/SIGTERM drains job state."""
import asyncio
import logging
import os
import signal
import time
from app.core.collector import Collector, status
from app.datasources.http import get_client


async def serve():
    os.environ['TZ'] = 'Asia/Shanghai'
    if hasattr(time,'tzset'):
        time.tzset()
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    # Windows: add_signal_handler 仅部分信号可用；Unix 用 SIGINT/SIGTERM。
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopped.set)
        except (NotImplementedError, RuntimeError, ValueError):
            signal.signal(sig, lambda *_: stopped.set())
    task = asyncio.create_task(Collector().run())
    waiter = asyncio.create_task(stopped.wait())
    try:
        done,_ = await asyncio.wait([task,waiter],return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task  # runtime failures must produce a nonzero process exit
    finally:
        task.cancel(); waiter.cancel()
        await asyncio.gather(task,waiter,return_exceptions=True)
        await get_client().aclose()


if __name__ == '__main__':
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status',action='store_true',help='Print current collector heartbeat and recent jobs; no collection')
    args = parser.parse_args()
    if args.status:
        print(json.dumps(status(),ensure_ascii=False,indent=2))
    else:
        logging.basicConfig(level=logging.INFO,format='%(asctime)s %(name)s %(levelname)s %(message)s')
        logging.getLogger('httpx').setLevel(logging.WARNING)
        asyncio.run(serve())
