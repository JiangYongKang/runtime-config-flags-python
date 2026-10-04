"""变更留痕与历史版本保留（骨架）。

- ChangeRecord：一次变更（发布/回滚/被拒）的留痕；
- HistoryKeeper：线程安全的有界存储，保留最近 N 个历史版本快照与
  最近 M 条留痕，超出后淘汰最旧；淘汰不影响当前生效版本。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.models import Snapshot

#: 历史版本保留条数默认值（可配置）
DEFAULT_HISTORY_LIMIT = 50
#: 变更留痕条数上限默认值（可配置）
DEFAULT_AUDIT_LIMIT = 200


@dataclass(frozen=True)
class ChangeRecord:
    """一条变更留痕（不可变）。"""

    seq: int
    kind: str  # "bootstrap" | "reload" | "rollback"
    outcome: str  # "published" | "rejected"
    version: int | None  # 发布成功时的新版本号；被拒时为 None
    reason: str  # 人类可读说明（被拒时为具体拒绝原因）
    error_code: str | None = None  # 被拒时的稳定错误码
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可展示结构。"""
        return {
            "seq": self.seq,
            "kind": self.kind,
            "outcome": self.outcome,
            "version": self.version,
            "reason": self.reason,
            "error_code": self.error_code,
            "detail": dict(self.detail),
        }


class HistoryKeeper:
    """有界的历史版本 + 留痕存储（线程安全）。"""

    def __init__(
        self,
        *,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        audit_limit: int = DEFAULT_AUDIT_LIMIT,
    ) -> None:
        self._history_limit = history_limit
        self._audit_limit = audit_limit
        self._lock = threading.RLock()
        self._versions: deque[Snapshot] = deque()
        self._records: deque[ChangeRecord] = deque()
        self._seq = 0

    @property
    def history_limit(self) -> int:
        return self._history_limit

    @property
    def audit_limit(self) -> int:
        return self._audit_limit

    # ------------------------------------------------------------------ #
    # 历史版本
    # ------------------------------------------------------------------ #
    def retain(self, snapshot: Snapshot) -> None:
        """保留一个已发布版本；超出上限时淘汰最旧。

        淘汰只影响“还能查到/回滚到哪些历史版本”，不影响当前生效版本
        （生效快照由 SnapshotStore 独立持有）。
        """
        with self._lock:
            self._versions.append(snapshot)
            while len(self._versions) > self._history_limit:
                self._versions.popleft()

    def get(self, version: int) -> Snapshot | None:
        """查询保留范围内的历史版本；不在范围内返回 None。"""
        with self._lock:
            for snap in self._versions:
                if snap.version == version:
                    return snap
            return None

    def retained_versions(self) -> list[int]:
        """当前保留的版本号列表（升序）。"""
        with self._lock:
            return [snap.version for snap in self._versions]

    # ------------------------------------------------------------------ #
    # 变更留痕
    # ------------------------------------------------------------------ #
    def record(
        self,
        *,
        kind: str,
        outcome: str,
        version: int | None,
        reason: str,
        error_code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> ChangeRecord:
        """追加一条留痕；超出上限时淘汰最旧。"""
        with self._lock:
            self._seq += 1
            rec = ChangeRecord(
                seq=self._seq,
                kind=kind,
                outcome=outcome,
                version=version,
                reason=reason,
                error_code=error_code,
                detail=dict(detail or {}),
            )
            self._records.append(rec)
            while len(self._records) > self._audit_limit:
                self._records.popleft()
            return rec

    def records(self) -> list[ChangeRecord]:
        """全部留痕（按发生顺序）。"""
        with self._lock:
            return list(self._records)
