"""pytest 公共夹具与日志。

每个测试都会打印：
  - 输入配置（各 JSON 来源内容）
  - 生效版本
  - 开关判定依据 / 失败错误码
便于本地稳定复现与排障。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.service import RuntimeConfigService

logger = logging.getLogger("runtime_config.tests")


@pytest.fixture
def write_source(tmp_path: Path):
    """返回一个把 dict 写成 JSON 来源文件的工具。"""

    created: list[Path] = []

    def _write(name: str, payload: dict) -> str:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        created.append(path)
        logger.info("[测试输入配置] %s <- %s", name, json.dumps(payload, ensure_ascii=False))
        return str(path)

    return _write


@pytest.fixture
def make_service(write_source):
    """构造服务并在 bootstrap 后输出生效版本。"""

    def _make(payloads: list[dict], *, strict: bool = True) -> RuntimeConfigService:
        paths = [
            write_source(f"source_{idx}.json", payload)
            for idx, payload in enumerate(payloads)
        ]
        svc = RuntimeConfigService(paths, strict=strict)
        snap = svc.bootstrap()
        if snap is not None:
            logger.info("[测试生效版本] version=%d", snap.version)
            logger.info("[测试合并依据] %s", list(snap.reasons))
        return svc

    return _make
