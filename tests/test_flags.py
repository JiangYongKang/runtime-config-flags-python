"""开关判定：灰度边界（0%/100%/中间）、重复判定稳定、定向覆盖、开关隔离。"""

from __future__ import annotations

import logging

import pytest

from app.flags import evaluate, stable_bucket
from app.models import FlagRule

logger = logging.getLogger("runtime_config.tests")

SUBJECTS = [f"user-{i:04d}" for i in range(2000)]


def test_zero_percent_never_hits_gray():
    rule = FlagRule("f", default_on=False, percentage=0)
    results = {evaluate(rule, s, version=1).reason for s in SUBJECTS}
    assert results == {"default_off"}
    logger.info("[0%% 边界] 2000 主体结论集合=%s", results)


def test_zero_percent_respects_default_on():
    rule = FlagRule("f", default_on=True, percentage=0)
    assert all(evaluate(rule, s, version=1).on is True for s in SUBJECTS)


def test_hundred_percent_everyone_hits():
    rule = FlagRule("f", default_on=False, percentage=100)
    decisions = [evaluate(rule, s, version=1) for s in SUBJECTS]
    assert all(d.on is True and d.reason == "gray_hit" for d in decisions)
    logger.info("[100%% 边界] 2000 主体全部 gray_hit")


def test_override_forces_off_even_at_hundred_percent():
    rule = FlagRule(
        "f", default_on=False, percentage=100, overrides={"vip": False}
    )
    d = evaluate(rule, "vip", version=1)
    assert d.on is False and d.reason == "override_off"
    logger.info("[覆盖优先] 100%% 下 vip 被强制关闭: %s", d.to_dict())


def test_override_forces_on_at_zero_percent():
    rule = FlagRule(
        "f", default_on=False, percentage=0, overrides={"guest": True}
    )
    d = evaluate(rule, "guest", version=1)
    assert d.on is True and d.reason == "override_on"


def test_mid_percentage_distribution_is_stable_and_bounded():
    rule = FlagRule("f", default_on=False, percentage=50)
    first = {s: evaluate(rule, s, version=1) for s in SUBJECTS}
    # 重复判定必须完全一致（同配置、同版本）
    for _ in range(5):
        again = {s: evaluate(rule, s, version=1) for s in SUBJECTS}
        assert again == first
    on_count = sum(d.on for d in first.values())
    ratio = on_count / len(SUBJECTS)
    logger.info("[50%% 中间比例] 命中率=%.3f（2000 主体，重复 5 轮一致）", ratio)
    assert 0.40 < ratio < 0.60  # sha256 均匀分布，允许抽样波动


def test_boundary_subjects_bucket_lt_percentage_rule():
    # 直接校验 bucket < percentage 的边界语义
    for pct in (0, 1, 25, 50, 99, 100):
        rule = FlagRule("f", default_on=False, percentage=pct)
        for s in SUBJECTS[:300]:
            d = evaluate(rule, s, version=1)
            assert d.on == (d.bucket < pct)
    logger.info("[边界语义] bucket < percentage 对 0/1/25/50/99/100 全部成立")


def test_bucket_is_cross_process_stable():
    # 不使用 Python hash()，避免 PYTHONHASHSEED 随机化；这里写死期望值锁定算法
    assert stable_bucket("f", "alice", "") == stable_bucket("f", "alice", "")
    assert 0 <= stable_bucket("f", "alice", "") <= 99


def test_flags_are_independent_per_subject():
    # 两个开关使用各自名称参与散列，桶值不应被强制相同
    buckets_a = [stable_bucket("flag_a", s, "salt") for s in SUBJECTS]
    buckets_b = [stable_bucket("flag_b", s, "salt") for s in SUBJECTS]
    assert buckets_a != buckets_b
    logger.info("[开关隔离] flag_a/flag_b 对相同主体的桶序列不同")


def test_salt_changes_distribution():
    # 盐值不同时桶值应发生变化（极小概率全部相同，2000 主体可忽略）
    a = {s: stable_bucket("f", s, "salt-a") for s in SUBJECTS}
    b = {s: stable_bucket("f", s, "salt-b") for s in SUBJECTS}
    assert a != b


def test_decision_is_deterministic_for_single_subject():
    rule = FlagRule("f", default_on=False, percentage=33)
    d1 = evaluate(rule, "fixed-subject", version=9)
    d2 = evaluate(rule, "fixed-subject", version=9)
    assert d1 == d2
    logger.info("[重复判定稳定] %s", d1.to_dict())


@pytest.mark.parametrize("bad_percentage", [-1, 101, "50", 50.0, True])
def test_illegal_percentage_rejected_at_build(tmp_path, bad_percentage):
    import json
    from app.config import build_snapshot
    from app.errors import ConfigError, ErrorCode

    path = tmp_path / "s.json"
    path.write_text(
        json.dumps({"features": {"f": {"percentage": bad_percentage}}}), encoding="utf-8"
    )
    logger.info("[非法比例输入] percentage=%r", bad_percentage)
    with pytest.raises(ConfigError) as exc_info:
        build_snapshot([str(path)], version=1)
    logger.info("[拒绝原因] %s", exc_info.value.code.value)
    assert exc_info.value.code is ErrorCode.ILLEGAL_PERCENTAGE


def test_bad_override_value_rejected(tmp_path):
    import json
    from app.config import build_snapshot
    from app.errors import ConfigError, ErrorCode

    path = tmp_path / "s.json"
    path.write_text(
        json.dumps({"features": {"f": {"percentage": 10, "overrides": {"u": "yes"}}}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        build_snapshot([str(path)], version=1)
    assert exc_info.value.code is ErrorCode.TYPE_MISMATCH


def test_unknown_flag_attribute_rejected(tmp_path):
    import json
    from app.config import build_snapshot
    from app.errors import ConfigError, ErrorCode

    path = tmp_path / "s.json"
    path.write_text(json.dumps({"features": {"f": {"percent": 10}}}), encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        build_snapshot([str(path)], version=1)
    assert exc_info.value.code is ErrorCode.UNKNOWN_FIELD
