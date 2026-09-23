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
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import DEFAULT_FIELD_SPECS, build_snapshot
from app.errors import ConfigError, ErrorCode
from app.flags import FlagDecision, evaluate
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
    ) -> None:
        self._source_paths: list[str] = list(source_paths or [])
        self._field_specs = field_specs
        self._strict = strict
        self._store = store or SnapshotStore()

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

        self._store.publish(snapshot)
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
        - expected_version 为 None 时基于当前生效版本做 CAS；
        - 任何校验失败或版本冲突都不会改变当前生效状态。
        """
        if source_paths is not None:
            self._source_paths = list(source_paths)

        current = self._store.try_current()
        base_version = current.version if current is not None else 0
        if expected_version is not None:
            base_version = expected_version

        new_version = base_version + 1
        logger.info(
            "reload 开始: 基于版本=%d -> 新版本=%d sources=%s",
            base_version,
            new_version,
            self._source_paths,
        )

        # 先在临时数据上完成全部加载与校验；失败时存储完全不被触碰。
        snapshot = build_snapshot(
            self._source_paths,
            field_specs=self._field_specs,
            version=new_version,
        )

        # 校验通过后做原子条件发布；失败（并发更新）时返回冲突，状态不变。
        self._store.publish(snapshot, expected_version=base_version)
        logger.info(
            "reload 成功，生效版本=%d，输入配置=%s",
            snapshot.version,
            snapshot.to_public_dict(),
        )
        return snapshot

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
