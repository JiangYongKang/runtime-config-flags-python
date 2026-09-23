"""配置中心：持有当前生效版本，支持原子热更新。

并发模型：
- 当前生效配置是一个不可变 MergedConfig，整体替换；
- 读路径只读取一次快照引用，因此任一请求看到的必是同一版本的完整配置，
  绝不会读到“半个版本”；
- 写路径在 RLock 内完成“解析 -> 合并 -> 发布”，失败时直接丢弃新对象，
  当前引用保持不变 => 无效更新天然回滚，当前生效状态不变；
- try_update 支持基于 expected_version 的乐观条件更新，便于并发写追溯
  VERSION_MISMATCH 失败原因。
"""

from __future__ import annotations

import threading
from typing import Any

from runtime.errors import (
    ConfigIssue,
    ConfigRejected,
    NotInitializedError,
    ReasonCode,
)
from runtime.flags import evaluate
from runtime.merger import merge_sources
from runtime.model import EvaluationResult, MergedConfig
from runtime.parser import parse_source


class ConfigStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current: MergedConfig | None = None
        # 最近一次被拒绝更新的留痕（原因可追溯），按时间追加，仅保留最近 20 条
        self._rejected: list[dict[str, Any]] = []

    # ---------- 读路径（无锁，引用赋值在 CPython 下原子；语义上快照不可变） ----------

    def current(self) -> MergedConfig:
        snap = self._current
        if snap is None:
            raise NotInitializedError()
        return snap

    def current_version(self) -> int:
        snap = self._current
        return 0 if snap is None else snap.version

    def is_initialized(self) -> bool:
        return self._current is not None

    def evaluate(self, flag: str, subject: str) -> EvaluationResult:
        """取一次当前快照并基于它判定（便捷方法）。

        注意：若一个请求内还需要同时读取 settings/flags，请先用 current()
        取一次快照，再用 evaluate_snapshot(snap, ...)，保证整请求同版本。
        """
        return evaluate(self.current(), flag, subject)

    @staticmethod
    def evaluate_snapshot(snap: MergedConfig, flag: str,
                          subject: str) -> EvaluationResult:
        """基于调用方已取到的快照判定：整个请求严格同一版本。"""
        return evaluate(snap, flag, subject)

    def rejected_history(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(self._rejected)

    # ---------- 写路径 ----------

    def load_sources(self, raws: list[dict[str, Any]]) -> MergedConfig:
        """解析+合并+原子发布；任一环节失败则当前版本保持不变。"""
        return self._publish(raws, expected_version=None)

    def try_update(
        self, raws: list[dict[str, Any]], *, expected_version: int
    ) -> MergedConfig:
        """乐观条件更新：版本不符抛 ConfigRejected(VERSION_MISMATCH)。"""
        return self._publish(raws, expected_version=expected_version)

    def _publish(
        self,
        raws: list[Any],
        *,
        expected_version: int | None,
    ) -> MergedConfig:
        with self._lock:
            old = self._current
            if expected_version is not None:
                cur_ver = old.version if old is not None else 0
                if cur_ver != expected_version:
                    issue = ConfigIssue(
                        ReasonCode.VERSION_MISMATCH, "$",
                        f"条件更新失败：期望基准版本 {expected_version}，"
                        f"当前生效版本为 {cur_ver}，当前状态未被改变")
                    self._record_rejection(raws, [issue])
                    raise ConfigRejected([issue])

            # 解析与合并全部在锁内完成，但在真正赋值前不触碰 _current，
            # 因此任何异常都意味着当前版本纹丝不动。
            issues: list[ConfigIssue] = []
            sources = []
            seen_ids: set[str] = set()
            for idx, raw in enumerate(raws or []):
                try:
                    parsed = parse_source(raw)
                except ConfigRejected as exc:
                    issues.extend(exc.issues)
                    continue
                if parsed.source_id in seen_ids:
                    issues.append(ConfigIssue(
                        ReasonCode.SOURCE_CONFLICT,
                        f"sources[{idx}].source_id",
                        f"来源 id {parsed.source_id!r} 在一次更新中重复出现",
                        source=parsed.source_id))
                    continue
                seen_ids.add(parsed.source_id)
                sources.append(parsed)

            if not issues:
                next_version = (old.version + 1) if old is not None else 1
                try:
                    merged = merge_sources(sources, version=next_version)
                except ConfigRejected as exc:
                    issues = exc.issues

            if issues:
                self._record_rejection(raws, issues)
                raise ConfigRejected(issues)

            # 单次引用赋值：原子发布，读者要么看到旧版本，要么看到新版本
            self._current = merged
            return merged

    def _record_rejection(self, raws: Any,
                          issues: list[ConfigIssue]) -> None:
        self._rejected.append({
            "issues": [
                {"code": i.code.value, "path": i.path,
                 "message": i.message, "source": i.source}
                for i in issues
            ],
            "attempted_sources": _summarize_raws(raws),
            "stayed_on_version": self.current_version(),
        })
        del self._rejected[:-20]


def _summarize_raws(raws: Any) -> list[str]:
    out: list[str] = []
    if isinstance(raws, list):
        for raw in raws:
            if isinstance(raw, dict):
                out.append(str(raw.get("source_id", "<missing>")))
            else:
                out.append(f"<{type(raw).__name__}>")
    return out
