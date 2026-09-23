# 运行时配置与特性开关（Runtime Config & Feature Flags）

在不重启服务的前提下，支持多来源配置合并、原子热更新、按主体维度的特性开关灰度
与定向覆盖。所有无效输入都有**可区分的拒绝原因码**，所有判定都**稳定可复现**。

## 1. 快速开始

```bash
uv sync                       # 安装依赖（含 dev：pytest/httpx）
uv run pytest                 # 运行全部测试（日志会打印输入配置/版本/判定依据）
uv run fastapi dev main.py    # 启动服务（默认加载 config/local.json）
```

启动后：

```bash
curl -s http://127.0.0.1:8000/health | python -m json.tool
curl -s "http://127.0.0.1:8000/flags/new_checkout/evaluate?subject=user-42"
```

## 2. 配置模型与来源优先级

一份配置由若干**来源（source）**组成，每个来源形如：

```json
{
  "source_id": "defaults",
  "priority": 10,
  "settings": {
    "max_connections": 100,
    "request_timeout_ms": 3000,
    "sample_rate": 0.1,
    "log_level": "INFO"
  },
  "flags": {
    "new_checkout": {
      "enabled": true,
      "percentage": 50,
      "overrides": {"user-42": true, "user-7": false}
    }
  }
}
```

### 2.1 settings 字段约束（类型/范围非法都会被拒绝）

| 字段 | 类型 | 合法区间 / 取值 |
| --- | --- | --- |
| `max_connections` | int（不接受 bool） | [1, 100000] |
| `request_timeout_ms` | int | [1, 3600000] |
| `sample_rate` | number（不接受 bool） | [0.0, 1.0] |
| `log_level` | string 枚举 | `DEBUG`/`INFO`/`WARN`/`ERROR` |

这四个字段在**合并后必须全部存在**，否则拒绝生效（`MISSING_FIELD` /
`SETTINGS_MISSING`）。

### 2.2 优先级与冲突规则（不静默采用任何值）

- `priority` 数字越大优先级越高；同优先级按 `source_id` 字典序处理，保证结果
  与输入顺序无关（确定性）。
- **不同优先级**对同一字段给出不同取值：高优先级确定性覆盖低优先级。被覆盖的
  来源链完整记录在 `settings_provenance` / `flags_provenance` 中，可追溯，
  因此不是“静默采用”。
- **相同优先级**对同一字段给出不同取值：无法裁决，整份更新被拒绝，返回
  `SOURCE_CONFLICT`（flags 的覆盖冲突返回 `CONFLICTING_OVERRIDE`）。
- 一次更新内 `source_id` 重复 => `SOURCE_CONFLICT`。
- 开关的 `enabled` / `percentage` 标量按上述规则合并；`overrides` 按**主体**
  合并（不同主体互不干扰）。

`GET /config` 返回生效配置，每个字段都带 `*_provenance`，例如：

```json
"settings_provenance": {"log_level": ["local-override", "defaults"]}
```

## 3. 热更新语义（不重启、原子、可回滚）

- 生效配置是一个**不可变快照** `MergedConfig`，带单调递增的 `version`。
- 更新在锁内完成“解析 → 合并 → 发布”，**成功才整体替换引用**；任一步失败都
  直接丢弃新对象，**当前版本保持不变**（天然回滚）。
- 读路径只读取一次快照引用：任一请求看到的 settings 与 flags 必属**同一版本**，
  不会出现“半个新配置”。需要在同一请求里既读配置又判开关时，取一次
  `store.current()` 后调用 `ConfigStore.evaluate_snapshot(snap, ...)`。
- 每次拒绝都会留痕（含输入来源摘要、全部原因码、停留版本），可通过
  `GET /admin/rejected` 查看。
- 支持乐观条件更新：`POST /admin/reload` 带 `"expected_version": N`，基准版本
  不符返回 409 `VERSION_MISMATCH`，当前状态不变。

```bash
# 有效更新 -> 新版本
curl -s -X POST http://127.0.0.1:8000/admin/reload \
  -H 'content-type: application/json' \
  -d '{"sources":[{"source_id":"svc","priority":10,
      "settings":{"max_connections":10,"request_timeout_ms":100,
                  "sample_rate":0.2,"log_level":"INFO"},
      "flags":{"rollout":{"enabled":true,"percentage":100,"overrides":{}}}}]}'

# 无效更新 -> 422，版本不变
curl -s -X POST http://127.0.0.1:8000/admin/reload \
  -H 'content-type: application/json' \
  -d '{"sources":[{"source_id":"bad","priority":1,
      "settings":{"max_connections":0}}]}'
```

### 启动行为（确定且可解释）

服务启动时加载 `config/local.json`：

- 加载成功：`/health` 返回 `initialized=true` 与版本；
- 文件缺失 / JSON 非法 / 校验失败：**不静默启动**，服务进入“未初始化”状态，
  `/health` 的 `startup.issues` 给出确切原因；此时读取配置/判开关返回
  `503 NOT_INITIALIZED`。可用 `POST /admin/reload` 推送有效配置后恢复。

## 4. 特性开关判定规则

对 `(flag, subject)`，按以下优先级判定（结果含 `reason` 与 `basis.detail`）：

1. **定向覆盖** `overrides[subject]` 存在 => 直接采用该布尔
   （`reason=override`，含来源）。覆盖优先于总闸与百分比。
2. **总闸关闭** `enabled=false` => 关（`reason=baseline_disabled`）。
3. 总闸开启时按**灰度百分比**：
   - `percentage=0`   => 恒关（`percentage_zero`）；
   - `percentage=100` => 恒开（`percentage_full`）；
   - `0 < p < 100`    => `bucket(flag, subject) < p` 则开
     （`percentage_rollout`）。

分桶公式（纯函数、跨进程一致）：

```
bucket = int(sha256("flag-eval|{flag}|{subject}").hexdigest()[:8], 16) % 100
```

- **稳定可复现**：同一 `(flag, subject)` 只要配置不变，任意时刻/任意进程判定
  完全一致（测试中有跨进程复现用例与固定向量锁定）。
- **开关互不影响**：flag 名参与哈希，不同开关对同一主体独立分桶。
- **边界确定**：bucket ∈ [0,99]，故 0% 全关、100% 全开，无 off-by-one。
- **未知开关**：不做静默兜底，返回 404 `UNKNOWN_FLAG` 并列出已知开关。

判定响应示例：

```json
{
  "flag": "new_checkout", "subject": "user-42", "on": true,
  "version": 1, "reason": "override",
  "basis": {"bucket": 0, "percentage": 50,
            "detail": "主体命中来源 'local-override' 的定向覆盖 => 开 ..."}
}
```

## 5. 拒绝原因码表（失败原因可区分）

| Code | 含义 |
| --- | --- |
| `NOT_A_MAPPING` | 配置体/请求体不是对象，或 `sources` 不是数组 |
| `MISSING_FIELD` | 必填字段缺失（如 `priority`、`percentage`、合并后缺 settings 字段） |
| `WRONG_TYPE` | 类型不符（含 bool 冒充 int、未知 settings 字段等结构错误） |
| `BAD_ENUM` | 枚举取值非法（如 `log_level=VERBOSE`） |
| `SETTINGS_MISSING` | 所有来源都没提供 settings |
| `SETTING_OUT_OF_RANGE` | settings 取值越界 |
| `FLAGS_NOT_A_MAPPING` / `FLAG_NOT_A_MAPPING` | flags 容器/单个开关定义结构非法 |
| `UNKNOWN_FLAG` | 判定了当前版本中不存在的开关 |
| `PERCENTAGE_OUT_OF_RANGE` | 灰度比例不在闭区间 [0,100] |
| `OVERRIDE_SUBJECT_EMPTY` / `OVERRIDE_VALUE_INVALID` | 覆盖主体为空 / 覆盖值非布尔 |
| `CONFLICTING_OVERRIDE` | 同优先级来源对同一主体给出冲突的定向值 |
| `SOURCE_ID_EMPTY` / `SOURCE_PRIORITY_INVALID` | 来源 id 为空 / priority 非非负整数 |
| `SOURCE_CONFLICT` | 同优先级来源同字段取值冲突，或来源 id 重复 |
| `EMPTY_UPDATE` | 提交了零个有效来源 |
| `NOT_INITIALIZED` | 尚未持有任何有效版本（启动失败或未加载） |
| `VERSION_MISMATCH` | 乐观更新基于过期版本（HTTP 409） |

HTTP 状态码：422 校验/合并冲突，409 版本冲突，404 未知开关，503 未初始化。

## 6. HTTP 接口

| 方法 路径 | 说明 |
| --- | --- |
| `GET /health` | 运行状态：是否初始化、当前版本、启动加载留痕 |
| `GET /config` | 当前生效配置全文（settings/flags + provenance） |
| `GET /flags/{name}/evaluate?subject=...` | 开关判定（on/reason/bucket/percentage/detail） |
| `POST /admin/reload` | 热更新；body `{"sources":[...]}`，可选 `expected_version` |
| `GET /admin/rejected` | 最近被拒绝更新的留痕（原因可追溯） |

## 7. 本地验证方法

```bash
# 全量测试（默认开启 log_cli，打印输入配置、生效版本、判定依据/失败原因）
uv run pytest

# 按主题运行
uv run pytest tests/test_parser_validation.py    # 各类无效输入的可区分原因
uv run pytest tests/test_merger.py               # 优先级覆盖/冲突/确定性
uv run pytest tests/test_flags_evaluation.py     # 0%/100%/中间比例、稳定性
uv run pytest tests/test_store_concurrency.py    # 并发原子可见/回滚/版本控制
uv run pytest tests/test_api.py                  # HTTP 状态码与错误结构
uv run pytest tests/test_reproducibility.py      # 跨进程判定一致 + 固定向量
```

覆盖点对照需求：

- **并发读写**：8 读线程 × 多版本发布风暴，校验无撕裂读、判定与快照同版本；
  20 并发写者被串行化为连续版本，不丢不跳不重。
- **无效更新回滚**：越界值/非法比例更新被 422 拒绝，版本与快照引用均不变，
  拒绝留痕可查。
- **灰度边界**：0% 恒关、100% 恒开、1/10/25/50/75/99% 严格等价
  `bucket < p`，并对 500 主体统计命中率。
- **重复判定稳定性**：同配置 6 轮结果一致；跨进程结果逐一一致；固定哈希向量
  锁定公式。
- **日志**：每个用例通过 `log_case` 打印输入配置、生效版本与判定依据/拒绝原因。

## 8. 代码结构

```
runtime/
  errors.py   # ReasonCode、ConfigIssue、ConfigRejected、NotInitializedError
  model.py    # 字段约束 + SourceConfig/MergedConfig/FlagRule/EvaluationResult
  parser.py   # 单来源严格校验（类型/范围/结构，累积全部问题）
  merger.py   # 多来源优先级合并、同优先级冲突检测、合并后完整性校验
  flags.py    # 稳定分桶(sha256) 与可解释判定
  store.py    # 原子快照存储、热更新/回滚、乐观版本控制、拒绝留痕
  api.py      # FastAPI 工厂、结构化错误响应、启动确定性加载
main.py       # create_app() 入口（默认加载 config/local.json）
config/local.json  # 本地示例：defaults + local-override 两个来源
tests/        # 上述 6 组测试
```
