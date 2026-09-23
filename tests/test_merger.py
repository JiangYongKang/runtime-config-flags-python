"""多来源合并：优先级覆盖、冲突拒绝、完整性与确定性。"""

from __future__ import annotations

import pytest

from runtime.errors import ConfigRejected, ReasonCode
from runtime.merger import merge_sources
from runtime.parser import parse_source

from .conftest import log_case


def src(source_id: str, priority: int, **body) -> dict:
    raw = {"source_id": source_id, "priority": priority}
    raw.update(body)
    return raw


FULL = {
    "settings": {"max_connections": 100, "request_timeout_ms": 3000,
                 "sample_rate": 0.1, "log_level": "INFO"},
}


def test_higher_priority_overrides_deterministically():
    low = src("low", 10, settings={**FULL["settings"], "log_level": "INFO"})
    high = src("high", 50, settings={"log_level": "DEBUG"})
    cfg = merge_sources([parse_source(low), parse_source(high)], version=1)
    assert cfg.settings["log_level"] == "DEBUG"
    assert cfg.settings_provenance["log_level"] == ("high", "low")
    # 合并顺序确定：优先级高在前
    assert cfg.sources == ("high", "low")
    log_case("高优先级确定性覆盖", raws=[low, high], version=cfg.version,
             result={"log_level": cfg.settings["log_level"],
                     "provenance": cfg.settings_provenance["log_level"]})


def test_same_priority_conflict_is_rejected():
    a = src("a", 10, settings={**FULL["settings"], "max_connections": 100})
    b = src("b", 10, settings={"max_connections": 200})
    with pytest.raises(ConfigRejected) as ei:
        merge_sources([parse_source(a), parse_source(b)], version=1)
    codes = {i.code for i in ei.value.issues}
    assert codes == {ReasonCode.SOURCE_CONFLICT}
    log_case("同优先级冲突必须拒绝", raws=[a, b],
             error=[i.explain() for i in ei.value.issues])


def test_flag_scalar_conflict_same_priority():
    a = src("a", 10, settings=FULL["settings"],
            flags={"f": {"enabled": False, "percentage": 10, "overrides": {}}})
    b = src("b", 10,
            flags={"f": {"enabled": True, "percentage": 10, "overrides": {}}})
    with pytest.raises(ConfigRejected) as ei:
        merge_sources([parse_source(a), parse_source(b)], version=1)
    assert any(i.path == "flags.f.enabled" and
               i.code == ReasonCode.SOURCE_CONFLICT
               for i in ei.value.issues)
    log_case("开关基线同优先级冲突", raws=[a, b],
             error=[i.explain() for i in ei.value.issues])


def test_overrides_merge_by_subject_and_conflict():
    a = src("a", 10, settings=FULL["settings"], flags={"f": {
        "enabled": True, "percentage": 0, "overrides": {"u1": True}}})
    b = src("b", 20, flags={"f": {
        "enabled": True, "percentage": 0, "overrides": {"u2": False}}})
    cfg = merge_sources([parse_source(a), parse_source(b)], version=1)
    ov = cfg.flags["f"].override_map()
    assert ov == {"u1": True, "u2": False}
    log_case("不同主体覆盖合并", raws=[a, b], version=1, result=ov)

    c = src("c", 10, flags={"f": {
        "enabled": True, "percentage": 0, "overrides": {"u1": False}}})
    with pytest.raises(ConfigRejected) as ei:
        merge_sources([parse_source(a), parse_source(c)], version=1)
    assert any(i.code == ReasonCode.CONFLICTING_OVERRIDE
               for i in ei.value.issues)
    log_case("同优先级同主体覆盖冲突", raws=[a, c],
             error=[i.explain() for i in ei.value.issues])


def test_higher_priority_override_wins_for_same_subject():
    a = src("a", 10, settings=FULL["settings"], flags={"f": {
        "enabled": True, "percentage": 0, "overrides": {"u1": False}}})
    b = src("b", 20, flags={"f": {
        "enabled": True, "percentage": 0, "overrides": {"u1": True}}})
    cfg = merge_sources([parse_source(a), parse_source(b)], version=1)
    assert cfg.flags["f"].override_map()["u1"] is True
    log_case("高优先级覆盖同一主体", raws=[a, b], version=1,
             result=cfg.flags["f"].override_map())


def test_missing_required_settings_after_merge():
    a = src("a", 10, settings={"max_connections": 1})
    with pytest.raises(ConfigRejected) as ei:
        merge_sources([parse_source(a)], version=1)
    missing_paths = {i.path for i in ei.value.issues
                     if i.code == ReasonCode.MISSING_FIELD}
    assert {"settings.request_timeout_ms", "settings.sample_rate",
            "settings.log_level"} <= missing_paths
    log_case("合并后必填字段缺失", raws=[a],
             error=[i.explain() for i in ei.value.issues])


def test_empty_sources_rejected():
    with pytest.raises(ConfigRejected) as ei:
        merge_sources([], version=1)
    assert ei.value.issues[0].code == ReasonCode.EMPTY_UPDATE
    log_case("无任何来源", error=ei.value.issues[0].explain())


def test_merge_order_is_deterministic():
    raws = [src("z", 10, settings=FULL["settings"]),
            src("a", 50), src("m", 50)]
    cfg1 = merge_sources([parse_source(r) for r in raws], version=1)
    cfg2 = merge_sources([parse_source(r) for r in reversed(raws)], version=1)
    assert cfg1.sources == cfg2.sources == ("a", "m", "z")
    log_case("合并顺序与输入顺序无关（确定性）", raws=raws, version=1,
             result={"sources": list(cfg1.sources)})
