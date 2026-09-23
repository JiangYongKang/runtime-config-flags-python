"""热更新：无效更新回滚（状态不变、原因可追溯）、CAS 版本冲突、版本单调。"""

from __future__ import annotations

import json
import logging

import pytest

from app.errors import ConfigError, ErrorCode

logger = logging.getLogger("runtime_config.tests")


VALID = {
    "config": {"timeout_ms": 1000, "max_connections": 100},
    "features": {"f": {"percentage": 10}},
}
VALID2 = {
    "config": {"timeout_ms": 2000, "max_connections": 100},
    "features": {"f": {"percentage": 20}},
}


def _write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    logger.info("[测试输入配置] %s <- %s", name, payload)
    return str(path)


def test_invalid_reload_keeps_current_state(tmp_path):
    p1 = _write(tmp_path, "v1.json", VALID)
    svc = __import__("app.service", fromlist=["RuntimeConfigService"]).RuntimeConfigService([p1])
    snap1 = svc.bootstrap()
    assert snap1.version == 1

    p_bad = _write(tmp_path, "bad.json", {"config": {"timeout_ms": 999999}})
    with pytest.raises(ConfigError) as exc_info:
        svc.reload([p_bad])
    err = exc_info.value
    logger.info("[无效更新被拒绝] code=%s detail=%s", err.code.value, err.to_dict())
    assert err.code is ErrorCode.VALUE_OUT_OF_RANGE

    # 当前生效状态必须完全不变
    assert svc.version() == 1
    assert svc.get("timeout_ms") == 1000
    assert svc.evaluate_flag("f", "u").percentage == 10
    logger.info("[回滚确认] 仍为版本=%d timeout_ms=%s", svc.version(), svc.get("timeout_ms"))


def test_reload_with_missing_source_rolls_back(tmp_path):
    p1 = _write(tmp_path, "v1.json", VALID)
    from app.service import RuntimeConfigService

    svc = RuntimeConfigService([p1])
    svc.bootstrap()
    with pytest.raises(ConfigError) as exc_info:
        svc.reload([str(tmp_path / "ghost.json")])
    assert exc_info.value.code is ErrorCode.SOURCE_NOT_FOUND
    assert svc.version() == 1


def test_valid_reload_publishes_new_version(tmp_path):
    p1 = _write(tmp_path, "v1.json", VALID)
    p2 = _write(tmp_path, "v2.json", VALID2)
    from app.service import RuntimeConfigService

    svc = RuntimeConfigService([p1])
    svc.bootstrap()
    snap2 = svc.reload([p2])
    assert snap2.version == 2
    assert svc.version() == 2
    assert svc.get("timeout_ms") == 2000
    assert svc.evaluate_flag("f", "u").percentage == 20
    logger.info("[热更成功] 新版本=%d", snap2.version)


def test_cas_stale_version_conflict(tmp_path):
    p1 = _write(tmp_path, "v1.json", VALID)
    p2 = _write(tmp_path, "v2.json", VALID2)
    from app.service import RuntimeConfigService

    svc = RuntimeConfigService([p1])
    svc.bootstrap()
    svc.reload([p2])  # 版本推进到 2
    with pytest.raises(ConfigError) as exc_info:
        svc.reload([p2], expected_version=1)
    assert exc_info.value.code is ErrorCode.VERSION_CONFLICT
    assert svc.version() == 2
    logger.info("[CAS 冲突] %s", exc_info.value.to_dict())


def test_strict_bootstrap_refuses_invalid_config(tmp_path):
    p_bad = _write(tmp_path, "bad.json", {"config": {"timeout_ms": "no"}})
    from app.service import RuntimeConfigService

    svc = RuntimeConfigService([p_bad], strict=True)
    with pytest.raises(ConfigError) as exc_info:
        svc.bootstrap()
    assert exc_info.value.code is ErrorCode.TYPE_MISMATCH


def test_non_strict_bootstrap_is_deterministic_no_snapshot(tmp_path):
    from app.service import RuntimeConfigService

    svc = RuntimeConfigService([str(tmp_path / "missing.json")], strict=False)
    assert svc.bootstrap() is None
    for action in (svc.version, lambda: svc.get("timeout_ms"), lambda: svc.flag_on("f", "u")):
        with pytest.raises(ConfigError) as exc_info:
            action()
        assert exc_info.value.code is ErrorCode.NO_SNAPSHOT
    logger.info("[非严格模式] 无快照时所有读取确定性返回 no_snapshot")


def test_snapshot_is_immutable_publication_unit(tmp_path):
    p1 = _write(tmp_path, "v1.json", VALID)
    from app.service import RuntimeConfigService

    svc = RuntimeConfigService([p1])
    svc.bootstrap()
    snap = svc.snapshot()
    with pytest.raises(Exception):
        snap.values["timeout_ms"] = 1  # type: ignore[misc]
    with pytest.raises(Exception):
        snap.version = 99  # type: ignore[misc]
