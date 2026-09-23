# 运行时配置与特性开关服务（Python / FastAPI）

在不重启服务的前提下，提供**多来源配置合并、原子热更新、按主体灰度的特性开关**。
配置变更、灰度判定与运行状态在本地完全可复现；任何无效输入都以**稳定可区分的
错误码**显式拒绝，绝不静默采用。

## 目录结构

```
app/
  errors.py    # ErrorCode 与 ConfigError（错误码、定位、来源、上下文）
  models.py    # FieldSpec / FlagRule / 不可变 Snapshot
  config.py    # 多源加载、校验、合并（默认字段规格在此登记）
  store.py     # 线程安全原子快照存储（引用替换 + CAS）
  flags.py     # 稳定桶值与灰度/覆盖判定（sha256）
  service.py   # 门面：bootstrap / reload / get / evaluate_flag
  api.py       # FastAPI 路由与结构化错误响应
config/        # 本地示例配置（base.json 低优先级，local.json 高优先级）
tests/         # pytest：校验、灰度边界、热更回滚、并发、跨进程确定性
main.py        # ASGI 入口（uv main:app）
```

## 一、配置优先级

配置来源为一个**有序列表，后者优先级高于前者**，合并时按字段覆盖：

```
config/base.json   → 低优先级
config/local.json  → 高优先级（同名字段覆盖 base）
```

- 启动来源可用环境变量覆盖：`RUNTIME_CONFIG_FILES=a.json:b.json`（冒号或分号分隔）。
- 每个 JSON 文档形如：

```json
{
  "config": { "timeout_ms": 1500 },
  "features": {
    "new_ui": { "default_on": false, "percentage": 25,
                "overrides": {"tester@example.com": true}, "salt": "checkout-2026" }
  }
}
```

- **config 段**：逐字段合并。字段必须在内置 `DEFAULT_FIELD_SPECS` 中登记
  （`app/config.py`），未知字段直接拒绝。未在任何来源出现的字段使用规格默认值；
  若规格声明为 `MISSING`（无默认）则按 `missing_field` 拒绝。
- **features 段**：同名开关由**最高优先级来源的整条规则替换**（不做规则内属性
  的逐层合并，语义简单、结果可解释）。
- 每次合并都会在快照 `reasons` 中记录“字段来自哪个来源 / 被谁覆盖 / 是否用默认值”，
  通过 `GET /config` 可见，可追溯。

### 拒绝原因（不静默采用）

| 错误码 | 触发条件 |
| --- | --- |
| `source_not_found` / `source_not_file` | 来源不存在或不是普通文件 |
| `source_parse_error` | JSON 解析失败（含行列号） |
| `source_not_mapping` | 顶层不是 JSON 对象 |
| `unknown_field` | 未知顶层段、未登记 config 字段、开关规则内未知属性 |
| `missing_field` | 必填字段在所有来源缺失且无默认值 |
| `type_mismatch` | 类型不符（**bool 不会被当作 int**；int 字段不接受 1.5/float） |
| `value_out_of_range` | int/float 超出规格闭区间 |
| `source_conflict` | 同一字段在不同来源中以**不兼容类型**出现（即使低优先级值会被覆盖也拒绝，避免脏来源被静默吞掉） |
| `illegal_percentage` | 灰度比例不是 0..100 的整数（含 bool、浮点、字符串） |

## 二、启动行为（确定且可解释）

- `strict=True`（默认）：启动时配置无效 → 抛 `ConfigError`，**拒绝启动**，
  日志打印错误码、字段路径与来源。
- `strict=False`：服务仍可启动，但进入“无快照”降级状态；此后所有配置/开关读取
  确定性地返回 `no_snapshot`（HTTP 503），`GET /health` 返回
  `{"status":"degraded"}`，不会使用半成品配置。

## 三、热更新语义（原子、可回滚、可追溯）

`POST /reload`（或 `service.reload()`）：

1. 从来源列表重新读取，在**临时数据**上完成全部加载与校验，构建新的不可变快照；
2. 校验通过后，基于当前版本号做 **CAS（compare-and-set）发布**——底层只是一次
   持锁引用替换；
3. **原子可见**：任一请求拿到的要么是完整旧版本、要么是完整新版本，绝不会读到
   半个配置。`Snapshot` 为 frozen dataclass，内部映射以 `MappingProxyType` 冻结；
4. **无效更新不改变当前状态**：校验失败时根本不会触碰存储（天然回滚），错误码、
   字段路径、来源随 422 响应与日志返回；
5. **并发更新**：`expected_version` 过期时返回 409 `version_conflict`，当前状态
   不变，由调用方重试；不带 `expected_version` 时无条件发布，版本严格 +1。

版本号从 1 开始，每次成功热更 +1，单调不跳号。

## 四、特性开关判定规则

对开关 `flag` 与主体 `subject`，依据当前快照中的规则依次判定：

1. **定向覆盖优先**：`overrides` 中含该主体 → 直接采用覆盖值
   （`override_on` / `override_off`），任何灰度/默认都被翻转；
2. **灰度桶值**：`bucket = sha256(flag + "\x00" + salt + "\x00" + subject) 前8字节 % 100`，
   范围 `0..99`；当 `bucket < percentage` 命中（`gray_hit`），否则 `gray_miss`；
3. **默认值**：`percentage=0` 时不做灰度，采用 `default_on`
   （`default_on` / `default_off`）。

边界：

- `percentage=0`：无人命中灰度（仍可被 overrides 打开）；
- `percentage=100`：所有主体命中（仍可被 overrides 关闭）；
- 中间比例：对 2000 主体实测命中率约等于设定比例（0.40–0.60 @50%）。

确定性与隔离：

- 使用 **sha256** 而非 Python 内建 `hash()`，不受 `PYTHONHASHSEED` 影响，
  **跨进程、跨时间结果一致**（见 `tests/test_determinism.py`，在 0/1/12345 三种
  seed 子进程中锁定桶值）；
- 开关名参与散列，**开关之间互不影响**；修改 `salt` 可重新分布灰度人群；
- 未知开关 → `unknown_flag`；非法比例在构建期即 `illegal_percentage` 拒绝；
  覆盖值非布尔 → `type_mismatch` 拒绝。

判定响应包含完整依据：

```json
{ "flag": "new_ui", "subject": "alice", "on": false,
  "reason": "gray_miss", "percentage": 25, "bucket": 65, "version": 3 }
```

## 五、HTTP 接口

| 方法/路径 | 说明 |
| --- | --- |
| `GET /health` | 存活与当前版本（无快照时 degraded） |
| `GET /config` | 当前完整配置 + 开关 + 版本 + 合并依据 |
| `GET /config/{key}` | 单个配置字段 |
| `GET /flags` | 当前开关清单 |
| `GET /flags/{flag}?subject=...` | 开关判定（返回完整依据） |
| `POST /reload` | 热更新，体：`{"sources": [...], "expected_version": 1}`（均可选） |

错误响应统一为：HTTP 422（配置类）/ 409（版本冲突）/ 503（无快照），体：

```json
{ "error_code": "value_out_of_range", "message": "...",
  "path": "config.timeout_ms", "source": "config/local.json", "context": {...} }
```

## 六、本地验证方法

```bash
# 1) 安装依赖（Python 3.14 + uv）
uv sync

# 2) 跑全部单测（含并发读写、无效更新回滚、0%/100%/中间灰度、重复判定稳定性、
#    跨进程确定性；日志打印输入配置、生效版本与判定依据）
uv run pytest -s

# 3) 启动服务（默认读取 config/base.json + config/local.json）
uv run uvicorn main:app --reload

# 4) 手工验证
curl -s localhost:8000/health
curl -s localhost:8000/config | jq .
curl -s "localhost:8000/flags/new_ui?subject=alice" | jq .
curl -s -X POST localhost:8000/reload \
  -H 'content-type: application/json' \
  -d '{"sources":["config/base.json","config/local.json"],"expected_version":1}'
```

验证“无效更新回滚”：把 `config/local.json` 的 `timeout_ms` 改成 `99999999`
后 POST `/reload`，得到 422 `value_out_of_range`，而 `/health` 版本号与
`/config/timeout_ms` 保持不变。

测试日志（`-s`）中每个用例都会打印：
`[测试输入配置] ...`、`[测试生效版本] version=N`、`[测试合并依据] ...`、
`[判定依据] ...`、`[拒绝原因] code=...`，失败原因可区分、可复现。
