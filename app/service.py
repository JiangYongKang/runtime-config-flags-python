"""运行时配置服务门面：聚合加载、原子存储、开关判定、变更留痕与历史回滚。

启动语义：
  - strict=True（默认）：bootstrap 时若配置无效，直接抛出 ``ConfigError``，
    服务拒绝启动，失败原因可区分；
  - strict=False：配置无效时服务仍可启动，但保持“无快照”状态，此后任何读取
    都确定性地得到 ``NO_SNAPSHOT`` 错误，不会静默使用部分/默认配置。

热更新语义（reload）：
  1. 从当前来源路径完整重新加载，在临时数据上做全部校验并构建新快照；
  2. 构建（校验）成功后，基于当前版本号做 CAS 发布——任一请求看到的要么是
     完整旧版本，要么是完整新版本；
  3. 校验失败时根本不会触碰存储，当前生效状态（配置、开关、版本号、来源列表）
     全部保持不变，并留下一条带具体错误码的“被拒”留痕；
  4. CAS 失败（调用方显式给了过期的 expected_version）返回 VERSION_CONFLICT，
     当前状态同样不变；
  5. **不带 expected_version 的热更不会因并发失败**：所有写操作（reload /
     rollback）共用一把服务级写锁严格串行，锁内取最新版本号发布，版本严格 +1。

变更留痕与历史回滚：
  - 每次成功发布与每次被拒更新都会写入有界 AuditLog（错误码可区分）；
  - 每个成功版本的完整快照进入有界 VersionHistory，支持按版本查询与一键回滚；
  - 回滚会以目标历史版本的内容生成一个**全新、单调递增**的版本，绝不复用旧号；
  - 目标版本已被淘汰时返回 HISTORY_VERSION_NOT_FOUND，生效状态不变；
  - 留痕/历史的淘汰只作用于各自索引，绝不影响当前生效版本。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from app.audit import (
    DEFAULT_AUDIT_LIMIT,
    AuditEntry,
    AuditLog,
    ChangeKind,
)
from app.config import DEFAULT_FIELD_SPECS, build_snapshot
from app.errors import ConfigError, ErrorCode
from app.flags import FlagDecision, evaluate
from app.history import DEFAULT_HISTORY_LIMIT, VersionHistory
from app.models import FieldSpec, Snapshot
from app.store import SnapshotStore

logger = logging.getLogger("runtime_config")

_ROLLBACK_NOTE = "rollback：本版本由历史版本 {target} 一键回滚重新发布（内容与版本 {target} 一致）"


class RuntimeConfigService:
    """对外统一入口。"""

    def __init__(
        self,
        source_paths: list[str] | None = None,
        *,
        field_specs: tuple[FieldSpec, ...] = DEFAULT_FIELD_SPECS,
        strict: bool = True,
        store: SnapshotStore | None = None,
        audit_limit: int = DEFAULT_AUDIT_LIMIT,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> None:
        self._source_paths: list[str] = list(source_paths or [])
        self._field_specs = field_specs
        self._strict = strict
        self._store = store or SnapshotStore()
        # 所有变更（热更/回滚）共用的串行锁，保证两类写操作严格排队、
        # 绝不会各写各的互相覆盖；锁内再做存储层引用替换。
        self._write_lock = threading.RLock()
        self._audit = AuditLog(limit=audit_limit)
        self._history = VersionHistory(limit=history_limit)

    @property
    def source_paths(self) -> list[str]:
        return list(self._source_paths)

    @property
    def strict(self) -> bool:
        return self._strict

    @property
    def audit_limit(self) -> int:
        return self._audit.limit

    @property
    def history_limit(self) -> int:
        return self._history.limit

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def bootstrap(self) -> Snapshot | None:
        """首次加载并发布。

        非严格模式下配置无效时记录错误并返回 None（服务进入无快照状态）。
        成功/失败都会写入变更留痕。
        """
        logger.info(
            "bootstrap 开始: strict=%s sources=%s", self._strict, self._source_paths
        )
        try:
            snapshot = build_snapshot(
                self._source_paths,
                field_specs=self._field_specs,
                version=1,
            )
        except ConfigError as exc:
            self._record_rejected(
                ChangeKind.BOOTSTRAP,
                exc,
                sources=tuple(self._source_paths),
            )
            logger.error(
                "bootstrap 被拒绝，无生效配置: %s | detail=%s",
                exc,
                exc.to_dict(),
            )
            if self._strict:
                raise
            return None

        # 启动发布同样纳入写锁与历史/留痕，保持状态变更路径单一。
        with self._write_lock:
            self._store.publish(snapshot)
            self._history.add(snapshot)
            self._record_accepted(
                ChangeKind.BOOTSTRAP,
                snapshot,
                target_version=None,
                expected_version=None,
            )
        logger.info(
            "bootstrap 成功，生效版本=%d，输入配置=%s",
            snapshot.version,
            snapshot.to_public_dict(),
        )
        return snapshot

    def reload(
        self,
        source_paths: list[str] | None = None,
        *,
        expected_version: int | None = None,
    ) -> Snapshot:
        """重新加载并原子发布新版本。

        - source_paths 给定时替换本服务使用的来源集合（同样先校验后发布）；
          仅在发布成功后才真正替换来源，被拒不影响后续更新沿用原来源；
        - expected_version 为 None 时无条件发布：写锁内取最新基线，并发下
          串行重试，合法内容必定成功且版本严格 +1；
        - 显式给出过期 expected_version 时抛 VERSION_CONFLICT；
        - 任何校验失败或版本冲突都不会改变当前生效状态，并留下被拒留痕。
        """
        requested = list(source_paths) if source_paths is not None else list(self._source_paths)

        with self._write_lock:
            if expected_version is not None:
                snapshot = self._build_reload(
                    requested,
                    base_version=expected_version,
                    declared_expected=expected_version,
                )
                # 显式 CAS：基线过期直接抛 VERSION_CONFLICT（状态不变、留痕）。
                self._publish_or_record_reject(
                    snapshot,
                    kind=ChangeKind.RELOAD,
                    base_version=expected_version,
                    sources=tuple(requested),
                    expected_version=expected_version,
                )
            else:
                # 无条件发布：与其它写操作串行，锁内取最新基线，杜绝并发失败。
                # 理论上持写锁时无其它服务内写者，循环仅为与“外部共享 store”
                # 等极端情况保持健壮。
                while True:
                    current = self._store.try_current()
                    base_version = current.version if current is not None else 0
                    snapshot = self._build_reload(
                        requested,
                        base_version=base_version,
                        declared_expected=None,
                    )
                    try:
                        self._store.publish(snapshot, expected_version=base_version)
                    except ConfigError as exc:
                        if exc.code is ErrorCode.VERSION_CONFLICT:
                            logger.info("reload 发布时基线已变化，串行重试: %s", exc)
                            continue
                        self._record_rejected(
                            ChangeKind.RELOAD,
                            exc,
                            sources=tuple(requested),
                            expected_version=None,
                        )
                        raise
                    break

            # 发布成功后才提交来源列表，保证“被拒不影响后续用原来源发布”。
            self._source_paths = requested
            self._history.add(snapshot)
            self._record_accepted(
                ChangeKind.RELOAD,
                snapshot,
                target_version=None,
                expected_version=expected_version,
            )
        logger.info(
            "reload 成功，生效版本=%d，输入来源=%s，输入配置=%s",
            snapshot.version,
            requested,
            snapshot.to_public_dict(),
        )
        return snapshot

    def _build_reload(
        self,
        requested: list[str],
        *,
        base_version: int,
        declared_expected: int | None,
    ) -> Snapshot:
        """在临时数据上加载校验并构建新版本快照；失败留痕后原样抛出。"""
        new_version = base_version + 1
        logger.info(
            "reload 开始: 基于版本=%d -> 新版本=%d sources=%s",
            base_version,
            new_version,
            requested,
        )
        try:
            return build_snapshot(
                requested,
                field_specs=self._field_specs,
                version=new_version,
            )
        except ConfigError as exc:
            # 构建失败：存储与来源列表均未被触碰，仅记录被拒原因。
            self._record_rejected(
                ChangeKind.RELOAD,
                exc,
                sources=tuple(requested),
                expected_version=declared_expected,
            )
            raise

    def _publish_or_record_reject(
        self,
        snapshot: Snapshot,
        *,
        kind: ChangeKind,
        base_version: int,
        sources: tuple[str, ...],
        expected_version: int | None,
    ) -> None:
        """CAS 发布；冲突时留痕并原样抛出。"""
        try:
            self._store.publish(snapshot, expected_version=base_version)
        except ConfigError as exc:
            self._record_rejected(
                kind,
                exc,
                sources=sources,
                expected_version=expected_version,
            )
            raise

    # ------------------------------------------------------------------ #
    # 历史版本查询与回滚
    # ------------------------------------------------------------------ #
    def list_history_versions(self) -> list[int]:
        """返回仍在保留范围内的历史版本号（从旧到新）。"""
        return self._history.list_versions()

    def historical_snapshot(self, version: int) -> Snapshot:
        """取某个历史版本当时的完整快照（配置+开关自洽，不可变）。

        版本已不在保留范围内时抛 HISTORY_VERSION_NOT_FOUND。
        """
        return self._history.get(version)

    def rollback(
        self,
        target_version: int,
        *,
        expected_version: int | None = None,
    ) -> Snapshot:
        """把历史版本的内容重新发布为当前版本。

        - 生成的是**全新版本号**（当前版本 +1，单调递增、不跳号、不复用旧号），
          内容与目标历史版本的配置/开关完全一致；
        - target_version 已被淘汰 → HISTORY_VERSION_NOT_FOUND，状态不变并留痕；
        - 显式 expected_version 过期 → VERSION_CONFLICT，状态不变并留痕；
        - 与 reload 共用写锁，严格串行。
        """
        with self._write_lock:
            try:
                target = self._history.get(target_version)
            except ConfigError as exc:
                self._record_rejected(
                    ChangeKind.ROLLBACK,
                    exc,
                    sources=tuple(self._source_paths),
                    target_version=target_version,
                    expected_version=expected_version,
                )
                raise

            current = self._store.try_current()
            base_version = current.version if current is not None else 0
            if expected_version is not None and expected_version != base_version:
                conflict = ConfigError(
                    ErrorCode.VERSION_CONFLICT,
                    (
                        f"版本冲突：期望基于版本 {expected_version} 提交回滚，"
                        f"当前生效版本为 {base_version}"
                    ),
                    context={
                        "expected_version": expected_version,
                        "current_version": base_version,
                    },
                )
                self._record_rejected(
                    ChangeKind.ROLLBACK,
                    conflict,
                    sources=tuple(self._source_paths),
                    target_version=target_version,
                    expected_version=expected_version,
                )
                raise conflict

            new_version = base_version + 1
            logger.info(
                "rollback 开始: 目标历史版本=%d 当前版本=%d -> 新版本=%d",
                target_version,
                base_version,
                new_version,
            )

            # 以目标版本内容构造全新快照：values/flags 逐字复制（深拷贝由
            # Snapshot 冻结保证），追加一条回滚来源说明，版本号全新。
            rebuilt = Snapshot(
                version=new_version,
                values=dict(target.values),
                flags=dict(target.flags),
                sources=target.sources,
                reasons=tuple(target.reasons)
                + (_ROLLBACK_NOTE.format(target=target_version),),
            )
            self._store.publish(rebuilt, expected_version=base_version)
            # 回滚后，后续热更沿用目标版本当时的来源列表。
            self._source_paths = list(target.sources)
            self._history.add(rebuilt)
            self._record_accepted(
                ChangeKind.ROLLBACK,
                rebuilt,
                target_version=target_version,
                expected_version=expected_version,
            )
        logger.info(
            "rollback 成功：历史版本=%d 的内容已作为新版本=%d 生效，输入来源=%s",
            target_version,
            rebuilt.version,
            list(target.sources),
        )
        return rebuilt

    # ------------------------------------------------------------------ #
    # 变更留痕查询
    # ------------------------------------------------------------------ #
    def list_changes(self) -> list[AuditEntry]:
        """返回保留范围内的全部变更留痕（从旧到新）。"""
        return self._audit.list_entries()

    def _record_accepted(
        self,
        kind: ChangeKind,
        snapshot: Snapshot,
        *,
        target_version: int | None,
        expected_version: int | None,
    ) -> None:
        entry = self._audit.record_accepted(
            kind=kind,
            resulting_version=snapshot.version,
            sources=snapshot.sources,
            target_version=target_version,
            expected_version=expected_version,
            message=f"{kind.value} 成功，版本 {snapshot.version} 生效",
        )
        logger.info("[变更留痕-成功] %s", entry.to_dict())

    def _record_rejected(
        self,
        kind: ChangeKind,
        exc: ConfigError,
        *,
        sources: tuple[str, ...],
        target_version: int | None = None,
        expected_version: int | None = None,
    ) -> None:
        detail: dict[str, Any] = {
            "path": exc.path,
            "source": exc.source,
        }
        detail.update(exc.context)
        entry = self._audit.record_rejected(
            kind=kind,
            sources=sources,
            error_code=exc.code.value,
            message=str(exc),
            target_version=target_version,
            expected_version=expected_version,
            detail=detail,
        )
        logger.info("[变更留痕-拒绝] %s", entry.to_dict())

    # ------------------------------------------------------------------ #
    # 配置读取
    # ------------------------------------------------------------------ #
    def snapshot(self) -> Snapshot:
        """获取当前生效快照（同一次调用内配置天然同版本）。"""
        return self._store.current()

    def version(self) -> int:
        """当前生效版本号。"""
        return self._store.current().version

    def try_current_version(self) -> int | None:
        """当前生效版本号；尚无快照时返回 None（不抛异常）。"""
        snap = self._store.try_current()
        return snap.version if snap is not None else None

    def get(self, key: str) -> Any:
        """读取单个配置值；未登记字段给出 UNKNOWN_FIELD。"""
        snap = self._store.current()
        if key not in snap.values:
            raise ConfigError(
                ErrorCode.UNKNOWN_FIELD,
                f"未知配置字段: {key!r}（当前版本 {snap.version}）",
                path=f"config.{key}",
                context={"known": sorted(snap.values)},
            )
        return snap.values[key]

    def all_config(self) -> dict[str, Any]:
        """读取完整配置（返回拷贝，含版本号，保证来自同一快照）。"""
        snap = self._store.current()
        return {"version": snap.version, "config": dict(snap.values)}

    # ------------------------------------------------------------------ #
    # 开关判定
    # ------------------------------------------------------------------ #
    def evaluate_flag(self, flag: str, subject: str) -> FlagDecision:
        """返回带完整判定依据的结论（基于调用时刻的当前快照）。

        版本号、配置内容与开关判定同取自这一个不可变快照，单次请求内必然自洽。
        """
        return self.evaluate_on(self._store.current(), flag, subject)

    def evaluate_on(self, snapshot: Snapshot, flag: str, subject: str) -> FlagDecision:
        """针对指定快照做判定。

        适用于需要“先取一次完整快照，再在同一版本上做多次判定/读取”的场景，
        保证一批读取全部落在同一版本。
        """
        rule = snapshot.flags.get(flag)
        if rule is None:
            raise ConfigError(
                ErrorCode.UNKNOWN_FLAG,
                f"未知特性开关: {flag!r}（当前版本 {snapshot.version}）",
                path=f"features.{flag}",
                context={"known_flags": sorted(snapshot.flags)},
            )
        decision = evaluate(rule, subject, version=snapshot.version)
        logger.info(
            "开关判定: flag=%s subject=%s 生效版本=%d 结论=%s 依据=%s",
            flag,
            subject,
            snapshot.version,
            decision.on,
            decision.to_dict(),
        )
        return decision

    def flag_on(self, flag: str, subject: str) -> bool:
        """仅返回开关布尔结论。"""
        return self.evaluate_flag(flag, subject).on
