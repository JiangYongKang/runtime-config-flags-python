"""变更留痕、历史版本查询与一键回滚。

覆盖：
- 成功热更 / 被拒热更都留痕，且被拒原因可区分（具体错误码）；
- 被拒更新不改变生效配置/开关/版本，也不污染后续更新的来源；
- 留痕条数上限与淘汰（淘汰不影响生效版本）；
- 历史版本查询自洽；回滚后内容与目标历史版本一致，版本号全新单调；
- 回滚与并发热更严格串行：版本单调不跳号、最终一致；
  不带 expected_version 的合法热更在并发下全部成功。
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import ConfigError, ErrorCode
from app.service import RuntimeConfigService

logger = logging.getLogger("runtime_config.tests")


def _payload(timeout_ms: int, pct: int) -> dict:
    return {
        "config": {"timeout_ms": timeout_ms},
        "features": {"f": {"default_on": True, "percentage": pct}},
    }


def _write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    logger.info("[测试输入配置] %s <- %s", name, json.dumps(payload))
    return str(path)


def _make(tmp_path, **kwargs):
    p1 = _write(tmp_path, "v1.json", _payload(1000, 10))
    svc = RuntimeConfigService([p1], **kwargs)
    svc.bootstrap()
    return svc


# --------------------------------------------------------------------- #
# 留痕：成功与被拒
# --------------------------------------------------------------------- #
def test_successful_reload_leaves_audit_record(tmp_path):
    svc = _make(tmp_path)
    p2 = _write(tmp_path, "v2.json", _payload(2000, 20))
    snap = svc.reload([p2])
    logger.info("[操作] reload(v2) -> 生效版本=%d", snap.version)

    records = svc.audit_records()
    for rec in records:
        logger.info("[留痕] %s", rec.to_dict())
    last = records[-1]
    assert last.kind == "reload"
    assert last.outcome == "published"
    assert last.version == 2
    assert last.error_code is None


def test_rejected_reload_leaves_distinguishable_audit_records(tmp_path):
    svc = _make(tmp_path)

    # 1) 内容非法（取值越界）
    p_bad = _write(tmp_path, "bad.json", {"config": {"timeout_ms": 999999}})
    with pytest.raises(ConfigError) as exc_info:
        svc.reload([p_bad])
    assert exc_info.value.code is ErrorCode.VALUE_OUT_OF_RANGE

    # 2) 来源不存在
    with pytest.raises(ConfigError) as exc_info:
        svc.reload([str(tmp_path / "ghost.json")])
    assert exc_info.value.code is ErrorCode.SOURCE_NOT_FOUND

    # 3) CAS 版本冲突
    with pytest.raises(ConfigError) as exc_info:
        svc.reload(expected_version=99)
    assert exc_info.value.code is ErrorCode.VERSION_CONFLICT

    records = svc.audit_records()
    for rec in records:
        logger.info("[留痕] %s", rec.to_dict())
    rejected = [r for r in records if r.outcome == "rejected"]
    codes = [r.error_code for r in rejected]
    logger.info("[被拒原因序列] %s", codes)
    # 三类拒绝原因可区分，而不是笼统的失败
    assert codes == [
        ErrorCode.VALUE_OUT_OF_RANGE.value,
        ErrorCode.SOURCE_NOT_FOUND.value,
        ErrorCode.VERSION_CONFLICT.value,
    ]
    assert all(r.version is None for r in rejected)


def test_rejected_reload_keeps_state_and_original_sources(tmp_path):
    svc = _make(tmp_path)
    p_bad = _write(tmp_path, "bad.json", {"config": {"timeout_ms": 999999}})
    with pytest.raises(ConfigError):
        svc.reload([p_bad])  # 携带新来源但被拒

    # 生效配置、开关、版本均未变化
    assert svc.version() == 1
    assert svc.get("timeout_ms") == 1000
    assert svc.evaluate_flag("f", "u").percentage == 10

    # 后续不带 sources 的更新仍用“原来的来源”正常发布（来源未被污染）
    snap = svc.reload()
    logger.info("[被拒后续更新] 生效版本=%d timeout_ms=%s", snap.version, svc.get("timeout_ms"))
    assert snap.version == 2
    assert svc.get("timeout_ms") == 1000  # 仍来自原来源 v1.json


# --------------------------------------------------------------------- #
# 留痕上限与淘汰
# --------------------------------------------------------------------- #
def test_audit_records_are_bounded_and_evict_oldest(tmp_path):
    svc = _make(tmp_path, audit_limit=5)
    # 7 次成功 + 2 次被拒 = 9 次变更 + 1 次 bootstrap = 10 条，上限 5
    for i in range(7):
        svc.reload()
    for _ in range(2):
        with pytest.raises(ConfigError):
            svc.reload([str(tmp_path / "ghost.json")])

    records = svc.audit_records()
    for rec in records:
        logger.info("[留痕] %s", rec.to_dict())
    assert len(records) == 5
    seqs = [r.seq for r in records]
    assert seqs == sorted(seqs)  # 保序
    assert seqs[-1] == 10  # 最旧的已被淘汰，只留最新 5 条
    # 淘汰不影响生效版本
    assert svc.version() == 8
    assert svc.get("timeout_ms") == 1000
    logger.info("[淘汰后] 留痕 seq=%s，生效版本=%d（未受影响）", seqs, svc.version())


# --------------------------------------------------------------------- #
# 历史版本查询
# --------------------------------------------------------------------- #
def test_history_snapshot_is_self_consistent(tmp_path):
    svc = _make(tmp_path)
    p2 = _write(tmp_path, "v2.json", _payload(2000, 20))
    svc.reload([p2])

    logger.info("[保留版本] %s", svc.history_versions())
    assert svc.history_versions() == [1, 2]

    snap1 = svc.history_snapshot(1)
    # 同一次查询里版本号、配置、开关都来自同一历史版本
    assert snap1.version == 1
    assert snap1.values["timeout_ms"] == 1000
    assert snap1.flags["f"].percentage == 10
    logger.info("[历史查询] version=%d config=%s flags=%s",
                snap1.version, dict(snap1.values),
                {k: v.percentage for k, v in snap1.flags.items()})


def test_history_snapshot_not_retained_is_distinguishable(tmp_path):
    svc = _make(tmp_path)
    with pytest.raises(ConfigError) as exc_info:
        svc.history_snapshot(42)
    logger.info("[历史查询被拒] %s", exc_info.value.to_dict())
    assert exc_info.value.code is ErrorCode.VERSION_NOT_RETAINED


def test_history_retention_is_bounded_and_eviction_keeps_current(tmp_path):
    svc = _make(tmp_path, history_limit=3)
    for _ in range(4):  # 版本推进到 5
        svc.reload()
    logger.info("[保留版本] %s（上限 3）", svc.history_versions())
    assert svc.history_versions() == [3, 4, 5]
    # 最旧版本已淘汰：查询/回滚给出可区分拒绝
    with pytest.raises(ConfigError) as exc_info:
        svc.rollback(1)
    assert exc_info.value.code is ErrorCode.VERSION_NOT_RETAINED
    # 淘汰不影响当前生效版本
    assert svc.version() == 5
    assert svc.get("timeout_ms") == 1000


# --------------------------------------------------------------------- #
# 一键回滚
# --------------------------------------------------------------------- #
def test_rollback_republishes_history_with_fresh_version(tmp_path):
    svc = _make(tmp_path)
    p2 = _write(tmp_path, "v2.json", _payload(2000, 20))
    p3 = _write(tmp_path, "v3.json", _payload(3000, 30))
    svc.reload([p2])
    svc.reload([p3])
    assert svc.version() == 3

    target = svc.history_snapshot(1)
    snap = svc.rollback(1)
    logger.info(
        "[回滚] 目标版本=1 -> 新版本=%d，配置=%s",
        snap.version,
        snap.to_public_dict()["config"],
    )
    # 全新版本号：当前 3 + 1 = 4，不复用旧号、不跳号
    assert snap.version == 4
    assert svc.version() == 4
    # 内容与目标历史版本完全一致
    assert dict(snap.values) == dict(target.values)
    assert {k: (v.default_on, v.percentage, dict(v.overrides), v.salt)
            for k, v in snap.flags.items()} == {
        k: (v.default_on, v.percentage, dict(v.overrides), v.salt)
        for k, v in target.flags.items()
    }
    assert svc.get("timeout_ms") == 1000
    assert svc.evaluate_flag("f", "u").percentage == 10
    # 回滚本身也留痕
    last = svc.audit_records()[-1]
    logger.info("[留痕] %s", last.to_dict())
    assert last.kind == "rollback" and last.outcome == "published" and last.version == 4
    # 回滚后的版本也进入历史，可再次查询
    assert svc.history_snapshot(4).values["timeout_ms"] == 1000


def test_rollback_to_not_retained_version_rejected_and_state_unchanged(tmp_path):
    svc = _make(tmp_path)
    before = svc.snapshot()
    with pytest.raises(ConfigError) as exc_info:
        svc.rollback(99)
    err = exc_info.value
    logger.info("[回滚被拒] %s", err.to_dict())
    assert err.code is ErrorCode.VERSION_NOT_RETAINED
    # 当前生效状态保持不变
    assert svc.version() == before.version
    assert svc.snapshot().values["timeout_ms"] == before.values["timeout_ms"]
    # 被拒也留痕，原因可区分
    last = svc.audit_records()[-1]
    assert last.kind == "rollback"
    assert last.outcome == "rejected"
    assert last.error_code == ErrorCode.VERSION_NOT_RETAINED.value


# --------------------------------------------------------------------- #
# 回滚与并发热更
# --------------------------------------------------------------------- #
def test_rollback_and_concurrent_reloads_are_serialized(tmp_path):
    svc = _make(tmp_path)
    payloads = [_payload(1000 + i * 100, i) for i in range(1, 6)]
    paths = [_write(tmp_path, f"rb_{i}.json", p) for i, p in enumerate(payloads)]

    published: list[int] = []
    failures: list[str] = []
    lock = threading.Lock()

    def do_reload(path: str) -> None:
        try:
            snap = svc.reload([path])  # 不带 expected_version：合法内容必须成功
            with lock:
                published.append(snap.version)
        except ConfigError as exc:  # pragma: no cover - 不应发生
            with lock:
                failures.append(exc.code.value)

    def do_rollback(target: int) -> None:
        try:
            snap = svc.rollback(target)
            with lock:
                published.append(snap.version)
        except ConfigError as exc:  # pragma: no cover - 目标在保留范围内，不应发生
            with lock:
                failures.append(exc.code.value)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(do_reload, p) for p in paths]
        futs += [pool.submit(do_rollback, 1) for _ in range(3)]
        for f in futs:
            f.result(timeout=30)

    logger.info("[并发发布序列] %s，失败=%s", sorted(published), failures)
    # 不带期望版本的合法热更 + 目标在保留范围内的回滚：全部成功
    assert not failures
    assert len(published) == 8
    # 版本严格单调、不重号、不跳号：2..9 连续
    assert sorted(published) == list(range(2, 10))
    assert svc.version() == 9
    # 最终一致：当前快照内容与留痕中最后一次发布自洽
    final = svc.snapshot()
    last = svc.audit_records()[-1]
    assert last.outcome == "published" and last.version == final.version
    logger.info("[最终一致] 生效版本=%d 配置=%s", final.version, dict(final.values))


def test_concurrent_plain_reloads_all_succeed(tmp_path):
    """不带 expected_version 的合法热更在并发下不应大面积失败。"""
    svc = _make(tmp_path)
    path = _write(tmp_path, "again.json", _payload(1500, 15))

    def attempt(_: int):
        return svc.reload([path]).version

    with ThreadPoolExecutor(max_workers=10) as pool:
        versions = list(pool.map(attempt, range(20)))

    logger.info("[并发热更版本序列] %s", sorted(versions))
    assert sorted(versions) == list(range(2, 22))  # 全部成功、连续递增
    assert svc.version() == 21
