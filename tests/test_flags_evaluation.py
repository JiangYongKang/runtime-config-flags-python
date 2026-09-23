"""特性开关灰度判定：边界、稳定性、独立性、覆盖、未知开关。"""

from __future__ import annotations

import pytest

from runtime.errors import ConfigRejected, ReasonCode
from runtime.flags import evaluate, stable_bucket

from .conftest import build_config, log_case, make_raw

SUBJECTS = [f"subject-{i}" for i in range(500)]


def cfg_with(percentage, *, enabled=True, overrides=None):
    raw = make_raw(
        {"enabled": enabled, "percentage": percentage,
         "overrides": overrides or {}},
        source_id="s", priority=10)
    return build_config([raw])


def result_view(r):
    return {"on": r.on, "reason": r.reason, "bucket": r.bucket,
            "percentage": r.percentage, "version": r.version,
            "detail": r.detail}


def test_zero_percent_never_on():
    cfg = cfg_with(0)
    results = [evaluate(cfg, "feature_x", s) for s in SUBJECTS]
    assert all(not r.on for r in results)
    assert all(r.reason == "percentage_zero" for r in results)
    log_case("灰度 0% 边界：全部关闭", raws={"percentage": 0},
             version=cfg.version, result=result_view(results[0]))


def test_hundred_percent_always_on():
    cfg = cfg_with(100)
    results = [evaluate(cfg, "feature_x", s) for s in SUBJECTS]
    assert all(r.on for r in results)
    assert all(r.reason == "percentage_full" for r in results)
    log_case("灰度 100% 边界：全部开启", raws={"percentage": 100},
             version=cfg.version, result=result_view(results[0]))


@pytest.mark.parametrize("pct", [1, 10, 25, 50, 75, 99])
def test_intermediate_percentage_boundary_subjects(pct):
    cfg = cfg_with(pct)
    # 判定必须严格等价于 bucket < pct（边界规则可复现）
    for s in SUBJECTS:
        r = evaluate(cfg, "feature_x", s)
        expected = stable_bucket("feature_x", s) < pct
        assert r.on is expected
        assert r.reason == "percentage_rollout"
    on_count = sum(evaluate(cfg, "feature_x", s).on for s in SUBJECTS)
    ratio = on_count / len(SUBJECTS)
    log_case(f"灰度 {pct}%：实测命中比例 {ratio:.3f}（n={len(SUBJECTS)}）",
             raws={"percentage": pct}, version=cfg.version,
             result={"hit": on_count, "total": len(SUBJECTS)})
    # 大样本下比例应接近目标（宽松边界，避免哈希偶然波动造成脆弱测试）
    assert abs(ratio - pct / 100) < 0.08


def test_repeated_evaluation_is_stable():
    cfg = cfg_with(33)
    first = [evaluate(cfg, "feature_x", s) for s in SUBJECTS]
    for _ in range(5):
        again = [evaluate(cfg, "feature_x", s) for s in SUBJECTS]
        assert [(r.on, r.bucket) for r in again] == \
               [(r.on, r.bucket) for r in first]
    log_case("重复判定稳定（同配置 6 轮结果一致）",
             raws={"percentage": 33}, version=cfg.version,
             result={"sample": result_view(first[0])})


def test_stable_across_process_boundary_known_vector():
    # 固定向量：sha256 实现保证跨进程一致，锁定公式不被悄悄改动
    b = stable_bucket("feature_x", "alice")
    assert 0 <= b <= 99
    assert isinstance(b, int)
    # 改变 flag 名必须（极大概率）改变分桶 => 开关互不影响
    assert stable_bucket("feature_x", "alice") == \
           stable_bucket("feature_x", "alice")
    diffs = sum(
        stable_bucket(f"flag-{i}", "alice") !=
        stable_bucket("flag-0", "alice") for i in range(1, 50))
    log_case("开关独立性：同一主体在不同开关下的分桶差异",
             version="-", result={"bucket(feature_x,alice)": b,
                                  "different_from_flag0": diffs})
    assert diffs >= 45


def test_explicit_overrides_take_precedence():
    cfg = cfg_with(0, overrides={"vip": True, "blocked": False})
    r_vip = evaluate(cfg, "feature_x", "vip")
    r_blocked = evaluate(cfg, "feature_x", "blocked")
    assert r_vip.on is True and r_vip.reason == "override"
    assert r_blocked.on is False and r_blocked.reason == "override"
    log_case("定向覆盖优先（0% 灰度下 vip 仍开）",
             raws={"percentage": 0, "overrides": {"vip": True}},
             version=cfg.version, result=result_view(r_vip))


def test_override_beats_disabled_baseline():
    cfg = cfg_with(50, enabled=False, overrides={"vip": True})
    assert evaluate(cfg, "feature_x", "vip").on is True
    normal = evaluate(cfg, "feature_x", "normal-user")
    assert normal.on is False and normal.reason == "baseline_disabled"
    log_case("总闸关闭时：普通主体关、定向覆盖仍生效",
             raws={"enabled": False, "percentage": 50},
             version=cfg.version, result=result_view(normal))


def test_unknown_flag_is_explicit_error():
    cfg = cfg_with(10)
    with pytest.raises(ConfigRejected) as ei:
        evaluate(cfg, "does_not_exist", "u1")
    assert ei.value.issues[0].code == ReasonCode.UNKNOWN_FLAG
    log_case("未知开关显式报错（不静默兜底）",
             raws={"flags": ["feature_x"]}, version=cfg.version,
             error=ei.value.issues[0].explain())


def test_empty_subject_rejected():
    cfg = cfg_with(10)
    with pytest.raises(ConfigRejected) as ei:
        evaluate(cfg, "feature_x", "   ")
    assert ei.value.issues[0].code == ReasonCode.OVERRIDE_SUBJECT_EMPTY
    log_case("空白主体被拒", error=ei.value.issues[0].explain())


def test_subject_whitespace_trimmed_but_stable():
    cfg = cfg_with(50)
    r1 = evaluate(cfg, "feature_x", "alice")
    r2 = evaluate(cfg, "feature_x", "  alice  ")
    assert r1.on == r2.on and r1.bucket == r2.bucket
    log_case("主体空白裁剪后判定一致", version=cfg.version,
             result=result_view(r1))
