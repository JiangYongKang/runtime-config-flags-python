"""多来源配置的合并与整体校验。

优先级与冲突语义（不静默采用任何无法解释的值）：
- priority 数字越大优先级越高；同优先级按 source_id 字典序保证确定性；
- 不同优先级来源对同一字段给出不同取值时，高优先级“确定性覆盖”低优先级，
  这是分层配置的正常语义；被覆盖关系完整记录在 provenance 中，可追溯，
  不属于静默采用；
- 相同优先级来源对同一字段给出不同取值 => SOURCE_CONFLICT（无法裁决，拒绝）；
- flags 的 enabled/percentage 同理；overrides 按主体合并：
  * 高优先级来源对某主体的定向覆盖优先；
  * 同优先级不同来源对同一主体给出不同布尔 => CONFLICTING_OVERRIDE；
- 合并完成后检查必填 settings 是否齐备，缺失 => SETTINGS_MISSING/MISSING_FIELD。
"""

from __future__ import annotations

from runtime.errors import ConfigIssue, ConfigRejected, ReasonCode
from runtime.model import REQUIRED_SETTINGS, FlagRule, MergedConfig, SourceConfig


def merge_sources(sources: list[SourceConfig], *, version: int) -> MergedConfig:
    issues: list[ConfigIssue] = []

    # 确定性顺序：优先级高在前，同优先级按 source_id
    ordered = sorted(sources, key=lambda s: (-s.priority, s.source_id))
    if not ordered:
        raise ConfigRejected([ConfigIssue(
            ReasonCode.EMPTY_UPDATE, "$",
            "没有任何有效来源，拒绝生成空配置版本")])

    # key -> (value, priority, source_id)
    settings_state: dict[str, tuple[object, int, str]] = {}
    settings_provenance: dict[str, list[str]] = {}

    flags_enabled: dict[str, tuple[bool, int, str]] = {}
    flags_percentage: dict[str, tuple[int, int, str]] = {}
    flag_provenance: dict[str, set[str]] = {}
    # flag -> subject -> (value, priority, source)
    overrides: dict[str, dict[str, tuple[bool, int, str]]] = {}

    for src in ordered:
        for key, value in src.settings:
            settings_provenance.setdefault(key, []).append(src.source_id)
            if key in settings_state:
                old_val, old_prio, old_src = settings_state[key]
                if old_prio == src.priority and old_val != value:
                    issues.append(ConfigIssue(
                        ReasonCode.SOURCE_CONFLICT, f"settings.{key}",
                        f"同优先级(priority={src.priority})来源 "
                        f"{old_src!r}={old_val!r} 与 {src.source_id!r}="
                        f"{value!r} 取值冲突，无法裁决；如需覆盖请设置不同"
                        f"优先级", source=src.source_id))
                    continue
                if src.priority > old_prio:
                    settings_state[key] = (value, src.priority, src.source_id)
            else:
                settings_state[key] = (value, src.priority, src.source_id)

        for name, rule in src.flags:
            flag_provenance.setdefault(name, set()).add(src.source_id)
            if name in flags_enabled:
                old_en, en_prio, en_src = flags_enabled[name]
                # 遍历从高优先级到低优先级：仅同优先级的不同取值是冲突；
                # 已有值优先级更高时直接胜出。
                if en_prio == src.priority and old_en != rule.enabled:
                    issues.append(ConfigIssue(
                        ReasonCode.SOURCE_CONFLICT, f"flags.{name}.enabled",
                        f"同优先级来源 {en_src!r}={old_en!r} 与 "
                        f"{src.source_id!r}={rule.enabled!r} 基线开关冲突",
                        source=src.source_id))

                old_pct, pct_prio, pct_src = flags_percentage[name]
                if pct_prio == src.priority and old_pct != rule.percentage:
                    issues.append(ConfigIssue(
                        ReasonCode.SOURCE_CONFLICT,
                        f"flags.{name}.percentage",
                        f"同优先级来源 {pct_src!r}={old_pct} 与 "
                        f"{src.source_id!r}={rule.percentage} 灰度比例冲突",
                        source=src.source_id))
            else:
                flags_enabled[name] = (
                    rule.enabled, src.priority, src.source_id)
                flags_percentage[name] = (
                    rule.percentage, src.priority, src.source_id)

            bucket = overrides.setdefault(name, {})
            for subject, value in rule.override_map().items():
                prov = rule.provenance_map().get(subject, src.source_id)
                if subject in bucket:
                    old_val, old_prio, old_src = bucket[subject]
                    # 遍历顺序为优先级从高到低：old_prio >= src.priority。
                    # 仅当同优先级且取值不同才是无法裁决的冲突；
                    # old_prio 更高时其覆盖理应胜出，静默忽略当前值即可
                    # （关系已记录在 provenance 中，不属于静默采用）。
                    if old_prio == src.priority and old_val != value:
                        issues.append(ConfigIssue(
                            ReasonCode.CONFLICTING_OVERRIDE,
                            f"flags.{name}.overrides.{subject}",
                            f"主体 {subject!r} 在同优先级来源 "
                            f"{old_src!r}={old_val} 与 {prov!r}={value} "
                            f"定向覆盖冲突，无法裁决", source=src.source_id))
                        continue
                    if src.priority > old_prio:
                        bucket[subject] = (value, src.priority, prov)
                else:
                    bucket[subject] = (value, src.priority, prov)

    settings = {k: v for k, (v, _, _) in settings_state.items()}
    if not settings:
        issues.append(ConfigIssue(
            ReasonCode.SETTINGS_MISSING, "settings",
            "所有来源均未提供 settings"))
    else:
        for key in REQUIRED_SETTINGS:
            if key not in settings:
                issues.append(ConfigIssue(
                    ReasonCode.MISSING_FIELD, f"settings.{key}",
                    f"合并后仍缺少必填字段 {key!r}，无法生效"))

    if issues:
        raise ConfigRejected(issues)

    flags_out: dict[str, FlagRule] = {}
    flags_prov_out: dict[str, tuple[str, ...]] = {}
    for name in flags_enabled:
        ov = overrides.get(name, {})
        flags_out[name] = FlagRule(
            enabled=flags_enabled[name][0],
            percentage=flags_percentage[name][0],
            overrides=frozenset((s, v) for s, (v, _, _) in ov.items()),
            override_provenance=frozenset(
                (s, src) for s, (_, _, src) in ov.items()),
        )
        order = [s.source_id for s in ordered
                 if s.source_id in flag_provenance[name]]
        flags_prov_out[name] = tuple(dict.fromkeys(order))

    settings_prov_out: dict[str, tuple[str, ...]] = {}
    ordered_ids = [s.source_id for s in ordered]
    for key, srcs in settings_provenance.items():
        settings_prov_out[key] = tuple(
            sid for sid in ordered_ids if sid in srcs)

    return MergedConfig(
        version=version,
        settings=dict(sorted(settings.items())),
        flags=dict(sorted(flags_out.items())),
        settings_provenance=dict(sorted(settings_prov_out.items())),
        flags_provenance=dict(sorted(flags_prov_out.items())),
        sources=tuple(ordered_ids),
    )
