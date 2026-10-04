"""错误类型与可区分的拒绝原因。

所有配置/开关相关的失败都通过 ``ConfigError`` 抛出，并携带一个稳定的
``ErrorCode``，使启动失败、更新回滚与开关判定的失败原因都可区分、可追溯。
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """稳定的错误码（字符串形式，便于日志与 API 输出）。"""

    # —— 配置加载 / 字段校验 ——
    SOURCE_NOT_FOUND = "source_not_found"
    SOURCE_NOT_FILE = "source_not_file"
    SOURCE_NOT_MAPPING = "source_not_mapping"
    SOURCE_PARSE_ERROR = "source_parse_error"
    MISSING_FIELD = "missing_field"
    TYPE_MISMATCH = "type_mismatch"
    VALUE_OUT_OF_RANGE = "value_out_of_range"
    SOURCE_CONFLICT = "source_conflict"
    DUPLICATE_FIELD_SPEC = "duplicate_field_spec"
    UNKNOWN_FIELD = "unknown_field"

    # —— 特性开关 ——
    UNKNOWN_FLAG = "unknown_flag"
    ILLEGAL_PERCENTAGE = "illegal_percentage"
    OVERRIDE_CONFLICT = "override_conflict"
    BAD_OVERRIDE_VALUE = "bad_override_value"

    # —— 运行时 / 热更新 ——
    NO_SNAPSHOT = "no_snapshot"
    UPDATE_REJECTED = "update_rejected"
    VERSION_CONFLICT = "version_conflict"

    # —— 历史版本 / 回滚 ——
    HISTORY_VERSION_NOT_FOUND = "history_version_not_found"


class ConfigError(Exception):
    """带错误码、来源路径与上下文的配置异常。

    属性：
        code:    稳定的错误码，见 :class:`ErrorCode`
        message: 人类可读的说明
        path:    出错字段在配置文档中的定位，如 ``config.timeout_ms``、
                 ``features.new_ui.percentage``
        source:  来源标识（文件路径或来源名）
        context: 任意附加上下文（收到的值、边界等）
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        path: str = "",
        source: str = "",
        context: dict[str, Any] | None = None,
    ) -> None:
        self._code = code
        self._message = message
        self.path = path
        self.source = source
        self.context: dict[str, Any] = dict(context or {})
        super().__init__(message)

    @property
    def code(self) -> ErrorCode:
        return self._code

    def __str__(self) -> str:
        loc = self.path or "<root>"
        src = f" source={self.source}" if self.source else ""
        return f"[{self._code.value}] {self._message} (path={loc}{src})"

    def to_dict(self) -> dict[str, Any]:
        """序列化为结构化字典（用于 HTTP 响应与日志）。"""
        return {
            "error_code": self._code.value,
            "message": self._message,
            "path": self.path,
            "source": self.source,
            "context": self.context,
        }
