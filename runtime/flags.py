"""特性开关的稳定判定（百分比灰度 + 定向覆盖）。

判定优先级（对 flag=f、subject=s）：
1. 定向覆盖 overrides[s] 存在 => 直接采用，依据 override:<source>
   （覆盖甚至优先于总闸，便于应急强制打开/关闭指定主体）；
2. enabled 为 False => 关（总闸/熔断开关关闭，百分比不参与判定），
   依据 baseline_disabled；
3. enabled 为 True 时按灰度比例：
   - percentage == 0   => 关；
   - percentage == 100 => 开；
   - 0 < p < 100       => bucket(flag, subject) < p 则开。
   bucket = sha256(f"flag-eval|{flag}|{subject}") 前 8 位十六进制 mod 100。
   - flag 参与哈希 => 不同开关对同一主体是相互独立的分桶，开关互不影响；
   - sha256 纯函数 => 同一 (flag, subject) 在配置不变时结果跨进程/重复
     调用完全一致，稳定可复现；
   - 边界确定：bucket 取值 0..99，p=0 永不开；p=100 恒开。
未知开关 => ConfigRejected(UNKNOWN_FLAG)。
"""

from __future__ import annotations

import hashlib

from runtime.errors import ConfigIssue, ConfigRejected, ReasonCode
from runtime.model import EvaluationResult, FlagRule, MergedConfig


def stable_bucket(flag: str, subject: str) -> int:
    """返回 0..99 的稳定分桶；flag 参与哈希，开关之间互不影响。"""
    digest = hashlib.sha256(
        f"flag-eval|{flag}|{subject}".encode("utf-8")
    ).hexdigest()
    return int(digest[:8], 16) % 100


def evaluate(config: MergedConfig, flag: str, subject: str) -> EvaluationResult:
    """对某主体判定某开关；未知开关抛 ConfigRejected(UNKNOWN_FLAG)。"""
    rule = config.flags.get(flag)
    if rule is None:
        raise ConfigRejected([ConfigIssue(
            ReasonCode.UNKNOWN_FLAG, f"flags.{flag}",
            f"未知开关 {flag!r}：当前版本 v{config.version} 中不存在该开关，"
            f"已知开关：{sorted(config.flags)}；判定结论按“关”之外的显式错误"
            f"处理，不做静默兜底")])

    if not isinstance(subject, str) or not subject.strip():
        raise ConfigRejected([ConfigIssue(
            ReasonCode.OVERRIDE_SUBJECT_EMPTY, f"flags.{flag}.subject",
            "判定主体必须是非空字符串")])
    subject = subject.strip()

    on, reason, detail = _decide(rule, flag, subject)
    return EvaluationResult(
        flag=flag,
        subject=subject,
        on=on,
        version=config.version,
        reason=reason,
        bucket=stable_bucket(flag, subject),
        percentage=rule.percentage,
        detail=detail,
    )


def _decide(rule: FlagRule, flag: str, subject: str
            ) -> tuple[bool, str, str]:
    bucket = stable_bucket(flag, subject)
    overrides = rule.override_map()
    provenance = rule.provenance_map()

    if subject in overrides:
        on = overrides[subject]
        src = provenance.get(subject, "?")
        reason = "override"
        detail = (
            f"主体命中来源 {src!r} 的定向覆盖 => {'开' if on else '关'}，"
            f"覆盖优先于总闸与灰度（bucket={bucket} 仅记录不参与判定）")
        return on, reason, detail

    if not rule.enabled:
        reason = "baseline_disabled"
        detail = (
            f"无定向覆盖；总闸 enabled=False，灰度({rule.percentage}%)不参与"
            f"判定 => 关（bucket={bucket} 仅记录）")
        return False, reason, detail

    if rule.percentage == 0:
        reason = "percentage_zero"
        detail = "总闸开启且灰度比例=0%，任何主体都不进入灰度 => 关"
        return False, reason, detail

    if rule.percentage == 100:
        reason = "percentage_full"
        detail = "总闸开启且灰度比例=100%，所有未显式覆盖的主体 => 开"
        return True, reason, detail

    on = bucket < rule.percentage
    reason = "percentage_rollout"
    detail = (
        f"无定向覆盖；总闸 enabled=True；稳定分桶 "
        f"bucket({flag!r},{subject!r})={bucket}，灰度比例="
        f"{rule.percentage}%，命中条件 bucket<{rule.percentage}"
        f" => {'开' if on else '关'}")
    return on, reason, detail
