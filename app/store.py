"""原子快照存储：配置版本的线程安全发布与读取。

使用不可变 ``Snapshot`` + 单一引用替换实现原子可见性：任一请求拿到的
要么是完整的旧版本，要么是完整的新版本，绝不会读到半个配置。

发布支持乐观并发控制（expected_version）：多个热更新并发提交时，只有基于
当前版本提交的那一个会成功，其余得到 VERSION_CONFLICT，由调用方决定重试。
"""

from __future__ import annotations

import threading

from app.errors import ConfigError, ErrorCode
from app.models import Snapshot


class SnapshotStore:
    """持有当前生效版本，支持条件（CAS）更新。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current: Snapshot | None = None

    def publish(self, snapshot: Snapshot, *, expected_version: int | None = None) -> None:
        """发布新版本；可通过 expected_version 做乐观并发控制。

        - expected_version 为 None：无条件发布。
        - 否则仅当当前版本号等于 expected_version 时发布；
          尚未初始化时只允许 expected_version == 0。
        """
        with self._lock:
            current = self._current
            if expected_version is not None:
                current_version = current.version if current is not None else 0
                if current_version != expected_version:
                    raise ConfigError(
                        ErrorCode.VERSION_CONFLICT,
                        (
                            f"版本冲突：期望基于版本 {expected_version} 提交，"
                            f"当前生效版本为 {current_version}"
                        ),
                        context={
                            "expected_version": expected_version,
                            "current_version": current_version,
                        },
                    )
            self._current = snapshot

    def current(self) -> Snapshot:
        """返回当前快照；尚未初始化时抛出 NO_SNAPSHOT。"""
        with self._lock:
            if self._current is None:
                raise ConfigError(
                    ErrorCode.NO_SNAPSHOT,
                    "配置尚未成功加载，当前没有生效版本",
                )
            return self._current

    def try_current(self) -> Snapshot | None:
        """返回当前快照；未初始化时返回 None（不抛异常）。"""
        with self._lock:
            return self._current
