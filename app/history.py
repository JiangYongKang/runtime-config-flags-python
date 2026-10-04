"""历史版本保留与查询。

每次成功发布（bootstrap / reload / rollback）产生的不可变快照都会在此保留，
保留条数有界（可配置，默认 :data:`DEFAULT_HISTORY_LIMIT`），底层使用
``deque(maxlen=limit)``：超出上限时淘汰最旧版本。淘汰只作用于历史索引，
绝不修改当前生效快照（当前版本即使被挤出历史，也依旧正常生效）。

回滚目标版本若已被淘汰出保留范围，抛出可区分的
``HISTORY_VERSION_NOT_FOUND``，当前生效状态保持不变。
"""

from __future__ import annotations

import threading
from collections import deque

from app.errors import ConfigError, ErrorCode
from app.models import Snapshot

DEFAULT_HISTORY_LIMIT = 10


class VersionHistory:
    """线程安全的有界历史版本索引。"""

    def __init__(self, *, limit: int = DEFAULT_HISTORY_LIMIT) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError(f"history limit 必须是正整数，实际为 {type(limit).__name__}")
        if limit < 1:
            raise ValueError(f"history limit 必须 >= 1，实际为 {limit}")
        self._limit = limit
        self._snapshots: deque[Snapshot] = deque(maxlen=limit)
        self._lock = threading.Lock()

    def add(self, snapshot: Snapshot) -> None:
        """发布成功后登记一个新版本；超出上限淘汰最旧版本。

        快照本身不可变，历史只持有引用；淘汰一个引用不会影响当前生效版本。
        """
        with self._lock:
            # 正常路径下版本严格递增；这里仍做防御性检查，避免重复登记。
            if self._snapshots and snapshot.version <= self._snapshots[-1].version:
                raise ValueError(
                    f"历史版本必须单调递增：最新保留版本={self._snapshots[-1].version}，"
                    f"登记版本={snapshot.version}"
                )
            self._snapshots.append(snapshot)

    def get(self, version: int) -> Snapshot:
        """取指定历史版本的完整快照；不在保留范围内抛 HISTORY_VERSION_NOT_FOUND。"""
        with self._lock:
            for snap in self._snapshots:
                if snap.version == version:
                    return snap
            kept = [s.version for s in self._snapshots]
        raise ConfigError(
            ErrorCode.HISTORY_VERSION_NOT_FOUND,
            (
                f"目标历史版本 {version} 已不在保留范围内"
                f"（当前保留版本: {kept or '无'}，上限 {self._limit}），无法回滚或查询"
            ),
            path=f"version.{version}",
            context={"target_version": version, "kept_versions": kept, "limit": self._limit},
        )

    def contains(self, version: int) -> bool:
        """版本是否仍在保留范围内。"""
        with self._lock:
            return any(s.version == version for s in self._snapshots)

    def list_versions(self) -> list[int]:
        """按版本号从旧到新返回保留范围内的全部版本号。"""
        with self._lock:
            return [s.version for s in self._snapshots]

    @property
    def limit(self) -> int:
        """历史保留条数上限。"""
        return self._limit
