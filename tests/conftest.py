"""测试公共夹具与判定日志工具。

每个测试通过 log_case() 打印：输入配置、生效版本、判定依据/失败原因，
满足“单测日志需打印输入配置、生效版本与判定依据，失败原因可区分”。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from runtime.merger import merge_sources
from runtime.parser import parse_source
from runtime.store import ConfigStore

log = logging.getLogger("rcf.test")

SETTINGS_FULL = {
    "max_connections": 100,
    "request_timeout_ms": 3000,
    "sample_rate": 0.1,
    "log_level": "INFO",
}


def log_case(title: str, *, raws: Any = None, version: Any = None,
             result: Any = None, error: Any = None) -> None:
    """统一格式的单测用例日志。"""
    lines = [f"==== {title} ===="]
    if raws is not None:
        lines.append("输入配置:\n" + json.dumps(raws, ensure_ascii=False, indent=2))
    if version is not None:
        lines.append(f"生效版本: v{version}")
    if result is not None:
        lines.append("判定依据: " + json.dumps(result, ensure_ascii=False))
    if error is not None:
        lines.append(f"拒绝/失败原因: {error}")
    log.info("\n".join(lines))


def make_raw(flag: dict[str, Any] | None = None, *, source_id: str = "src",
             priority: int = 10, **settings: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {"source_id": source_id, "priority": priority}
    raw["settings"] = {**SETTINGS_FULL, **settings}
    if flag is not None:
        raw["flags"] = {"feature_x": flag}
    return raw


def build_config(raws: list[dict[str, Any]], *, version: int = 1):
    return merge_sources([parse_source(r) for r in raws], version=version)


def flag_rule(*, enabled: bool = True, percentage: int = 0,
              overrides: dict[str, bool] | None = None,
              source_id: str = "src"):
    return {"enabled": enabled, "percentage": percentage,
            "overrides": overrides or {}}


@pytest.fixture
def full_settings() -> dict[str, Any]:
    return dict(SETTINGS_FULL)


@pytest.fixture
def store_with() -> Any:
    """返回 (store, load) ；load(raws) 发布一份配置。"""
    store = ConfigStore()

    def load(raws: list[dict[str, Any]]):
        return store.load_sources(raws)

    return store, load
