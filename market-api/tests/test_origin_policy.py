import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.config import Settings, settings
from app.datasources import zizizaizai


class OriginPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_origin_network_functions_are_blocked_before_http_or_throttle(self):
        calls = [
            (zizizaizai.plates_rank, (17, "2026-09-11")),
            (zizizaizai.plates_rank_days, (17, "2026-09-11")),
            (zizizaizai.plate_popular_reason, ("801660",)),
            (zizizaizai.sub_plates_stocks, ("801660", ["2026-09-11"])),
            (zizizaizai.plate_kline_main, ("801660",)),
            (zizizaizai.plate_kline_sub, ("801660",)),
        ]
        sleep = AsyncMock()
        client = Mock()

        with patch.object(settings, "enable_origin_reference", False), \
                patch.object(zizizaizai.asyncio, "sleep", sleep), \
                patch.object(zizizaizai.httpx, "AsyncClient", client):
            for function, args in calls:
                with self.subTest(function=function.__name__):
                    with self.assertRaises(zizizaizai.OriginReferenceDisabledError):
                        await function(*args)

        client.assert_not_called()
        sleep.assert_not_awaited()

    def test_origin_reference_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(Settings().enable_origin_reference)

    def test_origin_reference_requires_explicit_environment_truth(self):
        env_name = "ZZQUANT_ENABLE_ORIGIN_REFERENCE"
        with patch.dict(os.environ, {env_name: "true"}, clear=False):
            self.assertTrue(Settings().enable_origin_reference)
        with patch.dict(os.environ, {env_name: "1"}, clear=False):
            self.assertTrue(Settings().enable_origin_reference)
        with patch.dict(os.environ, {env_name: "false"}, clear=False):
            self.assertFalse(Settings().enable_origin_reference)


if __name__ == "__main__":
    unittest.main()
