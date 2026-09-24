"""Single-host collector ownership and durable, observable job execution."""
from __future__ import annotations
import asyncio
import logging
import os
import sys
import time
import uuid
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core.store import store, Store

TZ = ZoneInfo('Asia/Shanghai')
logger = logging.getLogger('zzquant.collector')

# ponytail: fcntl is Unix-only; Windows uses msvcrt byte-range lock for local single-host start
if sys.platform == 'win32':
    import msvcrt
    fcntl = None
else:
    import fcntl
    msvcrt = None


def code_fingerprint() -> dict:
    """Cheap fingerprint of the deployed app package: newest mtime and total bytes.

    The collector runs a long-lived process and does not hot-reload code. If a
    source file changes after the running worker started, the database keeps
    collecting with stale behavior while appearing healthy. This fingerprint is
    recorded at worker start and compared by status() to surface that mismatch.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    newest, total = 0.0, 0
    for path in sorted(root.rglob('*.py')):
        try:
            st = path.stat()
        except OSError:
            continue
        newest = max(newest, st.st_mtime)
        total += st.st_size
    return {'newest_mtime': newest, 'total_bytes': total}


class ProcessLock:
    """OS releases ownership on process exit, including crash; never delete the inode."""
    def __init__(self, path):
        self.path = path
        self.file = None
    def acquire(self):
        Path(self.path).parent.mkdir(parents=True,exist_ok=True)
        handle = open(self.path,'a+')
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                handle.seek(0)
                if handle.read(1) == '':
                    handle.write('0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except (BlockingIOError, OSError):
            handle.close()
            return False
        self.file = handle
        return True
    def close(self):
        if self.file:
            if fcntl is not None:
                fcntl.flock(self.file.fileno(),fcntl.LOCK_UN)
            else:
                try:
                    self.file.seek(0)
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            self.file.close()
            self.file = None


JOB_TIMEOUTS = {
    'ml_r1_preopen': 300,
    'ml_r1_eod': 2400,
    'kaipanla_plate_history': 300,
}


def due_jobs(now: datetime) -> list[tuple[str,str]]:
    """Desired slots only; the runner separately confirms today's exchange session."""
    if now.weekday() >= 5:
        return []
    from app.config import settings
    day, minute, t = now.date().isoformat(), now.strftime('%H:%M'), now.time()
    jobs = []
    if dtime(9,15) <= t < dtime(9,25):
        jobs.append(('auction_candidates',day+'T'+minute))
    if dtime(9,25) <= t < dtime(9,30):
        jobs.append(('auction',day+'T'+minute+f':{now.second//15*15:02d}'))
    if now.second >= 5 and (dtime(9,30) <= t.replace(second=0,microsecond=0) <= dtime(11,30) or dtime(13) <= t.replace(second=0,microsecond=0) <= dtime(15)):
        jobs.append(('market_minute',day+'T'+minute))
        # 题材榜盘中：每 5 分钟一次，整表替换今日预览
        if now.minute % 5 == 0:
            jobs.append(('kaipanla_plate_intraday', day+'T'+minute))
    if dtime(9,15) <= t <= dtime(15,5):
        jobs.append(('limit_pools',day+'T'+minute))
        jobs.append(('index_trends',day+'T'+minute))
        jobs.append(('popular',day+'T'+minute))
        jobs.extend((name,day+'T'+minute) for name in ('board_industry','board_concept'))
    elif dtime(15,5) < t <= dtime(18,5):
        jobs.extend((name,day+'T'+now.strftime('%H')) for name in ('board_industry','board_concept'))
    if t >= dtime(15,10):
        slot = day+(':late' if t >= dtime(20) else ':close')
        jobs.extend((name,slot) for name in ('limit_pools','block_top','index_trends','lhb','daily_close','topic_close','auction_close','index_history','movement','popular','stock_flow'))
        if settings.enable_origin_reference:
            jobs.append(('plate_rank', slot))
            jobs.append(('popular_deep', slot))
        jobs.append(('ai_report', slot))
        jobs.append(('ths_board_daily', slot))
    # 题材榜收盘定稿：15:30 / 16:00 / 20:00 各采一次；历史缺口每天盘后扫一次
    if t >= dtime(15, 30):
        finalize_tag = '2000' if t >= dtime(20) else ('1600' if t >= dtime(16) else '1530')
        jobs.append(('kaipanla_plate_finalize', f'{day}:finalize:{finalize_tag}'))
    if t >= dtime(16, 0):
        jobs.append(('kaipanla_plate_history', f'{day}:history'))
    # 题材成分：收盘后用开盘啦切片名单补近端成员（非原站）
    if t >= dtime(16, 30):
        jobs.append(('kaipanla_members', f'{day}:members'))
    if settings.enable_origin_reference and t >= dtime(20,30):
        jobs.append(('plate_reason', day+':evening'))
    if settings.enable_origin_reference and dtime(20,30) <= t < dtime(22,30):
        # 子板块成分每日快照：69 板块 x 6.5s 限流 ~= 7.5 分钟，超出单任务 180s，
        # 每 30 分钟窗口触发一批（slot 含小时，同一小时内不重复）
        hour_slot = day+'T'+now.strftime('%H')
        jobs.append(('plate_members', hour_slot))
    if (dtime(9,30) <= t <= dtime(11,30) or dtime(13) <= t <= dtime(15,10)) and now.minute % 10 == 0:
        jobs.append(('free_hotspot_members', day+'T'+minute))
    # ML-R1点时链路：盘前universe必须在09:30点时门禁前落库（每日一次）；
    # 盘后15:12起（EM快照15:00门禁+当日板块目录已采集）EOD→预计算→次日成员采集。
    if dtime(9) <= t < dtime(9,28):
        jobs.append(('ml_r1_preopen', day))
        jobs.append(('ai_report_morning', day+'T'+now.strftime('%H')+str(now.minute//10)))
    if t >= dtime(15,12):
        jobs.append(('ml_r1_eod', day+':eod'))
    return jobs


async def perform(job: str, now: datetime, days: list[str], slot: str = ''):
    from app.services import auction, boards, daily_close, index_feed, index_history, intraday, lhb, movement, pools, popular, topic
    from app.services import ai_report
    day = now.date().isoformat()
    if job == 'ml_r1_preopen':
        from app.services import ml_r1_kline
        return await ml_r1_kline.collect_universe()
    if job == 'ml_r1_eod':
        from app.services import ml_r1_kline, ml_r1_pipeline
        try:
            eod = await ml_r1_kline.update_eod(day)
        except Exception as exc:
            eod = {'status': 'missing_input', 'error': type(exc).__name__}
        out = {'kline_rows': eod.get('kline_rows'), 'action_days': eod.get('action_days'),
               'universe_total': (eod.get('universe') or {}).get('total'),
               'source_as_of': eod.get('source_as_of'),
               'stages': {'quotes': {'status': eod.get('status'), 'error': eod.get('error')}}}
        for pt in ml_r1_pipeline.prepared_types(day):
            try:
                res = await asyncio.to_thread(lambda t=pt: ml_r1_pipeline.precompute(ml_r1_pipeline.build_local_input(day, t), t))
                out['pt%d_status' % pt] = res.get('status')
                out['pt%d_reasons' % pt] = (res.get('reasons') or [])[:8]
            except Exception as exc:  # noqa: BLE001 - 单侧失败不阻塞成员采集
                out['pt%d_error' % pt] = type(exc).__name__
        bc = ml_r1_kline.board_codes_for(day)
        codes = sorted(set(bc.get('14', []) + bc.get('15', [])))
        if not codes:  # 当日目录缺失：退回最新已知目录，保证次日成员点时门禁
            codes = sorted({p['plate_code'] for pt2 in (14, 15)
                            for p in ml_r1_pipeline.native_catalog(pt2)})
        mem = await ml_r1_kline.collect_all_members(codes, concurrency=6)
        out['members_saved'] = mem.get('saved')
        out['member_errors'] = mem.get('error_count')
        out['stages']['members'] = {'status': 'available' if not mem.get('error_count') else 'partial_preview', 'saved': mem.get('saved')}
        out['stages']['publication'] = {str(t): out.get('pt%d_status' % t, 'missing_input') for t in ml_r1_pipeline.prepared_types(day)}
        out['status'] = 'final' if all(out.get('pt%d_status' % t) == 'final' for t in ml_r1_pipeline.prepared_types(day)) else 'missing_input'
        return out
    if job == 'auction_candidates':
        data = await auction.candidates(day,days[-2])
        return {'count':len(data['stocks'])}
    if job in ('auction','auction_close'):
        data = await auction.collect(day,days)
        try:
            data['limit_buy_backfill'] = auction.backfill_limit_buy(day)
        except Exception:
            data['limit_buy_backfill'] = None
        if data['errors'] or data['received'] < data['expected']:
            raise ValueError('Incomplete auction quote batch')
        return data
    if job == 'limit_pools':
        data = await pools.collect_session(day)
        return data
    if job == 'block_top':
        data = await pools.collect_block_top(day)
        return {'date': data['date'], 'total': data['total']}
    if job == 'index_trends':
        data = await index_feed.collect(day)
        return {'date':data['date'],'slot':data['slot'],'total':data['total']}
    if job == 'lhb':
        data = await lhb.collect_with_seats(day)
        return {'date':data['date'],'total':data['total']}
    if job.startswith('board_'):
        data = await boards.collect(2 if job=='board_industry' else 3)
        if data['trade_date'] != day:
            raise ValueError('Board source date differs from current session')
        # 盘后本地增量预计算与缺口快照；与上游抓取分离，页面GET不补数。
        if datetime.now(ZoneInfo('Asia/Shanghai')).hour >= 15:
            from app.services.ml_r1_pipeline import build_local_input, precompute
            typ = 14 if job == 'board_industry' else 15
            try:
                prepared = await asyncio.to_thread(lambda: precompute(build_local_input(day, typ), typ))
                data['ml_r1'] = {'status': prepared['status'], 'snapshot_id': prepared['snapshot_id']}
            except ValueError as exc:
                data['ml_r1'] = {'status': 'missing_input', 'reason': str(exc)}
            data['stages'] = {'collection': 'available', 'calculation': data['ml_r1']['status']}
            data['status'] = data['ml_r1']['status']
        return data
    if job == 'market_minute':
        data = await intraday.sample_minute()
        if not data:
            raise ValueError('Minute collection produced no sample')
        return {'date':data['date'],'minute':data['minute'],'source_as_of':data.get('source_as_of'),
                'hs_count':data.get('hs_count'),'bj_count':data.get('bj_count')}
    if job == 'daily_close':
        count = await daily_close.backfill_day(day)
        if not count:
            raise ValueError('Daily close collection produced no rows')
        # 收盘统一口径：用已发布 daily_close（含北交所 prev_close）重算当日分钟样本
        # 的横截面字段，覆盖盘中实时快照可能的口径漂移（见 docs 待解决清单 sentiment 条目）
        intraday.reclose_minute_samples(day)
        return {'date':day,'count':count}
    if job == 'stock_flow':
        from app.services import plate_flow
        data = await plate_flow.collect_flow(day)
        from app.services.ml_r1_pipeline import build_local_input, precompute, prepared_types
        data['calculation'] = {}
        for pt in prepared_types(day):
            try:
                prepared = await asyncio.to_thread(lambda t=pt: precompute(build_local_input(day,t),t))
                data['calculation'][str(pt)] = prepared['status']
            except ValueError as exc:
                data['calculation'][str(pt)] = 'missing_input'
        data['status'] = 'final' if all(v=='final' for v in data['calculation'].values()) else 'missing_input'
        return data
    if job == 'free_hotspot_members':
        from app.services import free_hotspots
        ranked = await free_hotspots.rank_days(day, 1, 1, 12, 15)
        if not ranked:
            raise ValueError('No current free board ranking available')
        saved, errors = 0, []
        for row in ranked:
            try:
                await free_hotspots.collect_members(row['plate_code'])
                saved += 1
            except Exception as exc:
                errors.append({'code': row['plate_code'], 'error_type': type(exc).__name__})
                if len(errors) >= 3:
                    break
        return {'date': day, 'saved': saved, 'errors': errors, 'source': 'eastmoney_public',
                'status': 'partial_preview' if errors else 'available'}
    if job == 'kaipanla_plate_intraday':
        from app.services import plate_rank_refresh
        return await plate_rank_refresh.refresh_intraday(day, now=now)
    if job == 'kaipanla_plate_finalize':
        from app.services import plate_rank_refresh
        return await plate_rank_refresh.finalize_today(day, now=now)
    if job == 'kaipanla_plate_history':
        from app.services import plate_rank_refresh
        return await plate_rank_refresh.backfill_history(day, lookback=20)
    if job == 'kaipanla_members':
        from app.services import kaipanla_members
        members = await kaipanla_members.collect_top_members(end=day, concurrency=1, only_missing=False)
        names = await kaipanla_members.backfill_stock_names(concurrency=8)
        quotes = await kaipanla_members.backfill_member_quotes(n_days=25, concurrency=12)
        return {'members': members, 'names': names, 'quotes': quotes}
    if job in ('plate_rank', 'plate_reason', 'plate_members'):
        from app.config import settings
        if not settings.enable_origin_reference:
            raise ValueError('Origin calibration collection is disabled')
    if job == 'plate_rank':
        from app.services import plate_flow
        data = await plate_flow.collect_plate_rank(day)
        return data
    if job == 'plate_reason':
        from app.services import plate_flow
        # 15 plates x 6.5s throttle + rank request ~= 105s, inside the 180s job timeout
        data = await plate_flow.collect_plate_reasons_for(day, plate_types=(17,), top_n=15)
        return data
    if job == 'plate_members':
        from app.services import plate_flow
        # 每小时一批，按快照中 curated 板块列表切片（最后一小时取剩余全部）
        plates = plate_flow.load_members()
        codes = [c for c, v in plates.items() if v.get('sub_plates')]
        try:
            batch = max(1, int(slot.rsplit('T', 1)[-1][:2]) - 19)  # 20->1, 21->2, 22->3
        except (ValueError, IndexError):
            batch = 1
        batches = 3
        per = (len(codes) + batches - 1) // batches
        start = (batch - 1) * per
        chunk = codes[start:] if batch == batches else codes[start:start + per]
        data = await plate_flow.collect_members(day, chunk)
        return {'date': data['date'], 'saved': data['saved'], 'batch': batch, 'total': len(chunk)}
    if job == 'topic_close':
        return await topic.backfill_day(day,limit=1000)
    if job == 'index_history':
        data = await index_history.collect(day)
        return {'date':data['date'],'coverage':data['coverage']}
    if job == 'movement':
        data = await movement.collect(day)
        return {'date':data['date'],'total':data['total'],'notice_count':data.get('notice_count')}
    if job == 'popular':
        minute = slot.split('T', 1)[1] if slot and 'T' in slot else None
        data = await popular.collect(day, minute=minute)
        try:
            codes = [i['symbol_code'] for i in data.get('items') or [] if i.get('symbol_code')]
            patched = await daily_close.patch_turnover(dates=[day], codes=codes)
            data['turnover_patched'] = patched.get('total') or 0
        except Exception:
            data['turnover_patched'] = 0
        return {'date':data['date'],'total':data['total'],'quote_coverage':data.get('quote_coverage'),'turnover_patched':data.get('turnover_patched')}
    if job == 'popular_deep':
        from app.config import settings
        if not settings.enable_origin_reference:
            raise ValueError('Origin calibration collection is disabled')
        return await popular.collect_origin_ths_top(day)
    if job == 'ai_report':
        evening = await ai_report.build('evening', day)
        return {'date': day, 'evening': evening.get('id'), 'status': evening.get('status')}
    if job == 'ai_report_morning':
        morning = await ai_report.build('morning', day)
        return {'date': day, 'morning': morning.get('id'), 'status': morning.get('status')}
    if job == 'ths_board_daily':
        from app.services.ths_board_backfill import backfill as ths_backfill
        report = await ths_backfill(workers=4)
        if report['saved'] + report['skipped_fresh'] < report['boards_total'] * 0.8:
            raise ValueError('THS board refresh below 80% coverage')
        return {'saved': report['saved'], 'skipped_fresh': report['skipped_fresh'],
                'errors': len(report['errors']), 'total': report['boards_total']}
    raise ValueError('Unknown collector job')


class Collector:
    def __init__(self, db: Store = store):
        self.db = db
        self.owner = f'{os.getpid()}:{uuid.uuid4().hex}'
        self.lock = ProcessLock(os.path.realpath(db.path)+'.collector.lock')
        self.active = {}
        self.calendar = None
        self.calendar_checked = 0
        self.calendar_error = None
        self.heartbeat = None
        self.started_code = code_fingerprint()

    async def heartbeat_loop(self):
        while True:
            try:
                from app.core.freshness import run_check
                run_check(self.db)
            except Exception as exc:  # 监控绝不能拖垮心跳
                logger.warning('freshness check failed: %s', type(exc).__name__)
            self.db.kv_set('collector_runtime_v1',{'owner':self.owner,'pid':os.getpid(),'status':'running',
                'heartbeat_at':time.time(),'heartbeat_iso':datetime.now(TZ).isoformat(),
                'code_fingerprint':self.started_code,
                'active_jobs':[job for job,task in self.active.items() if not task.done()],
                'calendar_error':self.calendar_error,'scope':'single_host_same_database'})
            await asyncio.sleep(5)

    async def run_job(self, job: str, slot: str, action, *, at=None, timeout: float = 180):
        clock = at or time.time
        problem_key = 'collector_problem:' + job
        bounded = True  # 同一源错误跨调度slot最多3次，避免后台无限消耗。
        problem = self.db.kv_get(problem_key,{}) if bounded else {}
        if problem.get('attempts',0) >= 3:
            return 'attempt_limit_reached'
        previous = self.db.collector_run(job,slot)
        if previous and previous['status'] in ('success', 'partial'):
            return 'already_completed'
        if previous and previous.get('attempts', 0) >= 3 and previous['status'] == 'failed':
            return 'attempt_limit_reached'
        if previous and previous['status'] == 'failed' and clock()-(previous['finished_at'] or 0)<60:
            return 'retry_later'
        self.db.collector_start(job,slot,self.owner,clock())
        try:
            async with asyncio.timeout(timeout):
                detail = await action()
        except asyncio.CancelledError:
            self.db.collector_finish(job,slot,self.owner,'interrupted',clock(),{'reason':'collector_stopped'})
            raise
        except Exception as exc:
            self.db.collector_finish(job,slot,self.owner,'failed',clock(),{'error_type':type(exc).__name__})
            if bounded:
                attempts = problem.get('attempts',0)+1 if problem.get('error_type') == type(exc).__name__ else 1
                self.db.kv_set(problem_key,{'attempts':attempts,'error_type':type(exc).__name__,
                                           'slot':slot,'at':datetime.now(TZ).isoformat(),
                                           'state':'attempt_limit_reached' if attempts>=3 else 'retry_pending'})
            logger.warning('collector job %s failed: %s',job,type(exc).__name__)
            return 'failed'
        if bounded and problem:
            self.db.kv_set(problem_key,{'attempts':0,'resolved_at':datetime.now(TZ).isoformat()})
        outcome = 'partial' if (detail or {}).get('status') in ('missing_input', 'partial_preview', 'unavailable') else 'success'
        self.db.collector_finish(job,slot,self.owner,outcome,clock(),detail or {})
        return outcome

    async def confirmed_days(self, now):
        if self.calendar is None or time.monotonic()-self.calendar_checked >= 60:
            from app.services.auction import sessions
            self.calendar = await sessions(now.date().isoformat())
            self.calendar_checked = time.monotonic()
            self.db.kv_set('collector_calendar_v1',{'days':self.calendar,'verified_at':now.isoformat()})
        return self.calendar

    async def run(self):
        try:
            while not self.lock.acquire():
                # Standby process does not overwrite active owner's heartbeat.
                await asyncio.sleep(5)
            logger.info('collector owner acquired: %s',self.owner)
            self.heartbeat = asyncio.create_task(self.heartbeat_loop())
            while True:
                if self.heartbeat.done():
                    self.heartbeat.result()
                now = datetime.now(TZ)
                plan = due_jobs(now)
                calendar_error = None
                try:
                    days = await self.confirmed_days(now) if plan else []
                    if now.date().isoformat() not in days or len(days)<2:
                        plan = []
                except Exception as exc:
                    calendar_error = type(exc).__name__
                    plan, days = [], []
                for job,task in list(self.active.items()):
                    if task.done():
                        try:
                            task.result()
                        except Exception as exc:
                            logger.error('collector task state failure %s: %s',job,type(exc).__name__)
                        del self.active[job]
                for job,slot in plan:
                    if job not in self.active:
                        # 收盘派生：同 slot 的 daily_close 未成功且库内日线不足时，不抢跑 movement
                        if job == 'movement':
                            day = slot.split(':', 1)[0]
                            close_prev = self.db.collector_run('daily_close', slot)
                            close_ok = close_prev and close_prev.get('status') in ('success', 'partial')
                            if not close_ok:
                                from app.services import movement as movement_svc
                                try:
                                    movement_svc.ensure_close_ready(day)
                                except ValueError:
                                    continue
                        self.active[job] = asyncio.create_task(self.run_job(job,slot,lambda j=job,n=now,d=days,s=slot:perform(j,n,d,s),timeout=JOB_TIMEOUTS.get(job,180)))
                self.calendar_error = calendar_error
                await asyncio.sleep(5)
        finally:
            tasks = list(self.active.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            if self.heartbeat:
                self.heartbeat.cancel()
                await asyncio.gather(self.heartbeat,return_exceptions=True)
            if self.lock.file:
                self.db.kv_set('collector_runtime_v1',{'owner':self.owner,'pid':os.getpid(),'status':'stopped','heartbeat_at':time.time()})
                self.lock.close()


def status(db: Store = store) -> dict:
    runtime = db.kv_get('collector_runtime_v1',{})
    age = max(0,time.time()-runtime['heartbeat_at']) if runtime.get('heartbeat_at') else None
    started = runtime.get('code_fingerprint') or {}
    current = code_fingerprint()
    code_stale = bool(started) and started != current
    return {'online':runtime.get('status')=='running' and age is not None and age<30,
            'heartbeat_age_seconds':round(age,1) if age is not None else None,
            'code_stale':code_stale,
            'runtime':runtime,'recent_jobs':db.collector_recent()}
