import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock,patch

from app.core.collector import Collector, ProcessLock, code_fingerprint, due_jobs, status, TZ
from app.core.store import Store


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Store(str(Path(self.tmp.name)/'db.sqlite'))
    def tearDown(self):self.tmp.cleanup()

    def test_status_reports_stale_code_after_source_change(self):
        """Worker fingerprints code at start; status must flag drift after edits."""
        runner=Collector(self.db)
        self.db.kv_set('collector_runtime_v1',{'owner':runner.owner,'status':'running',
            'heartbeat_at':__import__('time').time(),'code_fingerprint':runner.started_code})
        current=status(self.db)
        self.assertFalse(current['code_stale'])
        drifted=dict(runner.started_code)
        drifted['total_bytes']=drifted['total_bytes']+1
        self.db.kv_set('collector_runtime_v1',{'owner':runner.owner,'status':'running',
            'heartbeat_at':__import__('time').time(),'code_fingerprint':drifted})
        self.assertTrue(status(self.db)['code_stale'])
        self.assertEqual(code_fingerprint(),{'newest_mtime':runner.started_code['newest_mtime'],
            'total_bytes':runner.started_code['total_bytes']})

    def test_cross_process_lock_releases_after_owner_exit(self):
        path=str(Path(self.tmp.name)/'runtime.lock')
        script='from app.core.collector import ProcessLock; import sys; l=ProcessLock(sys.argv[1]); print(l.acquire(),flush=True); sys.stdin.read()'
        process=subprocess.Popen([sys.executable,'-c',script,path],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        challenger=ProcessLock(path)
        try:
            self.assertEqual(process.stdout.readline().strip(),'True')
            self.assertFalse(challenger.acquire())
            process.kill();process.wait(timeout=5)
            self.assertTrue(challenger.acquire())
        finally:
            challenger.close()
            if process.poll() is None:process.kill();process.wait()
            process.stdin.close();process.stdout.close()

    async def test_durable_success_skipped_after_restart_and_failure_retry(self):
        job=AsyncMock(return_value={'count':5})
        runner=Collector(self.db)
        self.assertEqual(await runner.run_job('sample','s1',job,at=lambda:100),'success')
        runner=Collector(self.db)
        self.assertEqual(await runner.run_job('sample','s1',job,at=lambda:101),'already_completed')
        self.assertEqual(job.await_count,1)
        job.side_effect=RuntimeError('no network')
        self.assertEqual(await runner.run_job('sample','s2',job,at=lambda:100),'failed')
        self.assertEqual(await runner.run_job('sample','s2',job,at=lambda:110),'retry_later')
        job.side_effect=None
        self.assertEqual(await runner.run_job('sample','s2',job,at=lambda:161),'success')
        self.assertEqual(self.db.collector_run('sample','s2')['attempts'],2)

    async def test_same_source_error_is_bounded_across_slots(self):
        action=AsyncMock(side_effect=ValueError('source unavailable'))
        runner=Collector(self.db)
        for slot in ('a','b','c'):
            self.assertEqual(await runner.run_job('board_concept',slot,action),'failed')
        self.assertEqual(await Collector(self.db).run_job('board_concept','d',action),'attempt_limit_reached')
        self.assertEqual(action.await_count,3)

    async def test_cancelled_job_records_interruption(self):
        event=asyncio.Event()
        async def action():
            event.set();await asyncio.Event().wait()
        runner=Collector(self.db)
        task=asyncio.create_task(runner.run_job('slow','s1',action))
        await event.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(self.db.collector_run('slow','s1')['status'],'interrupted')

    def test_schedule_boundaries_and_completed_slots(self):
        def jobs(t,day='2026-09-10'):
            return dict(due_jobs(datetime.fromisoformat(day+'T'+t).replace(tzinfo=TZ)))
        self.assertIn('auction_candidates',jobs('09:24:59'))
        self.assertNotIn('auction',jobs('09:24:59'))
        self.assertEqual(jobs('09:25:17')['auction'],'2026-09-10T09:25:15')
        self.assertNotIn('auction',jobs('09:30:01'))
        self.assertNotIn('market_minute',jobs('09:30:04'))
        self.assertIn('market_minute',jobs('11:30:05'))
        self.assertNotIn('market_minute',jobs('12:00:05'))
        self.assertIn('market_minute',jobs('15:00:05'))
        self.assertNotIn('market_minute',jobs('15:01:05'))
        self.assertTrue(jobs('20:05:00')['daily_close'].endswith(':late'))
        self.assertTrue(jobs('20:05:00')['movement'].endswith(':late'))
        self.assertTrue(jobs('20:05:00')['index_history'].endswith(':late'))
        self.assertTrue(jobs('20:05:00')['popular'].endswith(':late'))
        self.assertTrue(jobs('20:05:00')['block_top'].endswith(':late'))
        self.assertNotIn('block_top', jobs('09:30:05'))
        self.assertIn('popular', jobs('09:30:05'))
        self.assertTrue(jobs('20:05:00')['ai_report'].endswith(':late'))
        self.assertNotIn('plate_members', jobs('20:35:00'))
        self.assertNotIn('plate_members', jobs('21:30:00'))
        self.assertIn('free_hotspot_members', jobs('10:10:00'))
        self.assertEqual(jobs('10:10:05')['kaipanla_plate_intraday'], '2026-09-10T10:10')
        self.assertNotIn('kaipanla_plate_intraday', jobs('10:11:05'))
        self.assertEqual(jobs('15:35:00')['kaipanla_plate_finalize'], '2026-09-10:finalize:1530')
        self.assertEqual(jobs('16:05:00')['kaipanla_plate_finalize'], '2026-09-10:finalize:1600')
        self.assertEqual(jobs('20:05:00')['kaipanla_plate_finalize'], '2026-09-10:finalize:2000')
        self.assertEqual(jobs('16:05:00')['kaipanla_plate_history'], '2026-09-10:history')
        self.assertNotIn('plate_members', jobs('20:25:00'))
        self.assertNotIn('plate_members', jobs('23:30:00'))
        self.assertEqual(jobs('09:26:00','2026-09-12'),{})

    def test_liveness_is_not_inferred_from_old_status(self):
        self.assertFalse(status(self.db)['online'])
        self.db.kv_set('collector_runtime_v1',{'status':'running','heartbeat_at':1})
        self.assertFalse(status(self.db)['online'])

    async def test_api_external_mode_does_not_start_collectors(self):
        from app.main import app,lifespan
        from app.config import settings
        with patch.object(settings,'collector_mode','external'),patch('app.main.start_scheduler',AsyncMock()) as scheduler:
            async with lifespan(app):pass
        scheduler.assert_not_awaited()


    async def test_published_board_and_auction_reads_never_collect(self):
        from app.services import auction,boards
        from app.config import settings
        self.db.kv_set('collector_calendar_v1',{'days':['2026-09-09','2026-09-10']})
        with patch.object(settings,'collector_mode','external'),patch.object(auction,'store',self.db),patch.object(boards,'store',self.db),patch.object(auction,'collect',AsyncMock()) as a,patch.object(boards,'collect',AsyncMock()) as b,patch.object(auction,'sessions',AsyncMock()) as calendar:
            await auction.review('2026-09-10')
            await boards.evolution(3,'2026-09-10',3,12,'pct')
        a.assert_not_awaited();b.assert_not_awaited();calendar.assert_not_awaited()
