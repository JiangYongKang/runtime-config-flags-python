"""可区分的配置拒绝原因与领域异常。

每一种无效输入都对应一个独立的 ReasonCode，调用方可以据此区分：
字段缺失、类型不符、取值越界、结构非法、来源冲突、灰度覆盖冲突等。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReasonCode(str, Enum):
    """配置/开关相关的拒绝原因码（取值即稳定字符串，便于日志与断言）。"""

    # ---- 通用结构 ----
    NOT_A_MAPPING = "NOT_A_MAPPING"                # 配置整体不是对象
    MISSING_FIELD = "MISSING_FIELD"                # 必填字段缺失
    WRONG_TYPE = "WRONG_TYPE"                      # 类型不符
    BAD_ENUM = "BAD_ENUM"                          # 枚举字段取值非法

    # ---- settings ----
    SETTINGS_MISSING = "SETTINGS_MISSING"          # 合并后 settings 整体缺失
    SETTING_OUT_OF_RANGE = "SETTING_OUT_OF_RANGE"  # 取值越界

    # ---- flags ----
    FLAGS_NOT_A_MAPPING = "FLAGS_NOT_A_MAPPING"
    FLAG_NOT_A_MAPPING = "FLAG_NOT_A_MAPPING"
    UNKNOWN_FLAG = "UNKNOWN_FLAG"                  # 评估时开关不存在
    PERCENTAGE_OUT_OF_RANGE = "PERCENTAGE_OUT_OF_RANGE"  # 灰度比例非法（非 0..100）
    OVERRIDE_SUBJECT_EMPTY = "OVERRIDE_SUBJECT_EMPTY"
    OVERRIDE_VALUE_INVALID = "OVERRIDE_VALUE_INVALID"
    CONFLICTING_OVERRIDE = "CONFLICTING_OVERRIDE"  # 同一主体在多个来源中冲突
    DUPLICATE_SUBJECT_OVERRIDE = "DUPLICATE_SUBJECT_OVERRIDE"  # 同一来源内重复主体

    # ---- 来源 ----
    SOURCE_ID_EMPTY = "SOURCE_ID_EMPTY"
    SOURCE_PRIORITY_INVALID = "SOURCE_PRIORITY_INVALID"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"            # 来源间同字段取值不一致
    EMPTY_UPDATE = "EMPTY_UPDATE"                  # 更新后无任何有效配置

    # ---- 运行时 ----
    NOT_INITIALIZED = "NOT_INITIALIZED"            # 启动未成功 / 尚未加载
    VERSION_MISMATCH = "VERSION_MISMATCH"          # 基于过期版本的条件更新


@dataclass(frozen=True, slots=True)
class ConfigIssue:
    """单条可追溯的问题描述。"""

    code: ReasonCode
    path: str
    message: str
    source: str | None = None

    def explain(self) -> str:
        prefix = f"[{self.source}] " if self.source else ""
        return f"{self.code.value}: {prefix}{self.path}: {self.message}"


class ConfigRejected(ValueError):
    """配置被拒绝（校验/合并失败）。携带全部问题，保证原因可区分、可追溯。"""

    def __init__(self, issues: list[ConfigIssue]):
        self.issues = issues
        super().__init__("; ".join(i.explain() for i in issues))


class NotInitializedError(RuntimeError):
    """配置中心尚未持有任何有效版本时读取/判定。"""

    def __init__(self) -> None:
        self.issue = ConfigIssue(
            code=ReasonCode.NOT_INITIALIZED,
            path="$",
            message="配置中心尚未加载任何有效配置，服务处于未初始化状态",
        )
        super().__init__(self.issue.explain())
