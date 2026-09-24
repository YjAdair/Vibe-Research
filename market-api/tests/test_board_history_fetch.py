"""board_history.fetch_series 多主机容错行为测试。"""
import asyncio
import sys
import os
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402
from app.core.cache import TTLCache  # noqa: E402
from app.datasources import board_history  # noqa: E402


def _ok_response(code='BK0475', klines=('2026-09-01,10,11,12,9,100,1000,30,10,1,2',)):
    return {'rc': 0, 'data': {'code': code, 'klines': list(klines)}}


class TestFetchSeriesHosts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_failure_cache = board_history._failure_cache
        self.old_last_request_ts = board_history._last_request_ts
        self.old_request_lock = board_history._request_lock
        board_history._failure_cache = TTLCache()
        board_history._last_request_ts = 0.0
        board_history._request_lock = asyncio.Lock()
        self.sleep = AsyncMock()
        self.sleep_patch = patch.object(board_history.asyncio, 'sleep', self.sleep)
        self.sleep_patch.start()

    def tearDown(self):
        self.sleep_patch.stop()
        board_history._failure_cache = self.old_failure_cache
        board_history._last_request_ts = self.old_last_request_ts
        board_history._request_lock = self.old_request_lock

    async def test_empty_klines_fails_fast(self):
        """空序列最多由三个主机确认，不能扩张成多轮请求。"""
        fetch = AsyncMock(return_value=_ok_response(klines=()))
        with patch.object(board_history, 'fetch_json', fetch):
            with self.assertRaises(ValueError) as cm:
                await board_history.fetch_series('BK0475', '2026-09-01', '2026-09-10')
        self.assertIn('No historical bars', str(cm.exception))
        self.assertEqual(fetch.await_count, 3)

    async def test_first_host_down_rotates_to_second(self):
        """主机 1 断连 -> 轮转主机 2 成功返回。"""
        responses = [
            httpx.RemoteProtocolError('Server disconnected'),
            _ok_response(),
        ]

        async def side_effect(url, **kw):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with patch.object(board_history, 'fetch_json', side_effect=side_effect):
            rows = await board_history.fetch_series('BK0475', '2026-09-01', '2026-09-10')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['price'], 11)

    async def test_all_hosts_down_raises_last_error(self):
        """全部主机断连一次后进入冷却，紧接调用不新增 HTTP。"""
        err = httpx.RemoteProtocolError('Server disconnected')
        with patch.object(board_history, 'fetch_json', AsyncMock(side_effect=err)) as fetch:
            with self.assertRaises(httpx.RemoteProtocolError):
                await board_history.fetch_series('BK0475', '2026-09-01', '2026-09-10')
            self.assertEqual(fetch.await_count, 3)
            with self.assertRaises(httpx.RemoteProtocolError):
                await board_history.fetch_series('BK0475', '2026-09-01', '2026-09-10')
        self.assertEqual(fetch.await_count, 3)

    async def test_flow_series_rotates_hosts(self):
        """资金流接口同样走多主机轮转。"""
        responses = [
            httpx.RemoteProtocolError('down'),
            httpx.RemoteProtocolError('down'),
            {'rc': 0, 'data': {'code': 'BK0475', 'klines': ['2026-09-01,-25,10,5,-20,-5,-2.5,1,.5,-2,-.5,11,10,0,0']}},
        ]

        async def side_effect(url, **kw):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with patch.object(board_history, 'fetch_json', side_effect=side_effect) as fetch:
            rows = await board_history.fetch_series('BK0475', '2026-09-01', '2026-09-10', flow=True)
        self.assertEqual(rows[0]['main_net_inflow'], -25)
        self.assertEqual(fetch.await_count, 3)

    async def test_schema_error_stops_host_rotation(self):
        """schema 错误立即停止，不把坏响应扩散到其他主机。"""
        malformed = _ok_response(klines=('2026-09-01,10,11,12,9,100,1000,30,10,1,2,extra',))
        fetch = AsyncMock(return_value=malformed)
        with patch.object(board_history, 'fetch_json', fetch):
            with self.assertRaisesRegex(ValueError, 'Unexpected historical field count'):
                await board_history.fetch_series('BK0475', '2026-09-01', '2026-09-10')
        self.assertEqual(fetch.await_count, 1)

    async def test_three_failed_calls_enter_cooldown_then_expire(self):
        """三 host 失败后冷却 5 分钟，冷却期间不发请求，过期后恢复探测。"""
        err = httpx.RemoteProtocolError('Server disconnected')
        responses = [err] * 3 + [_ok_response()]
        fetch = AsyncMock(side_effect=responses)
        clock = [1000.0]

        with patch('app.core.cache.time.monotonic', lambda: clock[0]), \
                patch.object(board_history, 'fetch_json', fetch):
            with self.assertRaises(httpx.RemoteProtocolError):
                await board_history.fetch_series('BK0475', '2026-09-01', '2026-09-10')
            self.assertEqual(fetch.await_count, 3)

            with self.assertRaises(httpx.RemoteProtocolError):
                await board_history.fetch_series('BK0475', '2026-09-01', '2026-09-10')
            self.assertEqual(fetch.await_count, 3)

            clock[0] += board_history.FAILURE_COOLDOWN_SECONDS
            rows = await board_history.fetch_series('BK0475', '2026-09-01', '2026-09-10')

        self.assertEqual(fetch.await_count, 4)
        self.assertEqual(rows[0]['price'], 11)

    async def test_concurrent_same_key_uses_singleflight(self):
        """同 key 并发请求共享一次三 host 失败流程。"""
        err = httpx.RemoteProtocolError('Server disconnected')
        fetch = AsyncMock(side_effect=err)
        with patch.object(board_history, 'fetch_json', fetch):
            results = await asyncio.gather(*(
                board_history.fetch_series('BK0475', '2026-09-01', '2026-09-10')
                for _ in range(3)
            ), return_exceptions=True)

        self.assertEqual(fetch.await_count, 3)
        self.assertTrue(all(isinstance(result, httpx.RemoteProtocolError) for result in results))


if __name__ == '__main__':
    unittest.main()
