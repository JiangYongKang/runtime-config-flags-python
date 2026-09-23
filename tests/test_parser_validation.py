"""单来源校验：每类无效输入必须返回可区分的 ReasonCode。"""

from __future__ import annotations

import pytest

from runtime.errors import ConfigRejected, ReasonCode
from runtime.parser import parse_source

from .conftest import log_case

BASE = {
    "source_id": "s1",
    "priority": 10,
    "settings": {
        "max_connections": 100,
        "request_timeout_ms": 3000,
        "sample_rate": 0.1,
        "log_level": "INFO",
    },
    "flags": {"f": {"enabled": True, "percentage": 50, "overrides": {}}},
}


def codes_from(raw, **kw):
    try:
        parse_source(raw, **kw)
    except ConfigRejected as exc:
        return {i.code for i in exc.issues}, exc
    return set(), None


def test_valid_source_passes():
    parsed = parse_source(BASE)
    assert parsed.source_id == "s1"
    log_case("合法来源", raws=BASE, version="(单来源，无版本)")


def test_not_a_mapping():
    codes, exc = codes_from(["not", "a", "dict"])
    assert codes == {ReasonCode.NOT_A_MAPPING}
    log_case("整体不是对象", raws=["not", "a", "dict"],
             error=exc.issues[0].explain())


def test_missing_source_id_and_priority():
    raw = {"settings": {}}
    codes, exc = codes_from(raw)
    assert ReasonCode.SOURCE_ID_EMPTY in codes
    assert ReasonCode.MISSING_FIELD in codes
    assert any(i.path == "priority" for i in exc.issues)
    log_case("缺少 source_id / priority 非法", raws=raw,
             error=[i.explain() for i in exc.issues])


def test_wrong_type_settings_field():
    raw = {"source_id": "s", "priority": 1,
           "settings": {"max_connections": "100"}}
    codes, exc = codes_from(raw)
    assert codes == {ReasonCode.WRONG_TYPE}
    log_case("整数字段给字符串", raws=raw, error=exc.issues[0].explain())


def test_bool_cannot_pose_as_int():
    raw = {"source_id": "s", "priority": 1,
           "settings": {"max_connections": True}}
    codes, exc = codes_from(raw)
    assert ReasonCode.WRONG_TYPE in codes
    log_case("bool 冒充 int 被拒", raws=raw, error=exc.issues[0].explain())


@pytest.mark.parametrize("value,lo,hi", [
    (0, 1, 100_000),          # max_connections 下界
    (100_001, 1, 100_000),    # 上界
    (-1, 1, 3_600_000),       # timeout 负
])
def test_setting_out_of_range(value, lo, hi):
    raw = {"source_id": "s", "priority": 1,
           "settings": {"max_connections": value,
                        "request_timeout_ms": 100,
                        "sample_rate": 0.1, "log_level": "INFO"}}
    codes, exc = codes_from(raw)
    assert codes == {ReasonCode.SETTING_OUT_OF_RANGE}
    log_case(f"取值越界 value={value} 允许[{lo},{hi}]", raws=raw,
             error=exc.issues[0].explain())


def test_sample_rate_out_of_range():
    raw = {"source_id": "s", "priority": 1, "settings": {
        "max_connections": 1, "request_timeout_ms": 1,
        "sample_rate": 1.5, "log_level": "INFO"}}
    codes, exc = codes_from(raw)
    assert codes == {ReasonCode.SETTING_OUT_OF_RANGE}
    log_case("sample_rate=1.5 越界", raws=raw, error=exc.issues[0].explain())


def test_bad_enum():
    raw = {"source_id": "s", "priority": 1, "settings": {
        "max_connections": 1, "request_timeout_ms": 1,
        "sample_rate": 0.0, "log_level": "VERBOSE"}}
    codes, exc = codes_from(raw)
    assert codes == {ReasonCode.BAD_ENUM}
    log_case("log_level 非法枚举", raws=raw, error=exc.issues[0].explain())


def test_unknown_setting_field_rejected():
    raw = {"source_id": "s", "priority": 1, "settings": {"mystery": 1}}
    codes, exc = codes_from(raw)
    # 未知字段按 WRONG_TYPE 类结构错误拒绝，且还缺必填字段（合并阶段再补查）
    assert ReasonCode.WRONG_TYPE in codes
    log_case("未知 settings 字段", raws=raw, error=exc.issues[0].explain())


def test_percentage_missing_and_wrong_type():
    raw_missing = {"source_id": "s", "priority": 1,
                   "flags": {"f": {"enabled": True}}}
    codes, exc = codes_from(raw_missing)
    assert ReasonCode.MISSING_FIELD in codes
    log_case("percentage 缺失", raws=raw_missing,
             error=[i.explain() for i in exc.issues])

    raw_type = {"source_id": "s", "priority": 1,
                "flags": {"f": {"enabled": True, "percentage": "50"}}}
    codes, exc = codes_from(raw_type)
    assert ReasonCode.WRONG_TYPE in codes
    log_case("percentage 类型错", raws=raw_type,
             error=[i.explain() for i in exc.issues])


@pytest.mark.parametrize("pct", [-1, 101, 1000])
def test_percentage_out_of_range(pct):
    raw = {"source_id": "s", "priority": 1,
           "flags": {"f": {"enabled": True, "percentage": pct}}}
    codes, exc = codes_from(raw)
    assert ReasonCode.PERCENTAGE_OUT_OF_RANGE in codes
    log_case(f"非法灰度比例 {pct}", raws=raw, error=exc.issues[0].explain())


def test_flag_not_mapping_and_overrides_invalid():
    raw1 = {"source_id": "s", "priority": 1, "flags": {"f": True}}
    codes, exc = codes_from(raw1)
    assert ReasonCode.FLAG_NOT_A_MAPPING in codes
    log_case("开关定义不是对象", raws=raw1, error=exc.issues[0].explain())

    raw2 = {"source_id": "s", "priority": 1,
            "flags": {"f": {"enabled": True, "percentage": 10,
                            "overrides": {"u1": "yes"}}}}
    codes, exc = codes_from(raw2)
    assert ReasonCode.OVERRIDE_VALUE_INVALID in codes
    log_case("覆盖值非布尔", raws=raw2, error=exc.issues[0].explain())


def test_multiple_issues_collected_together():
    raw = {"source_id": "", "priority": -5, "settings": {
        "max_connections": "bad", "sample_rate": 9}}
    _, exc = codes_from(raw)
    codes = {i.code for i in exc.issues}
    assert {
        ReasonCode.SOURCE_ID_EMPTY,
        ReasonCode.SOURCE_PRIORITY_INVALID,
        ReasonCode.WRONG_TYPE,
        ReasonCode.SETTING_OUT_OF_RANGE,
    } <= codes
    log_case("一次收集多条可区分原因", raws=raw,
             error=[i.explain() for i in exc.issues])
