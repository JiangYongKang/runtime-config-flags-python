"""运行时配置服务门面：聚合加载、原子存储与开关判定。

启动语义：
  - strict=True（默认）：bootstrap 时若配置无效，直接抛出 ``ConfigError``，
    服务拒绝启动，失败原因可区分；
  - strict=False：配置无效时服务仍可启动，但保持“无快照”状态，此后任何读取
    都确定性地得到 ``NO_SNAPSHOT`` 错误，不会静默使用部分/默认配置。

热更新语义（reload）：
  1. 从当前来源路径完整重新加载，在临时数据上做全部校验并构建新快照；
  2. 构建（校验）成功后，基于当前版本号做 CAS 发布——任一请求看到的要么是
     完整旧版本，要么是完整新版本；
  3. 校验失败时根本不会触碰存储，当前生效状态保持不变（天然回滚），
     错误码/路径/来源随异常返回并记录日志；
  4. CAS 失败（并发 reload）返回 VERSION_CONFLICT，当前状态同样不变。

变更留痕与历史回滚：
  - 每次热更/回滚无论成功还是被拒，都会追加一条 ChangeRecord 留痕
    （被拒时携带具体错误码，而非笼统失败）；留痕条数有上限（可配置），
    超出后淘汰最旧，淘汰不影响当前生效版本；
  - 每个成功发布的版本都会保留进历史（有界，可配置），可查询历史版本的
    完整配置与开关，也可一键回滚：把某个保留范围内的历史版本重新发布为
    当前版本，版本号取当前版本 +1（全新、单调递增，不复用旧号不跳号）；
  - reload 与 rollback 在同一把发布锁下严格串行，互不覆盖；不带
    expected_version 的 reload 在锁内取号，只要内容合法必然发布成功。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from app.config import DEFAULT_FIELD_SPECS, build_snapshot
from app.errors import ConfigError, ErrorCode
from app.flags import FlagDecision, evaluate
from app.history import (
    DEFAULT_AUDIT_LIMIT,
    DEFAULT_HISTORY_LIMIT,
    ChangeRecord,
    HistoryKeeper,
)
from app.models import FieldSpec, Snapshot
from app.store import SnapshotStore

logger = logging.getLogger("runtime_config")


class RuntimeConfigService:
    """对外统一入口。"""

    def __init__(
        self,
        source_paths: list[str] | None = None,
        *,
        field_specs: tuple[FieldSpec, ...] = DEFAULT_FIELD_SPECS,
        strict: bool = True,
        store: SnapshotStore | None = None,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        audit_limit: int = DEFAULT_AUDIT_LIMIT,
    ) -> None:
        self._source_paths: list[str] = list(source_paths or [])
        self._field_specs = field_specs
        self._strict = strict
        self._store = store or SnapshotStore()
        self._history = HistoryKeeper(
            history_limit=history_limit, audit_limit=audit_limit
        )
        # 发布锁：reload / rollback / bootstrap 严格串行，版本号在锁内分配，
        # 保证单调递增、不跳号，且不带 expected_version 的合法热更必然成功。
        self._publish_lock = threading.Lock()

    @property
    def source_paths(self) -> list[str]:
        return list(self._source_paths)

    @property
    def strict(self) -> bool:
        return self._strict

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def bootstrap(self) -> Snapshot | None:
        """首次加载并发布。

        非严格模式下配置无效时记录错误并返回 None（服务进入无快照状态）。
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
            logger.error(
                "bootstrap 被拒绝，无生效配置: %s | detail=%s",
                exc,
                exc.to_dict(),
            )
            if self._strict:
                raise
            return None

        with self._publish_lock:
            self._store.publish(snapshot)
            self._history.retain(snapshot)
            self._history.record(
                kind="bootstrap",
                outcome="published",
                version=snapshot.version,
                reason="首次加载发布成功",
                detail={"sources": list(snapshot.sources)},
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

        - source_paths 给定时替换本服务使用的来源集合（仅当本次发布成功才生效，
          被拒绝的更新不会污染后续更新使用的来源）；
        - expected_version 为 None 时在发布锁内基于当前版本取号，只要内容合法
          必然发布成功并拿到递增的新版本；给定时做 CAS，冲突返回 VERSION_CONFLICT；
        - 任何校验失败或版本冲突都不会改变当前生效状态，且都会留下带具体
          错误码的留痕。
        """
        with self._publish_lock:
            candidate_paths = (
                list(source_paths) if source_paths is not None else list(self._source_paths)
            )

            current = self._store.try_current()
            base_version = current.version if current is not None else 0
            if expected_version is not None and expected_version != base_version:
                exc = ConfigError(
                    ErrorCode.VERSION_CONFLICT,
                    (
                        f"版本冲突：期望基于版本 {expected_version} 提交，"
                        f"当前生效版本为 {base_version}"
                    ),
                    context={
                        "expected_version": expected_version,
                        "current_version": base_version,
                    },
                )
                self._record_rejected("reload", exc, sources=candidate_paths)
                raise exc

            new_version = base_version + 1
            logger.info(
                "reload 开始: 基于版本=%d -> 新版本=%d sources=%s",
                base_version,
                new_version,
                candidate_paths,
            )

            # 先在临时数据上完成全部加载与校验；失败时存储完全不被触碰。
            try:
                snapshot = build_snapshot(
                    candidate_paths,
                    field_specs=self._field_specs,
                    version=new_version,
                )
            except ConfigError as exc:
                self._record_rejected("reload", exc, sources=candidate_paths)
                raise

            # 校验通过：发布、替换来源、保留历史、留痕（均在同一锁内完成）。
            self._store.publish(snapshot)
            self._source_paths = candidate_paths
            self._history.retain(snapshot)
            self._history.record(
                kind="reload",
                outcome="published",
                version=snapshot.version,
                reason="热更新发布成功",
                detail={"sources": list(snapshot.sources)},
            )
        logger.info(
            "reload 成功，生效版本=%d，输入配置=%s",
            snapshot.version,
            snapshot.to_public_dict(),
        )
        return snapshot

    # ------------------------------------------------------------------ #
    # 历史版本与回滚
    # ------------------------------------------------------------------ #
    def history_versions(self) -> list[int]:
        """当前保留范围内的历史版本号（升序）。"""
        return self._history.retained_versions()

    def history_snapshot(self, version: int) -> Snapshot:
        """查询保留范围内某个历史版本的完整快照（配置+开关，自洽同版本）。

        版本不在保留范围时抛出 VERSION_NOT_RETAINED（可区分的拒绝理由）。
        """
        snap = self._history.get(version)
        if snap is None:
            raise ConfigError(
                ErrorCode.VERSION_NOT_RETAINED,
                (
                    f"历史版本 {version} 不在保留范围内"
                    f"（当前保留: {self._history.retained_versions()}）"
                ),
                context={
                    "requested_version": version,
                    "retained_versions": self._history.retained_versions(),
                },
            )
        return snap

    def rollback(self, target_version: int) -> Snapshot:
        """把保留范围内的历史版本重新发布为当前生效版本（一键回滚）。

        - 产生一个全新的、继续单调递增的版本号（当前版本 +1），不复用旧号、
          不跳号；
        - 与 reload 共用同一把发布锁，严格串行，不会互相覆盖；
        - 目标版本不在保留范围时以 VERSION_NOT_RETAINED 拒绝并留痕，
          当前生效状态保持不变。
        """
        with self._publish_lock:
            current = self._store.current()  # 无快照时抛 NO_SNAPSHOT
            target = self._history.get(target_version)
            if target is None:
                exc = ConfigError(
                    ErrorCode.VERSION_NOT_RETAINED,
                    (
                        f"回滚目标版本 {target_version} 不在保留范围内"
                        f"（当前保留: {self._history.retained_versions()}）"
                    ),
                    context={
                        "requested_version": target_version,
                        "retained_versions": self._history.retained_versions(),
                        "current_version": current.version,
                    },
                )
                self._record_rejected("rollback", exc)
                raise exc

            new_version = current.version + 1
            logger.info(
                "rollback 开始: 当前版本=%d -> 回滚到历史版本=%d，新版本=%d",
                current.version,
                target_version,
                new_version,
            )
            snapshot = Snapshot(
                version=new_version,
                values=dict(target.values),
                flags=dict(target.flags),
                sources=target.sources,
                reasons=tuple(target.reasons)
                + (f"本版本由历史版本 {target_version} 回滚重新发布",),
            )
            self._store.publish(snapshot)
            self._history.retain(snapshot)
            self._history.record(
                kind="rollback",
                outcome="published",
                version=snapshot.version,
                reason=f"回滚到历史版本 {target_version} 并重新发布",
                detail={
                    "target_version": target_version,
                    "sources": list(snapshot.sources),
                },
            )
        logger.info(
            "rollback 成功，生效版本=%d（内容来自历史版本=%d），配置=%s",
            snapshot.version,
            target_version,
            snapshot.to_public_dict(),
        )
        return snapshot

    # ------------------------------------------------------------------ #
    # 变更留痕
    # ------------------------------------------------------------------ #
    def audit_records(self) -> list[ChangeRecord]:
        """全部变更留痕（按发生顺序，含成功与被拒）。"""
        return self._history.records()

    def _record_rejected(
        self,
        kind: str,
        exc: ConfigError,
        *,
        sources: list[str] | None = None,
    ) -> None:
        """为一次被拒的更新留痕（携带具体错误码，而非笼统失败）。"""
        detail: dict[str, Any] = {"error": exc.to_dict()}
        if sources is not None:
            detail["sources"] = list(sources)
        rec = self._history.record(
            kind=kind,
            outcome="rejected",
            version=None,
            reason=f"{exc.code.value}: {exc}",
            error_code=exc.code.value,
            detail=detail,
        )
        logger.info(
            "更新被拒绝已留痕: seq=%d kind=%s code=%s detail=%s",
            rec.seq,
            kind,
            exc.code.value,
            exc.to_dict(),
        )

    # ------------------------------------------------------------------ #
    # 配置读取
    # ------------------------------------------------------------------ #
    def snapshot(self) -> Snapshot:
        """获取当前生效快照（同一次调用内配置天然同版本）。"""
        return self._store.current()

    def version(self) -> int:
        """当前生效版本号。"""
        return self._store.current().version

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
        """返回带完整判定依据的结论（基于调用时刻的当前快照）。"""
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
