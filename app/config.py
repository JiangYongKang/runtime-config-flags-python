"""多来源配置的加载、校验与合并。

来源按优先级从低到高排列（后者覆盖前者）。任何无效情况都会以带错误码的
``ConfigError`` 显式拒绝，绝不静默采用：

- 来源不存在 / 不是文件 / JSON 非法 / 顶层不是对象
- 未知字段（config 下的键、features 下的键、规则内未知属性）
- 字段缺失（没有默认值且所有来源都未提供）
- 类型不符（bool 不被当作 int）
- 取值越界（int/float 超出规格闭区间；percentage 越界 0..100）
- 来源之间冲突（同一字段在不同来源中以不兼容类型出现）

成功时产出不可变 :class:`~app.models.Snapshot`，并记录每一条覆盖来源，
作为可追溯的合并依据（reasons）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.errors import ConfigError, ErrorCode
from app.models import FieldSpec, FieldType, FlagRule, Snapshot

# 原始配置文档的形状： {"config": {...}, "features": {...}}
RawConfig = dict[str, Any]


class _Missing:
    """哨兵类型：表示某字段在所有来源中都缺失。"""

    _instance: "_Missing | None" = None

    def __new__(cls) -> "_Missing":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return "<MISSING>"

    def __bool__(self) -> bool:
        return False


# 哨兵：表示某字段在所有来源中都缺失。
MISSING: Any = _Missing()

# 默认字段规格（服务内置契约）。新增配置字段在这里登记。
DEFAULT_FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("max_connections", FieldType.INT, 100, 1, 10000),
    FieldSpec("timeout_ms", FieldType.INT, 5000, 0, 60000),
    FieldSpec("sample_rate", FieldType.FLOAT, 1.0, 0.0, 1.0),
    FieldSpec("enable_cache", FieldType.BOOL, True),
    FieldSpec("log_level", FieldType.STR, "info"),
)

_FLAG_ALLOWED_KEYS = {"default_on", "percentage", "overrides", "salt"}


def load_source(path: str | Path) -> RawConfig:
    """读取并解析单个 JSON 配置源；失败给出可区分错误码。"""
    p = Path(path)
    label = str(path)
    if not p.exists():
        raise ConfigError(
            ErrorCode.SOURCE_NOT_FOUND, f"配置来源不存在: {label}", source=label
        )
    if not p.is_file():
        raise ConfigError(
            ErrorCode.SOURCE_NOT_FILE, f"配置来源不是普通文件: {label}", source=label
        )
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 读取权限等边界
        raise ConfigError(
            ErrorCode.SOURCE_PARSE_ERROR, f"配置来源读取失败: {exc}", source=label
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            ErrorCode.SOURCE_PARSE_ERROR,
            f"配置来源 JSON 解析失败: {exc.msg} (line {exc.lineno}, col {exc.colno})",
            source=label,
            context={"line": exc.lineno, "col": exc.colno},
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(
            ErrorCode.SOURCE_NOT_MAPPING,
            f"配置来源顶层必须是 JSON 对象，实际为 {type(data).__name__}",
            source=label,
        )
    return data


def _local_type_matches(spec: FieldSpec, raw: Any) -> bool:
    """按规格判断原始值的“宽松类型”是否兼容（int 可喂给 float）。"""
    if spec.type is FieldType.BOOL:
        return isinstance(raw, bool)
    if spec.type is FieldType.INT:
        return isinstance(raw, int) and not isinstance(raw, bool)
    if spec.type is FieldType.FLOAT:
        return isinstance(raw, (int, float)) and not isinstance(raw, bool)
    return isinstance(raw, str)  # STR


def _as_bool(raw: Any, *, path: str, source: str) -> bool:
    if not isinstance(raw, bool):
        raise ConfigError(
            ErrorCode.TYPE_MISMATCH,
            f"{path} 期望布尔(bool)，实际为 {type(raw).__name__}: {raw!r}",
            path=path,
            source=source,
            context={"got": raw},
        )
    return raw


def _parse_flag_rule(name: str, raw: Any, *, source: str) -> FlagRule:
    """解析并校验单个开关规则。"""
    path = f"features.{name}"
    if not isinstance(raw, dict):
        raise ConfigError(
            ErrorCode.TYPE_MISMATCH,
            f"{path} 必须是 JSON 对象，实际为 {type(raw).__name__}",
            path=path,
            source=source,
            context={"got": raw},
        )

    unknown = set(raw) - _FLAG_ALLOWED_KEYS
    if unknown:
        raise ConfigError(
            ErrorCode.UNKNOWN_FIELD,
            f"{path} 包含未知属性: {sorted(unknown)}",
            path=path,
            source=source,
            context={"unknown": sorted(unknown)},
        )

    default_on = _as_bool(
        raw.get("default_on", False), path=f"{path}.default_on", source=source
    )

    pct_raw = raw.get("percentage", 0)
    if isinstance(pct_raw, bool) or not isinstance(pct_raw, int):
        raise ConfigError(
            ErrorCode.ILLEGAL_PERCENTAGE,
            f"{path}.percentage 必须是 0..100 的整数，实际为 {type(pct_raw).__name__}: {pct_raw!r}",
            path=f"{path}.percentage",
            source=source,
            context={"got": pct_raw},
        )
    if not 0 <= pct_raw <= 100:
        raise ConfigError(
            ErrorCode.ILLEGAL_PERCENTAGE,
            f"{path}.percentage 必须落在 0..100，实际为 {pct_raw}",
            path=f"{path}.percentage",
            source=source,
            context={"got": pct_raw, "min": 0, "max": 100},
        )

    overrides_raw = raw.get("overrides", {})
    if not isinstance(overrides_raw, dict):
        raise ConfigError(
            ErrorCode.TYPE_MISMATCH,
            f"{path}.overrides 必须是主体->布尔的对象，实际为 {type(overrides_raw).__name__}",
            path=f"{path}.overrides",
            source=source,
        )
    overrides: dict[str, bool] = {}
    for subject, val in overrides_raw.items():
        if not isinstance(subject, str):
            raise ConfigError(
                ErrorCode.TYPE_MISMATCH,
                f"{path}.overrides 的主体必须是字符串: {subject!r}",
                path=f"{path}.overrides",
                source=source,
            )
        overrides[subject] = _as_bool(
            val, path=f"{path}.overrides.{subject}", source=source
        )

    salt = raw.get("salt", "")
    if not isinstance(salt, str):
        raise ConfigError(
            ErrorCode.TYPE_MISMATCH,
            f"{path}.salt 必须是字符串，实际为 {type(salt).__name__}",
            path=f"{path}.salt",
            source=source,
        )

    return FlagRule(
        name=name,
        default_on=default_on,
        percentage=pct_raw,
        overrides=overrides,
        salt=salt,
    )


def _check_duplicate_specs(field_specs: tuple[FieldSpec, ...]) -> dict[str, FieldSpec]:
    specs: dict[str, FieldSpec] = {}
    for spec in field_specs:
        if spec.name in specs:
            raise ConfigError(
                ErrorCode.DUPLICATE_FIELD_SPEC,
                f"字段规格重复定义: {spec.name}",
                path=f"config.{spec.name}",
            )
        specs[spec.name] = specs.get(spec.name, spec)
    return specs


def build_snapshot(
    source_paths: list[str | Path],
    *,
    field_specs: tuple[FieldSpec, ...] = DEFAULT_FIELD_SPECS,
    version: int,
) -> Snapshot:
    """加载、校验、合并多个来源并构建不可变快照。

    任何无效内容都会抛出 ``ConfigError``（携带错误码、字段路径与来源）。
    来源顺序即优先级：靠后者覆盖靠前者。
    """
    specs = _check_duplicate_specs(field_specs)

    # 1) 加载并做结构校验。
    loaded: list[tuple[str, RawConfig]] = []
    for sp in source_paths:
        raw = load_source(sp)
        for top_key, section in raw.items():
            if top_key not in ("config", "features"):
                raise ConfigError(
                    ErrorCode.UNKNOWN_FIELD,
                    f"来源 {sp} 包含未知顶层段: {top_key!r}（仅允许 config / features）",
                    path=top_key,
                    source=str(sp),
                )
            if not isinstance(section, dict):
                seg = "config" if top_key == "config" else "features"
                raise ConfigError(
                    ErrorCode.TYPE_MISMATCH,
                    f"来源 {sp} 的 {seg} 段必须是 JSON 对象",
                    path=seg,
                    source=str(sp),
                )
        loaded.append((str(sp), raw))

    reasons: list[str] = []

    # 2) 合并 config 段：记录每个字段从各来源看到的值。
    #    layers[name] = [(source, raw_value), ...] 按优先级从低到高
    layers: dict[str, list[tuple[str, Any]]] = {}
    for source, raw in loaded:
        cfg = raw.get("config", {})
        unknown = set(cfg) - set(specs)
        if unknown:
            raise ConfigError(
                ErrorCode.UNKNOWN_FIELD,
                f"来源 {source} 包含未登记的配置字段: {sorted(unknown)}",
                path=f"config.{sorted(unknown)[0]}",
                source=source,
                context={"unknown": sorted(unknown)},
            )
        for name, val in cfg.items():
            layers.setdefault(name, []).append((source, val))

    values: dict[str, Any] = {}
    for name, spec in specs.items():
        chain = layers.get(name, [])
        if not chain:
            if spec.default is MISSING:
                raise ConfigError(
                    ErrorCode.MISSING_FIELD,
                    f"必填字段 {name} 在所有来源中缺失，且未声明默认值",
                    path=f"config.{name}",
                    context={"sources": [s for s, _ in loaded]},
                )
            values[name] = spec.default
            reasons.append(f"config.{name} 使用默认值 {spec.default!r}")
            continue

        # 跨来源冲突检测：被覆盖掉的低优先级值类型必须与规格兼容。
        for src, val in chain[:-1]:
            if not _local_type_matches(spec, val):
                raise ConfigError(
                    ErrorCode.SOURCE_CONFLICT,
                    (
                        f"字段 {name} 在来源 {src} 中的类型与规格({spec.type.value})不兼容，"
                        f"虽被更高优先级来源覆盖，仍判定为来源冲突而拒绝"
                    ),
                    path=f"config.{name}",
                    source=src,
                    context={"got_type": type(val).__name__, "expected": spec.type.value},
                )

        winner_source, winner_val = chain[-1]
        normalized = spec.validate_value(
            winner_val, path=f"config.{name}", source=winner_source
        )
        values[name] = normalized
        if len(chain) > 1:
            lower = ", ".join(s for s, _ in chain[:-1])
            reasons.append(
                f"config.{name} 由来源 {winner_source} 覆盖（低优先级: {lower}）-> {normalized!r}"
            )
        else:
            reasons.append(f"config.{name} 来自来源 {winner_source} -> {normalized!r}")

    # 3) 合并 features 段：高优先级来源整体替换同名开关规则。
    flag_layers: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for source, raw in loaded:
        for flag_name, flag_raw in raw.get("features", {}).items():
            flag_layers.setdefault(flag_name, []).append((source, flag_raw))

    flags: dict[str, FlagRule] = {}
    for flag_name, chain in flag_layers.items():
        winner_source, winner_raw = chain[-1]
        rule = _parse_flag_rule(flag_name, winner_raw, source=winner_source)
        flags[flag_name] = rule
        if len(chain) > 1:
            lower = ", ".join(s for s, _ in chain[:-1])
            reasons.append(
                f"features.{flag_name} 由来源 {winner_source} 整体覆盖（低优先级: {lower}）"
            )
        else:
            reasons.append(f"features.{flag_name} 来自来源 {winner_source}")

    sources_tuple = tuple(str(sp) for sp in source_paths)
    return Snapshot(
        version=version,
        values=values,
        flags=flags,
        sources=sources_tuple,
        reasons=tuple(reasons),
    )
