"""并发场景：

- 大量读者与高频热更并发时，任一读者在一次读取内拿到的必须是同一版本的
  完整配置（config 与 flags 版本号一致，字段值不串版本）；
- 无效热更在并发下也不会改变生效状态；
- 并发 reload 必须严格串行，版本号单调递增、不跳号。
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import ConfigError, ErrorCode

logger = logging.getLogger("runtime_config.tests")


def _make_versioned_file(tmp_path, version: int) -> str:
    payload = {
        "config": {
            "timeout_ms": 1000 + version,
            "max_connections": 100 + version,
            "sample_rate": 0.5,
            "enable_cache": True,
            "log_level": "info",
        },
        "features": {
            "f": {"percentage": version % 101},
            "g": {"percentage": (100 - version) % 101},
        },
    }
    path = tmp_path / f"v{version}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_concurrent_reads_always_see_consistent_version(tmp_path):
    from app.service import RuntimeConfigService

    paths = [_make_versioned_file(tmp_path, v) for v in range(1, 16)]
    svc = RuntimeConfigService([paths[0]])
    svc.bootstrap()

    stop = threading.Event()
    violations: list[str] = []
    seen_versions: set[int] = set()

    def reader(idx: int) -> None:
        while not stop.is_set():
            snap = svc.snapshot()  # 一次拿到完整快照
            # 快照内部一致性：所有字段必须属于同一版本
            expected_timeout = 1000 + snap.version
            if snap.values["timeout_ms"] != expected_timeout:
                violations.append(
                    f"reader{idx} 串版本: version={snap.version} timeout={snap.values['timeout_ms']}"
                )
            # 两个开关在“同一快照”上判定，版本必然一致
            df = svc.evaluate_on(snap, "f", f"user-{idx}")
            dg = svc.evaluate_on(snap, "g", f"user-{idx}")
            if df.version != snap.version or dg.version != snap.version:
                violations.append("开关判定版本与配置快照版本不一致")
            seen_versions.add(snap.version)

    def writer() -> None:
        for i in range(1, len(paths)):
            try:
                svc.reload([paths[i]])
            except ConfigError:
                pass  # CAS 冲突是允许的预期结果

    with ThreadPoolExecutor(max_workers=12) as pool:
        readers = [pool.submit(reader, i) for i in range(10)]
        w = pool.submit(writer)
        w.result(timeout=30)
        stop.set()
        for fut in readers:
            fut.result(timeout=10)

    assert not violations, violations
    logger.info("[并发一致性] 读者观察到的版本集合=%s（无串版本）", sorted(seen_versions))
    assert svc.version() == len(paths)


def test_concurrent_invalid_reloads_never_change_state(tmp_path):
    from app.service import RuntimeConfigService

    good = _make_versioned_file(tmp_path, 1)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"config": {"timeout_ms": 999999}}), encoding="utf-8")
    svc = RuntimeConfigService([good])
    svc.bootstrap()

    def attempt(i: int):
        try:
            svc.reload([str(bad)])
            return "accepted"
        except ConfigError as exc:
            return exc.code.value

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(40)))

    assert set(outcomes) == {ErrorCode.VALUE_OUT_OF_RANGE.value}
    assert svc.version() == 1
    assert svc.get("timeout_ms") == 1001
    logger.info("[并发无效更新] 40 次全部拒绝，版本保持=1")


def test_concurrent_valid_reloads_are_serialized(tmp_path):
    from app.service import RuntimeConfigService

    # 同一内容反复 reload；不加 expected_version（无条件发布）时，
    # 版本必须严格每次 +1，不出现跳号/重号。
    good = _make_versioned_file(tmp_path, 1)
    svc = RuntimeConfigService([good])
    svc.bootstrap()

    versions: list[int] = []
    lock = threading.Lock()

    def attempt(i: int):
        try:
            snap = svc.reload([good])
            with lock:
                versions.append(snap.version)
        except ConfigError:
            pass

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(attempt, range(30)))

    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))  # 无重号
    logger.info("[并发热更串行] 发布版本序列=%s", versions)


def test_concurrent_cas_only_one_succeeds(tmp_path):
    from app.service import RuntimeConfigService

    good = _make_versioned_file(tmp_path, 1)
    svc = RuntimeConfigService([good])
    svc.bootstrap()

    def attempt(i: int):
        try:
            svc.reload([good], expected_version=1)
            return "ok"
        except ConfigError as exc:
            return exc.code.value

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(attempt, range(10)))

    assert outcomes.count("ok") == 1
    assert outcomes.count(ErrorCode.VERSION_CONFLICT.value) == 9
    assert svc.version() == 2
    logger.info("[并发 CAS] 1 成功 / 9 版本冲突，最终版本=2")


def test_all_config_and_flags_read_together(tmp_path):
    from app.service import RuntimeConfigService

    paths = [_make_versioned_file(tmp_path, v) for v in range(1, 8)]
    svc = RuntimeConfigService([paths[0]])
    svc.bootstrap()

    stop = threading.Event()
    bad: list[str] = []

    def reader():
        while not stop.is_set():
            snap = svc.snapshot()
            bundle = snap.to_public_dict()
            # 公开结构也必须整体属于同一版本
            if bundle["version"] != snap.version:
                bad.append("公开结构版本不一致")
            if bundle["config"]["max_connections"] != 100 + snap.version:
                bad.append("公开配置与版本不匹配")

    def writer():
        for p in paths[1:]:
            svc.reload([p])

    with ThreadPoolExecutor(6) as pool:
        rs = [pool.submit(reader) for _ in range(4)]
        pool.submit(writer).result()
        stop.set()
        for f in rs:
            f.result()

    assert not bad
    logger.info("[整体读取] 配置+开关公开结构在并发下始终同版本")
