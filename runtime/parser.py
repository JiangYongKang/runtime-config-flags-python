"""把单个来源的原始 JSON/dict 解析并校验为 SourceConfig。

设计原则：
- 尽量累积该来源内的全部问题，一次性返回，便于定位；
- 每个问题都有独立的 ReasonCode，绝不静默采用非法值；
- 布尔是 int 的子类，校验时显式排除 bool 冒充整数的情况。
"""

from __future__ import annotations

from typing import Any

from runtime.errors import ConfigIssue, ConfigRejected, ReasonCode
from runtime.model import SETTING_ENUMS, SETTING_SPEC, FlagRule, SourceConfig


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return (isinstance(v, float) or _is_int(v)) and not isinstance(v, bool)


def parse_source(raw: Any, *, source_id: str = "",
                 priority: int | None = None) -> SourceConfig:
    """校验单个来源配置，非法时抛出 ConfigRejected（可能含多条 ConfigIssue）。

    raw 形如：
      {"source_id": "defaults", "priority": 10,
       "settings": {...}, "flags": {...}}
    source_id/priority 也可由参数显式给出（raw 内字段优先于参数）；
    两者都没有提供时按字段缺失处理，绝不静默采用默认值。
    """
    issues: list[ConfigIssue] = []

    if not isinstance(raw, dict):
        raise ConfigRejected([
            ConfigIssue(ReasonCode.NOT_A_MAPPING, "$",
                        f"来源配置必须是对象，实际为 {type(raw).__name__}")
        ])

    sid = raw.get("source_id", source_id)
    if not isinstance(sid, str) or not sid.strip():
        issues.append(ConfigIssue(
            ReasonCode.SOURCE_ID_EMPTY, "source_id",
            "来源 id 必须是非空字符串", source=source_id or None))
        sid = source_id or "<invalid>"
    else:
        sid = sid.strip()

    if "priority" in raw:
        prio = raw["priority"]
    elif priority is not None:
        prio = priority
    else:
        prio = None
        issues.append(ConfigIssue(
            ReasonCode.MISSING_FIELD, "priority",
            "来源缺少必填字段 priority（非负整数）", source=sid))
    if prio is not None and (not _is_int(prio) or prio < 0):
        issues.append(ConfigIssue(
            ReasonCode.SOURCE_PRIORITY_INVALID, "priority",
            f"priority 必须是非负整数，实际为 {prio!r}", source=sid))
        prio = 0

    settings = _parse_settings(raw.get("settings"), sid, issues)
    flags = _parse_flags(raw.get("flags"), sid, issues)

    if issues:
        raise ConfigRejected(issues)

    return SourceConfig(
        source_id=sid,
        priority=prio,
        settings=frozenset(settings.items()),
        flags=frozenset(flags.items()),
    )


def _parse_settings(raw: Any, sid: str,
                    issues: list[ConfigIssue]) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        issues.append(ConfigIssue(
            ReasonCode.WRONG_TYPE, "settings",
            f"settings 必须是对象，实际为 {type(raw).__name__}", source=sid))
        return {}

    out: dict[str, Any] = {}
    for key, value in raw.items():
        path = f"settings.{key}"
        if key in SETTING_SPEC:
            expected, lo, hi = SETTING_SPEC[key]
            if expected is int:
                ok = _is_int(value)
            else:  # float 字段接受 int/float，但拒绝 bool
                ok = _is_number(value)
            if not ok:
                issues.append(ConfigIssue(
                    ReasonCode.WRONG_TYPE, path,
                    f"期望类型 {expected.__name__}，实际为 "
                    f"{type(value).__name__}={value!r}", source=sid))
                continue
            if not (lo <= value <= hi):
                issues.append(ConfigIssue(
                    ReasonCode.SETTING_OUT_OF_RANGE, path,
                    f"取值 {value!r} 越界，允许区间 [{lo}, {hi}]",
                    source=sid))
                continue
            out[key] = value
        elif key in SETTING_ENUMS:
            allowed = SETTING_ENUMS[key]
            if not isinstance(value, str):
                issues.append(ConfigIssue(
                    ReasonCode.WRONG_TYPE, path,
                    f"期望字符串枚举，实际为 {type(value).__name__}",
                    source=sid))
            elif value not in allowed:
                issues.append(ConfigIssue(
                    ReasonCode.BAD_ENUM, path,
                    f"枚举值 {value!r} 非法，允许 {allowed}", source=sid))
            else:
                out[key] = value
        else:
            issues.append(ConfigIssue(
                ReasonCode.WRONG_TYPE, path,
                f"未知 settings 字段 {key!r}，合法字段："
                f"{tuple(SETTING_SPEC) + tuple(SETTING_ENUMS)}", source=sid))
    return out


def _parse_flags(raw: Any, sid: str,
                 issues: list[ConfigIssue]) -> dict[str, FlagRule]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        issues.append(ConfigIssue(
            ReasonCode.FLAGS_NOT_A_MAPPING, "flags",
            f"flags 必须是对象，实际为 {type(raw).__name__}", source=sid))
        return {}

    out: dict[str, FlagRule] = {}
    for name, body in raw.items():
        base = f"flags.{name}"
        if not isinstance(name, str) or not name.strip():
            issues.append(ConfigIssue(
                ReasonCode.WRONG_TYPE, "flags.<key>",
                "开关名必须是非空字符串", source=sid))
            continue
        if not isinstance(body, dict):
            issues.append(ConfigIssue(
                ReasonCode.FLAG_NOT_A_MAPPING, base,
                f"开关定义必须是对象，实际为 {type(body).__name__}",
                source=sid))
            continue

        if "enabled" not in body:
            issues.append(ConfigIssue(
                ReasonCode.MISSING_FIELD, f"{base}.enabled",
                "开关缺少必填字段 enabled", source=sid))
            enabled = False
        elif not isinstance(body["enabled"], bool):
            issues.append(ConfigIssue(
                ReasonCode.WRONG_TYPE, f"{base}.enabled",
                "enabled 必须是布尔值", source=sid))
            enabled = False
        else:
            enabled = body["enabled"]

        if "percentage" not in body:
            issues.append(ConfigIssue(
                ReasonCode.MISSING_FIELD, f"{base}.percentage",
                "开关缺少必填字段 percentage", source=sid))
            percentage = 0
        elif not _is_int(body["percentage"]):
            issues.append(ConfigIssue(
                ReasonCode.WRONG_TYPE, f"{base}.percentage",
                "percentage 必须是整数（0..100）", source=sid))
            percentage = 0
        elif not 0 <= body["percentage"] <= 100:
            issues.append(ConfigIssue(
                ReasonCode.PERCENTAGE_OUT_OF_RANGE, f"{base}.percentage",
                f"灰度比例 {body['percentage']} 越界，允许闭区间 [0, 100]",
                source=sid))
            percentage = 0
        else:
            percentage = body["percentage"]

        overrides, provenance = _parse_overrides(
            body.get("overrides"), base, sid, issues)

        # 只要本开关的关键字段没有致命错误就收录（overrides 致命错误已记入 issues，
        # 最终整体仍会拒绝）；收录便于错误累积阶段不产生连锁噪音。
        out[name] = FlagRule(
            enabled=enabled,
            percentage=percentage,
            overrides=frozenset(overrides.items()),
            override_provenance=frozenset(provenance.items()),
        )
    return out


def _parse_overrides(raw: Any, base: str, sid: str,
                     issues: list[ConfigIssue]) -> tuple[dict[str, bool],
                                                         dict[str, str]]:
    if raw is None:
        return {}, {}
    if not isinstance(raw, dict):
        issues.append(ConfigIssue(
            ReasonCode.WRONG_TYPE, f"{base}.overrides",
            f"overrides 必须是主体->布尔的对象，实际为 "
            f"{type(raw).__name__}", source=sid))
        return {}, {}

    overrides: dict[str, bool] = {}
    provenance: dict[str, str] = {}
    for subject, value in raw.items():
        path = f"{base}.overrides.{subject}"
        if not isinstance(subject, str) or not subject.strip():
            issues.append(ConfigIssue(
                ReasonCode.OVERRIDE_SUBJECT_EMPTY, path,
                "定向覆盖的主体必须是非空字符串", source=sid))
            continue
        if not isinstance(value, bool):
            issues.append(ConfigIssue(
                ReasonCode.OVERRIDE_VALUE_INVALID, path,
                f"覆盖值必须是布尔，实际为 {type(value).__name__}={value!r}",
                source=sid))
            continue
        subject = subject.strip()
        overrides[subject] = value
        provenance[subject] = sid
    return overrides, provenance
