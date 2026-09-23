"""服务入口：构造默认服务并暴露 ASGI ``app``。

默认配置来源可通过环境变量 RUNTIME_CONFIG_FILES（冒号/分号分隔）覆盖。
默认 strict=True：本地配置无效时拒绝启动并打印可区分原因。
"""

from __future__ import annotations

import os

from app.api import create_app
from app.service import RuntimeConfigService

_DEFAULT_SOURCES = ["config/base.json", "config/local.json"]


def _resolve_sources() -> list[str]:
    raw = os.environ.get("RUNTIME_CONFIG_FILES")
    if not raw:
        return list(_DEFAULT_SOURCES)
    return [p for p in raw.replace(";", ":").split(":") if p]


service = RuntimeConfigService(_resolve_sources(), strict=True)
service.bootstrap()
app = create_app(service)
