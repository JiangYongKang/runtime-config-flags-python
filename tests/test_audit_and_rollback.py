"""变更留痕 + 历史版本 + 一键回滚的自动化测试。

覆盖：
- 成功发布与各类被拒更新都留痕，且错误码可区分（越界 / 来源缺失 / 版本冲突 /
  目标历史版本已淘汰）；
- 被拒更新不改变生效配置、开关、版本号与来源，不影响后续用原来源发布；
- 留痕条数有界，超出淘汰最旧，淘汰不影响生效版本；
- 历史版本有界淘汰；可查询保留范围内版本的完整配置；回滚内容与目标版本一致，
  新版本号单调递增、不复用旧号、不跳号；
- 回滚与无条件热更并发时严格串行，版本单调连续、无重号，最终快照自洽；
- 单次请求内版本号/配置/开关判定来自同一次生效结果。

日志（pytest -s）会打印每次操作的输入、最终版本与被拒原因。
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.audit import AuditLog, ChangeKind
from app.errors import ConfigError, ErrorCode
from app.service import RuntimeConfigService

logger = logging.getLogger("runtime_config.tests")


def _write(tmp_path, name, payload):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    logger.info("[测试输入配置] %s <- %s", name, json.dumps(payload, ensure_ascii=False))
    return str(path)


VALID_A = {
    "config": {"timeout_ms": 1000, "max_connections": 100},
    "features": {"f": {"percentage": 10}},
}
VALID_B = {
    "config": {"timeout_ms": 2000, "max_connections": 100},
    "features": {"f": {"percentage": 20}},
}
VALID_C = {
    "config": {"timeout_ms": 3000, "max_connections": 100},
    "features": {"f": {"percentage": 30}},
}
BAD_RANGE = {"config": {"timeout_ms": 999999}}


# ==================================================================== #
# 变更留痕：成功 / 被拒 / 原因可区分
# ==================================================================== #
def test_accepted_and_rejected_changes_are_recorded(tmp_path):
    p_a = _write(tmp_path, "a.json", VALID_A)
    p_b = _write(tmp_path, "b.json", VALID_B)
    p_bad = _write(tmp_path, "bad.json", BAD_RANGE)

    svc = RuntimeConfigService([p_a])
    svc.bootstrap()
    svc.reload([p_b])
    with pytest.raises(ConfigError):
        svc.reload([p_bad])

    entries = svc.list_changes()
    kinds = [(e.kind.value, e.outcome.value, e.error_code) for e in entries]
    logger.info("[留痕列表] %s", kinds)
    assert kinds == [
        ("bootstrap", "accepted", ""),
        ("reload", "accepted", ""),
        ("reload", "rejected", "value_out_of_range"),
    ]

    rejected = entries[-1]
    assert rejected.resulting_version is None
    assert rejected.detail["path"] == "config.timeout_ms"
    assert rejected.sources == (p_bad,)
    logger.info("[拒绝留痕] code=%s detail=%s", rejected.error_code, rejected.detail)

    # 被拒不改变版本与生效内容
    assert svc.version() == 2
    assert svc.get("timeout_ms") == 2000


def test_distinct_reject_reasons_are_distinguishable(tmp_path):
    p_a = _write(tmp_path, "a.json", VALID_A)
    p_b = _write(tmp_path, "b.json", VALID_B)
    svc = RuntimeConfigService([p_a])
    svc.bootstrap()
    svc.reload([p_b])  # -> v2

    # 1) 来源不存在
    ghost = str(tmp_path / "ghost.json")
    with pytest.raises(ConfigError) as ei:
        svc.reload([ghost])
    assert ei.value.code is ErrorCode.SOURCE_NOT_FOUND

    # 2) 取值越界
    p_bad = _write(tmp_path, "bad.json", BAD_RANGE)
    with pytest.raises(ConfigError) as ei:
        svc.reload([p_bad])
    assert ei.value.code is ErrorCode.VALUE_OUT_OF_RANGE

    # 3) 显式 CAS 过期 -> 版本冲突
    with pytest.raises(ConfigError) as ei:
        svc.reload([p_a], expected_version=1)
    assert ei.value.code is ErrorCode.VERSION_CONFLICT
    logger.info("[版本冲突原因] %s", ei.value.to_dict())

    # 4) 回滚目标版本不在保留范围
    with pytest.raises(ConfigError) as ei:
        svc.rollback(12345)
    assert ei.value.code is ErrorCode.HISTORY_VERSION_NOT_FOUND
    ctx = ei.value.context
    assert ctx["target_version"] == 12345
    assert ctx["kept_versions"] == [1, 2]
    logger.info("[历史缺失原因] %s", ei.value.to_dict())

    codes = [e.error_code for e in svc.list_changes() if e.outcome.value == "rejected"]
    assert codes == [
        "source_not_found",
        "value_out_of_range",
        "version_conflict",
        "history_version_not_found",
    ]
    # 所有拒绝后生效状态始终保持 v2
    assert svc.version() == 2
    assert svc.get("timeout_ms") == 2000
    logger.info("[四类拒绝后] 仍为版本=%d timeout_ms=%d", svc.version(), svc.get("timeout_ms"))


def test_rejected_reload_keeps_source_for_later_updates(tmp_path):
    """被拒更新不能污染来源；下一次不带 sources 的热更仍用原来源正常发布。"""
    p_a = _write(tmp_path, "a.json", VALID_A)
    svc = RuntimeConfigService([p_a])
    svc.bootstrap()

    p_bad = _write(tmp_path, "bad.json", BAD_RANGE)
    with pytest.raises(ConfigError):
        svc.reload([p_bad])
    assert svc.source_paths == [p_a]

    # 修改原来源文件内容后，不带 sources 热更应沿用原路径并成功
    _write(tmp_path, "a.json", VALID_B)
    snap = svc.reload()
    assert snap.version == 2
    assert snap.values["timeout_ms"] == 2000
    assert svc.source_paths == [p_a]
    logger.info("[被拒后续发] 沿用原来源=%s，新版本=%d", svc.source_paths, snap.version)


# ==================================================================== #
# 留痕条数上限与淘汰
# ==================================================================== #
def test_audit_log_unit_evicts_oldest_and_keeps_seq():
    log = AuditLog(limit=3)
    for i in range(5):
        log.record_accepted(
            kind=ChangeKind.RELOAD,
            resulting_version=i + 1,
            sources=("s",),
        )
    entries = log.list_entries()
    assert [e.seq for e in entries] == [3, 4, 5]  # 最旧两条被淘汰
    assert [e.resulting_version for e in entries] == [3, 4, 5]
    assert log.next_seq == 6  # 总计数不因淘汰回绕
    logger.info("[留痕淘汰] 保留 seq=%s，下一条 seq=%d", [e.seq for e in entries], log.next_seq)


def test_service_audit_eviction_does_not_touch_live_version(tmp_path):
    p_a = _write(tmp_path, "a.json", VALID_A)
    p_b = _write(tmp_path, "b.json", VALID_B)
    svc = RuntimeConfigService([p_a], history_limit=100, audit_limit=2)
    svc.bootstrap()
    svc.reload([p_b])
    with pytest.raises(ConfigError):
        svc.reload([str(tmp_path / "ghost.json")])

    entries = svc.list_changes()
    assert len(entries) == 2  # bootstrap 记录已被淘汰
    assert [e.seq for e in entries] == [2, 3]
    # 淘汰留痕不影响当前生效版本与后续发布
    assert svc.version() == 2
    snap = svc.reload([p_a])
    assert snap.version == 3
    logger.info("[留痕淘汰下] 生效版本=%d 正常继续", svc.version())


def test_invalid_audit_limit_rejected():
    with pytest.raises(ValueError):
        AuditLog(limit=0)
    with pytest.raises(TypeError):
        AuditLog(limit=1.5)  # type: ignore[arg-type]


# ==================================================================== #
# 历史版本：保留、淘汰、查询
# ==================================================================== #
def test_history_retention_evicts_oldest(tmp_path):
    paths = [
        _write(tmp_path, f"v{i}.json", {
            "config": {"timeout_ms": 1000 + i, "max_connections": 100},
            "features": {"f": {"percentage": i}},
        })
        for i in range(1, 6)
    ]
    svc = RuntimeConfigService([paths[0]], history_limit=3)
    svc.bootstrap()
    for p in paths[1:]:
        svc.reload([p])

    assert svc.list_history_versions() == [3, 4, 5]
    assert svc.historical_snapshot(3).version == 3
    with pytest.raises(ConfigError) as ei:
        svc.historical_snapshot(1)
    assert ei.value.code is ErrorCode.HISTORY_VERSION_NOT_FOUND
    # 历史淘汰不影响当前生效版本（v5）
    assert svc.version() == 5
    assert svc.get("timeout_ms") == 1005
    logger.info("[历史淘汰] 保留=%s，生效仍为=%d", svc.list_history_versions(), svc.version())


def test_history_query_is_self_consistent(tmp_path):
    """历史查询返回的版本号、配置、开关必须互相自洽。"""
    p_a = _write(tmp_path, "a.json", VALID_A)
    p_b = _write(tmp_path, "b.json", VALID_B)
    svc = RuntimeConfigService([p_a])
    svc.bootstrap()
    svc.reload([p_b])

    old = svc.historical_snapshot(1)
    assert old.version == 1
    assert old.values["timeout_ms"] == 1000
    assert old.flags["f"].percentage == 10
    # 在历史快照上做开关判定，版本号必须也是 1
    decision = svc.evaluate_on(old, "f", "user")
    assert decision.version == 1
    logger.info("[历史自洽] version=%d timeout=%s pct=%d 判定版本=%d",
                old.version, old.values["timeout_ms"],
                old.flags["f"].percentage, decision.version)


def test_rollback_to_evicted_version_rejected_and_state_unchanged(tmp_path):
    p_a = _write(tmp_path, "a.json", VALID_A)
    svc = RuntimeConfigService([p_a], history_limit=2)
    svc.bootstrap()  # v1
    _write(tmp_path, "a.json", VALID_B)
    svc.reload()    # v2
    _write(tmp_path, "a.json", VALID_C)
    svc.reload()    # v3 -> 历史只剩 [2,3]，v1 淘汰

    before = svc.snapshot()
    with pytest.raises(ConfigError) as ei:
        svc.rollback(1)
    assert ei.value.code is ErrorCode.HISTORY_VERSION_NOT_FOUND
    after = svc.snapshot()
    assert after is before  # 当前生效快照引用都没变
    assert svc.version() == 3 and svc.get("timeout_ms") == 3000
    logger.info("[回滚淘汰目标被拒] 当前版本保持=%d", svc.version())


# ==================================================================== #
# 一键回滚：内容一致 + 版本全新单调
# ==================================================================== #
def test_rollback_republishes_target_content_with_new_version(tmp_path):
    p_a = _write(tmp_path, "a.json", VALID_A)
    p_b = _write(tmp_path, "b.json", VALID_B)
    p_c = _write(tmp_path, "c.json", VALID_C)
    svc = RuntimeConfigService([p_a])
    svc.bootstrap()  # v1: timeout=1000, pct=10
    svc.reload([p_b])  # v2: 2000/20
    svc.reload([p_c])  # v3: 3000/30

    snap = svc.rollback(1)
    # 全新版本号：+1、不复用旧号 1、不跳号
    assert snap.version == 4
    assert svc.version() == 4
    # 内容与目标历史版本 v1 完全一致
    assert snap.values["timeout_ms"] == 1000
    assert snap.flags["f"].percentage == 10
    assert svc.get("timeout_ms") == 1000
    assert svc.evaluate_flag("f", "u").percentage == 10
    # 回滚来源沿用目标版本来源，且留一条可追溯说明
    assert svc.source_paths == [p_a]
    assert any("rollback" in r and "1" in r for r in snap.reasons)
    logger.info(
        "[回滚内容一致] 目标=v1 -> 新版本=%d timeout=%s pct=%d sources=%s",
        snap.version, snap.values["timeout_ms"],
        snap.flags["f"].percentage, list(snap.sources),
    )

    # 回滚后再热更：版本继续单调 +1（4 -> 5），不跳号
    snap2 = svc.reload([p_c])
    assert snap2.version == 5
    logger.info("[回滚后再热更] 版本=%d（单调继续）", snap2.version)


def test_rollback_creates_independent_snapshot(tmp_path):
    """回滚产生的新快照与历史快照是不同版本对象，后续淘汰互不影响。"""
    p_a = _write(tmp_path, "a.json", VALID_A)
    p_b = _write(tmp_path, "b.json", VALID_B)
    svc = RuntimeConfigService([p_a], history_limit=10)
    svc.bootstrap()
    svc.reload([p_b])
    target = svc.historical_snapshot(1)
    rebuilt = svc.rollback(1)
    assert rebuilt is not target
    assert rebuilt.version == 3 and target.version == 1
    # 配置内容相等，但版本号不同
    assert dict(rebuilt.values) == dict(target.values)
    assert dict(rebuilt.flags) == dict(target.flags)
    logger.info("[回滚独立性] 历史 v%d 内容以全新 v%d 发布", target.version, rebuilt.version)


def test_rollback_cas_conflict_leaves_state_unchanged(tmp_path):
    p_a = _write(tmp_path, "a.json", VALID_A)
    p_b = _write(tmp_path, "b.json", VALID_B)
    svc = RuntimeConfigService([p_a])
    svc.bootstrap()
    svc.reload([p_b])  # v2
    with pytest.raises(ConfigError) as ei:
        svc.rollback(1, expected_version=1)  # 期望基线已过期
    assert ei.value.code is ErrorCode.VERSION_CONFLICT
    assert svc.version() == 2
    assert svc.get("timeout_ms") == 2000
    # 留痕中应能找到 rollback + version_conflict
    rb_rejects = [
        e for e in svc.list_changes()
        if e.kind.value == "rollback" and e.outcome.value == "rejected"
    ]
    assert rb_rejects and rb_rejects[0].error_code == "version_conflict"
    assert rb_rejects[0].target_version == 1
    logger.info("[回滚 CAS 冲突留痕] %s", rb_rejects[0].to_dict())


def test_rollback_version_is_monotonic_under_repeated_rollbacks(tmp_path):
    p_a = _write(tmp_path, "a.json", VALID_A)
    p_b = _write(tmp_path, "b.json", VALID_B)
    svc = RuntimeConfigService([p_a], history_limit=100)
    svc.bootstrap()
    svc.reload([p_b])  # v2
    v3 = svc.rollback(1)  # v3 内容=A
    v4 = svc.rollback(2)  # v4 内容=B
    v5 = svc.rollback(1)  # v5 内容=A
    assert [v3.version, v4.version, v5.version] == [3, 4, 5]
    assert v3.values["timeout_ms"] == 1000
    assert v4.values["timeout_ms"] == 2000
    assert v5.values["timeout_ms"] == 1000
    logger.info("[连续回滚] 版本序列=3,4,5，版本号全新不跳号")


# ==================================================================== #
# 单次请求一致性
# ==================================================================== #
def test_single_request_sees_one_consistent_version(tmp_path):
    p_a = _write(tmp_path, "a.json", VALID_A)
    svc = RuntimeConfigService([p_a])
    svc.bootstrap()
    snap = svc.snapshot()
    # 同一快照内：版本号、配置、开关判定全部同版本
    decision = svc.evaluate_on(snap, "f", "alice")
    bundle = snap.to_public_dict()
    assert decision.version == snap.version == bundle["version"]
    assert bundle["flags"]["f"]["percentage"] == snap.flags["f"].percentage
    logger.info(
        "[单次请求一致] version=%d config=%s flag_pct=%d decision_version=%d",
        snap.version, snap.values["timeout_ms"],
        snap.flags["f"].percentage, decision.version,
    )


def test_request_consistency_during_mixed_concurrent_updates(tmp_path):
    """回滚与热更高压并发期间，任一读者看到的版本/配置/开关必须同源同版本。"""
    paths = {}
    for v in range(1, 8):
        paths[v] = _write(tmp_path, f"v{v}.json", {
            "config": {
                "timeout_ms": 1000 + v,
                "max_connections": 100 + v,
            },
            "features": {"f": {"percentage": v}},
        })
    svc = RuntimeConfigService([paths[1]], history_limit=100)
    svc.bootstrap()

    stop = threading.Event()
    violations: list[str] = []

    def reader(idx: int) -> None:
        while not stop.is_set():
            snap = svc.snapshot()
            v = snap.version
            # 任一版本的内容都形如 timeout=1000+x, pct=x，其中 x 与该版本的
            # “实际来源内容”对应；由于回滚复用历史内容，用版本内容自洽性校验：
            decision = svc.evaluate_on(snap, "f", f"user-{idx}")
            if decision.version != v:
                violations.append(f"reader{idx}: 判定版本 {decision.version} != 快照版本 {v}")
            bundle = snap.to_public_dict()
            if bundle["version"] != v:
                violations.append(f"reader{idx}: 公开结构版本与快照不一致")
            if bundle["config"]["timeout_ms"] != snap.values["timeout_ms"]:
                violations.append(f"reader{idx}: 公开配置与快照值不一致")
            if bundle["flags"]["f"]["percentage"] != snap.flags["f"].percentage:
                violations.append(f"reader{idx}: 公开开关与快照规则不一致")

    versions_seen: list[int] = []
    vlock = threading.Lock()

    def writer(step: int) -> None:
        # 交替做普通热更（无条件）与回滚到某个仍保留的历史版本
        if step % 2 == 0:
            p = paths[1 + (step % 7)]
            snap = svc.reload([p])
        else:
            target = svc.list_history_versions()[0]  # 最旧但仍保留的版本
            snap = svc.rollback(target)
        with vlock:
            versions_seen.append(snap.version)

    with ThreadPoolExecutor(max_workers=12) as pool:
        readers = [pool.submit(reader, i) for i in range(8)]
        list(pool.map(writer, range(40)))
        stop.set()
        for f in readers:
            f.result(timeout=10)

    assert not violations, violations
    # 所有写操作产生的版本必须严格连续（2..41），无跳号无重号
    assert sorted(versions_seen) == list(range(2, 42)), sorted(versions_seen)
    logger.info(
        "[混合并发一致] 40 次写（热更+回滚）版本集合 2..41 无跳号；最终版本=%d",
        svc.version(),
    )


# ==================================================================== #
# 回滚 + 并发热更：串行、单调、最终一致
# ==================================================================== #
def test_concurrent_reload_and_rollback_are_serialized(tmp_path):
    """回滚和普通热更严格串行：版本单调连续；无条件热更不失败。"""
    p_a = _write(tmp_path, "a.json", VALID_A)
    p_b = _write(tmp_path, "b.json", VALID_B)
    svc = RuntimeConfigService([p_a], history_limit=100)
    svc.bootstrap()

    outcomes: list[tuple[str, int]] = []
    lock = threading.Lock()

    def do_reload(i: int) -> None:
        snap = svc.reload([p_b])  # 无条件热更：合法内容必须成功
        with lock:
            outcomes.append(("reload", snap.version))

    def do_rollback(i: int) -> None:
        snap = svc.rollback(1)  # 目标 v1 始终保留（limit=100）
        with lock:
            outcomes.append(("rollback", snap.version))

    with ThreadPoolExecutor(max_workers=16) as pool:
        futs = []
        for i in range(30):
            futs.append(pool.submit(do_reload if i % 2 == 0 else do_rollback, i))
        for f in futs:
            f.result(timeout=30)

    versions = sorted(v for _, v in outcomes)
    assert versions == list(range(2, 32)), versions  # 30 次写 -> v2..v31
    assert len(set(versions)) == 30
    # 最终快照自洽：版本号与内容同属一次生效
    final = svc.snapshot()
    assert final.version == 31
    last_op = [k for k, v in outcomes if v == 31][0]
    expected_timeout = 2000 if last_op == "reload" else 1000
    expected_pct = 20 if last_op == "reload" else 10
    assert final.values["timeout_ms"] == expected_timeout
    assert final.flags["f"].percentage == expected_pct
    assert svc.evaluate_flag("f", "u").version == 31
    logger.info(
        "[回滚+热更并发] 30 次写串行完成，版本 2..31；最后操作=%s 最终内容 timeout=%s pct=%d",
        last_op, expected_timeout, expected_pct,
    )


def test_concurrent_unconditional_reloads_never_fail(tmp_path):
    """不带 expected_version 的合法热更在并发下必须全部成功（不因 CAS 大面积失败）。"""
    p_a = _write(tmp_path, "a.json", VALID_A)
    svc = RuntimeConfigService([p_a])
    svc.bootstrap()

    errors: list[str] = []

    def do(i: int) -> int:
        try:
            return svc.reload([p_a]).version
        except ConfigError as exc:  # pragma: no cover - 不允许发生
            errors.append(exc.code.value)
            return -1

    with ThreadPoolExecutor(max_workers=16) as pool:
        versions = list(pool.map(do, range(40)))

    assert not errors, errors
    assert sorted(versions) == list(range(2, 42))
    logger.info("[无条件并发热更] 40/40 全部成功，版本 2..41，最终=%d", svc.version())
