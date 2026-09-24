"""data_fallback 的免费优先、预算和并发边界测试。"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.store import Store  # noqa: E402
from app.services import data_fallback  # noqa: E402


class DataFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "db.sqlite"))
        data_fallback.configure_store(self.store)

    def tearDown(self):
        data_fallback.configure_store(None)
        self.tmp.cleanup()

    async def test_free_success_never_calls_paid(self):
        free = AsyncMock(return_value=[{"date1": "2026-09-10", "pct": 1.2}])
        paid = AsyncMock(return_value=[{"date1": "paid"}])
        validate = AsyncMock(return_value=True)

        result = await data_fallback.resolve("board_history", free, validate, paid)

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "free")
        paid.assert_not_awaited()
        self.assertEqual(validate.await_count, 1)

    async def test_paid_closed_without_key_or_budget_is_never_called(self):
        cases = (
            {"QVERIS_PAID_FALLBACK_ENABLED": "0", "QVERIS_API_KEY": "secret", "QVERIS_PAID_DAILY_LIMIT": "2"},
            {"QVERIS_PAID_FALLBACK_ENABLED": "1", "QVERIS_API_KEY": "", "QVERIS_PAID_DAILY_LIMIT": "2"},
            {"QVERIS_PAID_FALLBACK_ENABLED": "1", "QVERIS_API_KEY": "secret", "QVERIS_PAID_DAILY_LIMIT": "0"},
        )
        for env in cases:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=False):
                free = AsyncMock(side_effect=ValueError("free unavailable"))
                paid = AsyncMock(return_value=[1])
                result = await data_fallback.resolve("board_history", free, lambda data: bool(data), paid)
                self.assertFalse(result["ok"])
                paid.assert_not_awaited()

    async def test_concurrent_reservations_do_not_exceed_daily_budget(self):
        with patch.dict(
            os.environ,
            {
                "QVERIS_PAID_FALLBACK_ENABLED": "1",
                "QVERIS_API_KEY": "test-key",
                "QVERIS_PAID_DAILY_LIMIT": "3",
            },
            clear=False,
        ):
            paid_calls = 0
            paid_lock = asyncio.Lock()

            async def paid():
                nonlocal paid_calls
                async with paid_lock:
                    paid_calls += 1
                await asyncio.sleep(0)
                return [1]

            async def free():
                await asyncio.sleep(0)
                raise ValueError("network down")

            results = await asyncio.gather(
                *(data_fallback.resolve("board_history", free, lambda data: bool(data), paid) for _ in range(20))
            )

        self.assertEqual(paid_calls, 3)
        self.assertEqual(sum(result["paid_attempted"] for result in results), 3)
        with self.store._conn() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT day, reserved_count FROM data_fallback_paid_budget"
            ).fetchone()
        # The implementation uses Asia/Shanghai explicitly; compare the only row to avoid
        # relying on the host SQLite timezone setting.
        self.assertIsNotNone(row)
        self.assertEqual(row[0], data_fallback._today_shanghai())  # noqa: SLF001
        self.assertEqual(row[1], 3)

    async def test_paid_failure_consumes_reserved_budget_without_retry(self):
        with patch.dict(
            os.environ,
            {
                "QVERIS_PAID_FALLBACK_ENABLED": "1",
                "QVERIS_API_KEY": "test-key",
                "QVERIS_PAID_DAILY_LIMIT": "2",
            },
            clear=False,
        ):
            paid = AsyncMock(side_effect=RuntimeError("provider down"))
            for _ in range(3):
                result = await data_fallback.resolve("board_history", lambda: None, lambda data: False, paid)
                self.assertFalse(result["ok"])
            self.assertEqual(paid.await_count, 2)

        with self.store._conn() as conn:  # noqa: SLF001
            row = conn.execute("SELECT reserved_count FROM data_fallback_paid_budget").fetchone()
        self.assertEqual(row[0], 2)

    async def test_free_failure_cooldown_avoids_unbounded_retries(self):
        free = AsyncMock(side_effect=RuntimeError("free provider down"))
        paid = AsyncMock(return_value=[1])
        with patch.dict(
            os.environ,
            {
                "QVERIS_PAID_FALLBACK_ENABLED": "0",
                "QVERIS_API_KEY": "",
                "QVERIS_PAID_DAILY_LIMIT": "0",
            },
            clear=False,
        ):
            first = await data_fallback.resolve("board_history", free, lambda data: False, paid, "same-request")
            second = await data_fallback.resolve("board_history", free, lambda data: False, paid, "same-request")

        self.assertEqual(first["reason"], "paid_disabled")
        self.assertEqual(second["reason"], "free_cooldown")
        self.assertEqual(free.await_count, 1)
        paid.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
