"""Probe free EW daily_nav for a plate without origin OHLC cache."""
import asyncio
from app.config import settings
from app.core.cache import cache
from app.services import market

# Clear cache so we don't hit stale empty/origin responses
cache.clear() if hasattr(cache, "clear") else None

async def main():
    # Force free path for a code we orphan from origin
    settings.enable_origin_reference = False
    data = await market.plate_kline_contract("801003", "main", 30)
    print("801003 free", data.get("status"), data.get("series_kind"),
          "n", len(data.get("x") or []), "asof", data.get("as_of_date"),
          "y0", (data.get("y") or [None])[0], "y-1", (data.get("y") or [None])[-1],
          "reason", data.get("reason"))
    settings.enable_origin_reference = True
    # plate without cache — may hit upstream; prefer nav if we mock empty
    from unittest.mock import AsyncMock, patch
    from app.datasources import zizizaizai
    with patch.object(zizizaizai, "plate_kline_main", AsyncMock(return_value={"x": [], "y": []})):
        cache.clear() if hasattr(cache, "clear") else None
        # wipe any in-memory hit by unique n
        data2 = await market.plate_kline_contract("801777", "main", 20)
    print("801777 nav-fallback", data2.get("status"), data2.get("series_kind"),
          "n", len(data2.get("x") or []), "reason", data2.get("reason"))

asyncio.run(main())
