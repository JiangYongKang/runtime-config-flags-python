"""配置领域的不可变数据结构。

层级：
  SourceConfig  —— 单个来源解析/校验后的结果
  MergedConfig  —— 多来源合并后、整体校验通过、按版本冻结的生效快照
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# settings 字段的合法取值域：key -> (类型, 最小值, 最大值)
SETTING_SPEC: dict[str, tuple[type, float, float]] = {
    "max_connections": (int, 1, 100_000),
    "request_timeout_ms": (int, 1, 3_600_000),
    "sample_rate": (float, 0.0, 1.0),
}

# settings 的枚举型字段及合法值
SETTING_ENUMS: dict[str, tuple[str, ...]] = {
    "log_level": ("DEBUG", "INFO", "WARN", "ERROR"),
}

# 合并后必须齐备的 settings 字段
REQUIRED_SETTINGS: tuple[str, ...] = (
    "max_connections",
    "request_timeout_ms",
    "sample_rate",
    "log_level",
)


@dataclass(frozen=True, slots=True)
class FlagRule:
    """单个特性开关的规则（合并后冻结形态）。

    enabled        基线开关（无定向覆盖的主体以此为准）
    percentage     灰度比例，0..100 闭区间；0 表示全关，100 表示除显式 off 外全开
    overrides      主体 -> 显式布尔，优先级最高，跨来源已确认无冲突
    override_provenance 主体 -> 来源 id（解释判定依据用）
    """

    enabled: bool
    percentage: int
    overrides: frozenset[tuple[str, bool]]
    override_provenance: frozenset[tuple[str, str]]

    def override_map(self) -> dict[str, bool]:
        return dict(self.overrides)

    def provenance_map(self) -> dict[str, str]:
        return dict(self.override_provenance)


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """单个来源校验通过后的配置片段。"""

    source_id: str
    priority: int
    settings: frozenset[tuple[str, Any]]
    flags: frozenset[tuple[str, FlagRule]]


@dataclass(frozen=True, slots=True)
class MergedConfig:
    """一次生效的完整、不可变配置快照（一个版本）。"""

    version: int
    settings: dict[str, Any]
    flags: dict[str, FlagRule]
    # 每个字段的来源依据：settings key -> [source_id...]（按优先级高到低）
    settings_provenance: dict[str, tuple[str, ...]]
    flags_provenance: dict[str, tuple[str, ...]]
    sources: tuple[str, ...]
    created_at: float = field(default_factory=time.time)

    def snapshot_source_ids(self) -> tuple[str, ...]:
        return self.sources


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """特性开关判定结果及其可解释依据。"""

    flag: str
    subject: str
    on: bool
    version: int
    reason: str
    bucket: int
    percentage: int
    detail: str
