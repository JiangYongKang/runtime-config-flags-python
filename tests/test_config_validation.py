"""配置校验：缺失、类型不符、越界、来源冲突、未知字段，原因必须可区分。"""

from __future__ import annotations

import logging

import pytest

from app.config import MISSING, build_snapshot
from app.errors import ConfigError, ErrorCode
from app.models import FieldSpec, FieldType

logger = logging.getLogger("runtime_config.tests")


def _expect_error(payloads, tmp_path, code, *, specs=None):
    paths = []
    for i, p in enumerate(payloads):
        path = tmp_path / f"s{i}.json"
        path.write_text(__import__("json").dumps(p), encoding="utf-8")
        paths.append(str(path))
    logger.info("[测试输入配置] %s", payloads)
    with pytest.raises(ConfigError) as exc_info:
        if specs is None:
            build_snapshot(paths, version=1)
        else:
            build_snapshot(paths, field_specs=specs, version=1)
    err = exc_info.value
    logger.info("[拒绝原因] code=%s detail=%s", err.code.value, err.to_dict())
    assert err.code is code
    return err


def test_missing_required_field_rejected(tmp_path):
    err = _expect_error(
        [{}],
        tmp_path,
        ErrorCode.MISSING_FIELD,
        specs=(FieldSpec("tenant", FieldType.STR, MISSING),),
    )
    assert err.path == "config.tenant"


def test_default_fills_missing_optional(make_service):
    svc = make_service([{"config": {"timeout_ms": 100}}])
    assert svc.get("timeout_ms") == 100
    assert svc.get("max_connections") == 100  # 默认值
    logger.info("[默认值生效] version=%d max_connections=%s", svc.version(), svc.get("max_connections"))


@pytest.mark.parametrize(
    "bad_value,expected_code",
    [
        ("3000", ErrorCode.TYPE_MISMATCH),   # int 收到字符串
        (True, ErrorCode.TYPE_MISMATCH),     # bool 不得当作 int
        (1.5, ErrorCode.TYPE_MISMATCH),      # int 收到 float
        (-1, ErrorCode.VALUE_OUT_OF_RANGE),
        (60001, ErrorCode.VALUE_OUT_OF_RANGE),
    ],
)
def test_timeout_validation(tmp_path, bad_value, expected_code):
    _expect_error(
        [{"config": {"timeout_ms": bad_value}}], tmp_path, expected_code
    )


def test_float_field_rejects_bool_and_string(tmp_path):
    _expect_error([{"config": {"sample_rate": True}}], tmp_path, ErrorCode.TYPE_MISMATCH)
    _expect_error([{"config": {"sample_rate": "0.5"}}], tmp_path, ErrorCode.TYPE_MISMATCH)


def test_float_range_bounds(tmp_path):
    _expect_error([{"config": {"sample_rate": 1.01}}], tmp_path, ErrorCode.VALUE_OUT_OF_RANGE)
    _expect_error([{"config": {"sample_rate": -0.01}}], tmp_path, ErrorCode.VALUE_OUT_OF_RANGE)


def test_bool_field_rejects_int(tmp_path):
    _expect_error([{"config": {"enable_cache": 1}}], tmp_path, ErrorCode.TYPE_MISMATCH)


def test_unknown_config_field_rejected(tmp_path):
    _expect_error([{"config": {"unregistered": 1}}], tmp_path, ErrorCode.UNKNOWN_FIELD)


def test_unknown_top_level_section_rejected(tmp_path):
    _expect_error([{"unknown_section": {}}], tmp_path, ErrorCode.UNKNOWN_FIELD)


def test_cross_source_type_conflict_rejected_even_when_overridden(tmp_path):
    # 低优先级给字符串、高优先级给合法 int：仍判定为来源冲突而非静默覆盖
    err = _expect_error(
        [
            {"config": {"timeout_ms": "1000"}},
            {"config": {"timeout_ms": 2000}},
        ],
        tmp_path,
        ErrorCode.SOURCE_CONFLICT,
    )
    assert err.source.endswith("s0.json")


def test_valid_override_is_recorded_in_reasons(make_service):
    svc = make_service(
        [
            {"config": {"timeout_ms": 1000, "log_level": "info"}},
            {"config": {"timeout_ms": 2000}},
        ]
    )
    assert svc.get("timeout_ms") == 2000
    reasons = svc.snapshot().reasons
    assert any("timeout_ms" in r and "覆盖" in r for r in reasons)
    logger.info("[覆盖依据] %s", [r for r in reasons if "timeout_ms" in r])


def test_source_not_found(tmp_path):
    missing = tmp_path / "nope.json"
    logger.info("[测试输入配置] 缺失来源 %s", missing)
    with pytest.raises(ConfigError) as exc_info:
        build_snapshot([str(missing)], version=1)
    assert exc_info.value.code is ErrorCode.SOURCE_NOT_FOUND


def test_source_parse_error(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        build_snapshot([str(path)], version=1)
    assert exc_info.value.code is ErrorCode.SOURCE_PARSE_ERROR


def test_source_top_level_must_be_object(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        build_snapshot([str(path)], version=1)
    assert exc_info.value.code is ErrorCode.SOURCE_NOT_MAPPING
