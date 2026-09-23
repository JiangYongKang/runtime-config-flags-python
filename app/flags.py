"""特性开关判定：默认值 / 灰度百分比 / 定向覆盖。

判定规则（按优先级）：
  1. 定向覆盖（overrides 中存在该主体）→ 采用覆盖值（reason=override_on/off）；
  2. 否则按稳定散列判定是否落入灰度百分比：
       bucket = hash(flag 名 + salt + subject) mod 100，取值 0..99；
       bucket < percentage 视为命中（reason=gray_hit / gray_miss）；
  3. percentage 为 0/100 时分别对应全关/全开，但仍可被覆盖翻转。
  4. 未进入灰度时，使用开关默认值（reason=default_on/default_off）。

确定性保证：
  - 使用 sha256（无随机盐、不受进程内 hash 随机化影响），同一 (flag, salt,
    subject) 桶值跨进程、跨时间稳定；
  - 开关名参与散列，两个开关对同一主体的桶值相互独立、互不影响。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.models import FlagRule

# 判定结论类型
Decision = str


@dataclass(frozen=True)
class FlagDecision:
    """一次开关判定的完整依据（可复现、可解释）。"""

    flag: str
    subject: str
    on: bool
    reason: Decision
    percentage: int
    bucket: int
    version: int

    def to_dict(self) -> dict:
        return {
            "flag": self.flag,
            "subject": self.subject,
            "on": self.on,
            "reason": self.reason,
            "percentage": self.percentage,
            "bucket": self.bucket,
            "version": self.version,
        }


def stable_bucket(flag: str, subject: str, salt: str) -> int:
    """计算主体在该开关上的稳定桶值，范围 [0, 99]。

    输入以 NUL 分隔，避免名字拼接歧义；盐值参与散列，允许运维在需要时
    重新分布灰度人群。
    """
    payload = "\x00".join([flag, salt, subject]).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    # 取前 8 字节做大端无符号整数后 mod 100
    num = int.from_bytes(digest[:8], "big", signed=False)
    return num % 100


def evaluate(rule: FlagRule, subject: str, *, version: int) -> FlagDecision:
    """对单个开关规则做判定，返回带依据的结论。"""
    bucket = stable_bucket(rule.name, subject, rule.salt)

    if subject in rule.overrides:
        on = rule.overrides[subject]
        reason = "override_on" if on else "override_off"
        return FlagDecision(
            flag=rule.name,
            subject=subject,
            on=on,
            reason=reason,
            percentage=rule.percentage,
            bucket=bucket,
            version=version,
        )

    in_rollout = bucket < rule.percentage
    if in_rollout:
        return FlagDecision(
            flag=rule.name,
            subject=subject,
            on=True,
            reason="gray_hit",
            percentage=rule.percentage,
            bucket=bucket,
            version=version,
        )

    if rule.percentage > 0:
        # 配了灰度但本主体未命中：结果为关，并说明是灰度未命中
        return FlagDecision(
            flag=rule.name,
            subject=subject,
            on=False,
            reason="gray_miss",
            percentage=rule.percentage,
            bucket=bucket,
            version=version,
        )

    return FlagDecision(
        flag=rule.name,
        subject=subject,
        on=rule.default_on,
        reason="default_on" if rule.default_on else "default_off",
        percentage=rule.percentage,
        bucket=bucket,
        version=version,
    )
