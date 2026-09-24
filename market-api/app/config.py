"""全局配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool = False) -> bool:
    """读取需要显式开启的布尔环境变量。"""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    app_name: str = "自在量化复刻版"
    app_version: str = "0.1.0"

    # 网络
    http_timeout: float = 12.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )

    # 缓存（秒）
    cache_ttl_realtime: int = 5      # 实时行情/分时，交易时段高频
    cache_ttl_ztpool: int = 30       # 涨停池
    cache_ttl_lhb: int = 300         # 龙虎榜
    cache_ttl_kline: int = 300       # 日K
    cache_ttl_sentiment: int = 60    # 情绪周期
    cache_ttl_topic: int = 60        # 题材热度

    # 数据源开关
    enable_tencent: bool = True
    enable_sina: bool = True
    enable_eastmoney: bool = True
    # 原站接口仅允许开发期离线旧数据对照；必须显式设置环境变量才可回源校准。
    enable_origin_reference: bool = field(
        default_factory=lambda: _env_bool("ZZQUANT_ENABLE_ORIGIN_REFERENCE", False)
    )

    # 交易时段（Asia/Shanghai）
    market_timezone: str = "Asia/Shanghai"
    # external: API never owns collectors; embedded is for single-process development.
    collector_mode: str = field(default_factory=lambda: os.environ.get('ZZQUANT_COLLECTOR_MODE', 'external'))


    # 认证（JWT 签名密钥；生产环境必须用环境变量覆盖）
    auth_secret: str = field(
        default_factory=lambda: os.environ.get(
            "ZZQUANT_AUTH_SECRET", "zzquant-dev-secret-do-not-use-in-prod"
        )
    )

    # 数据库
    db_path: str = field(
        default_factory=lambda: os.environ.get(
            "ZZQUANT_DB", os.path.join(os.path.dirname(__file__), "..", "zzquant.db")
        )
    )


settings = Settings()
