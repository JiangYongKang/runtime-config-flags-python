"""核心数据模型（不可变值对象）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from app.errors import ConfigError, ErrorCode

_TYPE_NAMES = {
    "int": "整数(int)",
    "float": "数字(float)",
    "bool": "布尔(bool)",
    "str": "字符串(str)",
}


class FieldType(str, Enum):
    """受支持的配置字段类型。"""

    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    STR = "str"


@dataclass(frozen=True)
class FieldSpec:
    """配置字段规格：名称、类型、取值边界与默认值。

    min_value/max_value 仅对 int/float 生效（闭区间）。
    """

    name: str
    type: FieldType
    default: Any
    min_value: float | None = None
    max_value: float | None = None

    def validate_value(self, value: Any, *, path: str, source: str) -> Any:
        """校验单个取值并返回规范化后的值；非法时抛出带错误码的 ConfigError。

        注意：bool 不被视为 int（Python 中 ``isinstance(True, int)`` 为真，
        这里显式拒绝，避免类型静默混用）。
        """
        if self.type is FieldType.INT:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(
                    ErrorCode.TYPE_MISMATCH,
                    f"字段 {self.name} 期望整数(int)，实际收到 {type(value).__name__}: {value!r}",
                    path=path,
                    source=source,
                    context={"expected": "int", "got_type": type(value).__name__, "got": value},
                )
            normalized: Any = value
        elif self.type is FieldType.FLOAT:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(
                    ErrorCode.TYPE_MISMATCH,
                    f"字段 {self.name} 期望数字(float)，实际收到 {type(value).__name__}: {value!r}",
                    path=path,
                    source=source,
                    context={"expected": "float", "got_type": type(value).__name__, "got": value},
                )
            normalized = float(value)
        elif self.type is FieldType.BOOL:
            if not isinstance(value, bool):
                raise ConfigError(
                    ErrorCode.TYPE_MISMATCH,
                    f"字段 {self.name} 期望布尔(bool)，实际收到 {type(value).__name__}: {value!r}",
                    path=path,
                    source=source,
                    context={"expected": "bool", "got_type": type(value).__name__, "got": value},
                )
            normalized = value
        else:  # STR
            if not isinstance(value, str):
                raise ConfigError(
                    ErrorCode.TYPE_MISMATCH,
                    f"字段 {self.name} 期望字符串(str)，实际收到 {type(value).__name__}: {value!r}",
                    path=path,
                    source=source,
                    context={"expected": "str", "got_type": type(value).__name__, "got": value},
                )
            normalized = value

        if self.type in (FieldType.INT, FieldType.FLOAT):
            if self.min_value is not None and normalized < self.min_value:
                raise ConfigError(
                    ErrorCode.VALUE_OUT_OF_RANGE,
                    f"字段 {self.name} 取值 {normalized} 小于下界 {self.min_value}",
                    path=path,
                    source=source,
                    context={"value": normalized, "min": self.min_value, "max": self.max_value},
                )
            if self.max_value is not None and normalized > self.max_value:
                raise ConfigError(
                    ErrorCode.VALUE_OUT_OF_RANGE,
                    f"字段 {self.name} 取值 {normalized} 大于上界 {self.max_value}",
                    path=path,
                    source=source,
                    context={"value": normalized, "min": self.min_value, "max": self.max_value},
                )
        return normalized


@dataclass(frozen=True)
class FlagRule:
    """单个特性开关的规则：默认开关、灰度百分比与定向覆盖。"""

    name: str
    default_on: bool
    percentage: int
    overrides: dict[str, bool] = field(default_factory=dict)
    salt: str = ""


@dataclass(frozen=True)
class Snapshot:
    """某一版本的完整配置 + 开关集合（不可变，原子发布单位）。"""

    version: int
    values: dict[str, Any]
    flags: dict[str, FlagRule]
    sources: tuple[str, ...]
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # frozen dataclass 只冻结属性绑定；这里进一步冻结内部映射，
        # 使发布出去的快照无法被任何调用方就地修改。
        object.__setattr__(self, "values", MappingProxyType(self.values))
        object.__setattr__(self, "flags", MappingProxyType(self.flags))

    def to_public_dict(self) -> dict[str, Any]:
        """输出可展示结构（深拷贝，避免外部修改内部状态）。"""
        return {
            "version": self.version,
            "sources": list(self.sources),
            "config": dict(self.values),
            "flags": {
                name: {
                    "default_on": rule.default_on,
                    "percentage": rule.percentage,
                    "overrides": dict(rule.overrides),
                    "salt": rule.salt,
                }
                for name, rule in self.flags.items()
            },
            "reasons": list(self.reasons),
        }
