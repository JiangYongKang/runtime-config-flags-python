"""变更留痕：记录每次成功发布与每次被拒更新（原因可区分）。

留痕为有界保留（条数上限可配置，默认 :data:`DEFAULT_AUDIT_LIMIT`），
底层使用 ``deque(maxlen=limit)``：超出上限时最旧记录被自动淘汰，淘汰只影响
留痕本身，绝不触碰当前生效版本与历史版本索引。

留痕序号（seq）单调递增、不因淘汰而复用或回绕，便于观察“共发生过多少次
变更”以及哪些记录已被淘汰。所有方法线程安全。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

DEFAULT_AUDIT_LIMIT = 100


class ChangeKind(str, Enum):
    """一次变更的类别。"""

    BOOTSTRAP = "bootstrap"
    RELOAD = "reload"
    ROLLBACK = "rollback"


class ChangeOutcome(str, Enum):
    """一次变更的结果。"""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class AuditEntry:
    """单条变更留痕（不可变）。

    属性：
        seq:              留痕自身的单调递增序号（从 1 开始，淘汰不复用）
        outcome:          accepted / rejected
        kind:             bootstrap / reload / rollback
        resulting_version: 成功后生效的新版本号；被拒时为 None
        target_version:   rollback 请求的目标历史版本；其余操作为 None
        expected_version: 调用方声明的期望基线版本（可空）
        sources:          本次使用的来源列表（回滚时为目标版本的来源）
        error_code:       被拒时的稳定错误码；成功时为空串
        message:          被拒原因或成功说明
        detail:           附加上下文（错误定位、来源等）
    """

    seq: int
    outcome: ChangeOutcome
    kind: ChangeKind
    resulting_version: int | None
    target_version: int | None
    expected_version: int | None
    sources: tuple[str, ...]
    error_code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可展示字典（深拷贝集合，避免外部改动）。"""
        return {
            "seq": self.seq,
            "outcome": self.outcome.value,
            "kind": self.kind.value,
            "resulting_version": self.resulting_version,
            "target_version": self.target_version,
            "expected_version": self.expected_version,
            "sources": list(self.sources),
            "error_code": self.error_code,
            "message": self.message,
            "detail": dict(self.detail),
        }


class AuditLog:
    """线程安全的有界变更留痕。"""

    def __init__(self, *, limit: int = DEFAULT_AUDIT_LIMIT) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError(f"audit limit 必须是正整数，实际为 {type(limit).__name__}")
        if limit < 1:
            raise ValueError(f"audit limit 必须 >= 1，实际为 {limit}")
        self._limit = limit
        # maxlen 保证超出上限时最旧记录被淘汰，且淘汰无需触碰其它任何状态。
        self._entries: deque[AuditEntry] = deque(maxlen=limit)
        self._seq = 0
        self._lock = threading.Lock()

    def _append(self, entry: AuditEntry) -> AuditEntry:
        with self._lock:
            self._entries.append(entry)
        return entry

    def record_accepted(
        self,
        *,
        kind: ChangeKind,
        resulting_version: int,
        sources: tuple[str, ...],
        target_version: int | None = None,
        expected_version: int | None = None,
        message: str = "",
        detail: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """记录一次成功发布。"""
        with self._lock:
            self._seq += 1
            seq = self._seq
        return self._append(
            AuditEntry(
                seq=seq,
                outcome=ChangeOutcome.ACCEPTED,
                kind=kind,
                resulting_version=resulting_version,
                target_version=target_version,
                expected_version=expected_version,
                sources=tuple(sources),
                error_code="",
                message=message,
                detail=dict(detail or {}),
            )
        )

    def record_rejected(
        self,
        *,
        kind: ChangeKind,
        sources: tuple[str, ...],
        error_code: str,
        message: str,
        target_version: int | None = None,
        expected_version: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """记录一次被拒更新（携带可区分错误码）。"""
        with self._lock:
            self._seq += 1
            seq = self._seq
        return self._append(
            AuditEntry(
                seq=seq,
                outcome=ChangeOutcome.REJECTED,
                kind=kind,
                resulting_version=None,
                target_version=target_version,
                expected_version=expected_version,
                sources=tuple(sources),
                error_code=error_code,
                message=message,
                detail=dict(detail or {}),
            )
        )

    def list_entries(self) -> list[AuditEntry]:
        """按时间从旧到新返回当前保留范围内的全部留痕（拷贝）。"""
        with self._lock:
            return list(self._entries)

    @property
    def limit(self) -> int:
        """留痕条数上限。"""
        return self._limit

    @property
    def next_seq(self) -> int:
        """下一条留痕的序号（已记录总条数，含已被淘汰的）。"""
        with self._lock:
            return self._seq + 1
