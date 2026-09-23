"""配置中心：热更新原子可见、并发读版本一致、无效更新回滚、乐观版本控制。"""

from __future__ import annotations

import threading

import pytest

from runtime.errors import ConfigRejected, NotInitializedError, ReasonCode
from runtime.store import ConfigStore

from .conftest import log_case


def good(marker: int, *, pct: int = 0):
    # max_connections 编码为 100+marker：第 n 个版本传 marker=n 时，
    # 读侧可校验 settings(=100+n) 与 flags(pct=n) 来自同一版本。
    return [{
        "source_id": "src",
        "priority": 10,
        "settings": {
            "max_connections": 100 + marker,
            "request_timeout_ms": 3000,
            "sample_rate": 0.1,
            "log_level": "INFO",
        },
        "flags": {"feature_x": {"enabled": True, "percentage": pct,
                                "overrides": {}}},
    }]


BAD_PCT = [{
    "source_id": "src", "priority": 10,
    "settings": {"max_connections": 1, "request_timeout_ms": 1,
                 "sample_rate": 0.0, "log_level": "INFO"},
    "flags": {"feature_x": {"enabled": True, "percentage": 150}},
}]

BAD_RANGE = [{
    "source_id": "src", "priority": 10,
    "settings": {"max_connections": -9},
}]


def test_not_initialized_is_deterministic():
    store = ConfigStore()
    assert not store.is_initialized()
    with pytest.raises(NotInitializedError):
        store.current()
    log_case("未初始化读取：确定性抛 NOT_INITIALIZED",
             error=ReasonCode.NOT_INITIALIZED.value)


def test_invalid_update_rolls_back():
    store = ConfigStore()
    v1 = store.load_sources(good(0))  # max=100
    snapshot_before = store.current()
    for bad in (BAD_PCT, BAD_RANGE):
        with pytest.raises(ConfigRejected):
            store.load_sources(bad)
        assert store.current_version() == 1
        assert store.current() is snapshot_before
        # 回滚后旧配置完整可读
        assert store.current().settings["max_connections"] == 100
    hist = store.rejected_history()
    codes = {i["code"] for h in hist for i in h["issues"]}
    assert {ReasonCode.PERCENTAGE_OUT_OF_RANGE,
            ReasonCode.SETTING_OUT_OF_RANGE} <= codes
    log_case("无效更新回滚：版本与快照引用均不变",
             raws={"valid": good(0), "invalid": [BAD_PCT[0], BAD_RANGE[0]]},
             version=1,
             error={"rejected_codes": sorted(codes),
                    "stayed_on_version": 1})


def test_versions_monotonic_and_snapshots_immutable():
    store = ConfigStore()
    seen = []
    for marker in (1, 2, 3):
        snap = store.load_sources(good(marker, pct=marker))
        seen.append(snap)
        assert snap.version == marker
    assert [s.version for s in seen] == [1, 2, 3]
    # 旧快照不被后续发布修改（不可变、可复现）
    assert seen[0].settings["max_connections"] == 101  # 100+1
    assert seen[1].settings["max_connections"] == 102
    log_case("版本单调递增且旧快照保持不变",
             raws=[good(1), good(2), good(3)], version=3,
             result={"versions": [s.version for s in seen]})


def test_optimistic_version_mismatch_preserves_state():
    store = ConfigStore()
    store.load_sources(good(1))
    store.load_sources(good(2))
    with pytest.raises(ConfigRejected) as ei:
        store.try_update(good(3), expected_version=1)
    assert ei.value.issues[0].code == ReasonCode.VERSION_MISMATCH
    assert store.current_version() == 2
    # v2 由 good(2) 发布 => max_connections = 102
    assert store.current().settings["max_connections"] == 102
    snap = store.try_update(good(3), expected_version=2)  # ->103
    assert snap.version == 3
    log_case("乐观更新：过期基准拒绝(409 语义)，正确基准成功",
             version=store.current_version(),
             error=ei.value.issues[0].explain())


def test_concurrent_reads_always_see_one_complete_version():
    """读线程在热更新风暴中读到的每个快照必须内部自洽（同一版本）。"""
    store = ConfigStore()
    store.load_sources(good(1, pct=1))  # v1: max=101, pct=1
    errors: list[str] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                snap = store.current()
                # 单次取引用：settings、flags 与判定必须全部基于同一个快照对象
                marker = snap.settings["max_connections"]
                pct = snap.flags["feature_x"].percentage
                r = store.evaluate_snapshot(snap, "feature_x", "subject-7")
                if r.version != snap.version:
                    errors.append("evaluation version != snapshot version")
                if marker != 100 + snap.version:
                    errors.append(
                        f"torn read: marker={marker} version={snap.version}")
                if pct != snap.version:
                    errors.append(
                        f"torn flags: pct={pct} version={snap.version}")
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

    readers = [threading.Thread(target=reader) for _ in range(8)]
    for t in readers:
        t.start()

    for v in range(2, 60):
        store.load_sources(good(v, pct=v))
    # 夹杂无效更新，确保回滚也不干扰读者
    for _ in range(5):
        try:
            store.load_sources(BAD_PCT)
        except ConfigRejected:
            pass

    stop.set()
    for t in readers:
        t.join()

    assert not errors, errors[:5]
    assert store.current_version() == 59
    log_case("并发读写：8 读线程 x 58 次有效更新 + 无效更新，无撕裂读",
             raws={"writers": "59 load_sources", "readers": 8},
             version=store.current_version(),
             result={"errors": errors, "final_marker":
                     store.current().settings["max_connections"]})


def test_concurrent_writers_only_one_wins_per_version_gap():
    """多写者并发：每次发布要么失败要么版本 +1，不丢不跳不重。"""
    store = ConfigStore()
    store.load_sources(good(101))
    results: list[tuple[int, bool]] = []
    lock = threading.Lock()

    def writer(marker: int):
        try:
            snap = store.load_sources(good(marker))
            with lock:
                results.append((snap.version, True))
        except ConfigRejected:
            with lock:
                results.append((-1, False))

    threads = [threading.Thread(target=writer, args=(200 + i,))
               for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = store.current_version()
    assert final == 21  # 1 初始 + 20 次串行化发布
    published = sorted(v for v, ok in results if ok)
    assert published == list(range(2, 22))
    log_case("并发写者：20 次发布被串行化为连续版本 2..21",
             version=final, result={"published_versions": published})
