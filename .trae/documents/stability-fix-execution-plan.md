# 稳定性修复 + 测试验证 — 执行计划（续作）

## 摘要

承接已批准的 `professional-test-plan.md` (v3)。后端 P0 稳定性修复已完成（3 处代码改动已就位），本计划聚焦**剩余执行工作**：前端 P0 保护修复 + 白盒测试补强 + 黑盒测试 + UAT 清单。

**目标**：彻底解决用户报告的两个核心稳定性问题——
1. 第三方接口探测多次没响应（卡死）
2. 机会挖掘全量同步多次卡在中间

覆盖用户 4 个维度：功能显示正常 / 交互合理 / 数据正确性 / UAT 阶段验证。

---

## 当前状态分析

### 已完成（后端 P0 稳定性修复 — 已验证就位）

| 文件 | 修复内容 | 验证位置 |
|------|---------|---------|
| `app/services/akshare_utils.py` | `_DEFAULT_TIMEOUT=(5,15)` + patch `requests.Session.request` 注入默认 timeout | 行 50-97 |
| `app/api/routes/akshare_apis.py` | `probe_api` 改 `async def` + `asyncio.wait_for(..., timeout=30)` + `_run_probe` 包装 | 行 14, 182-235 |
| `app/services/discovery_tasks.py` | `_watchdog_heartbeat` + `_sync_one_symbol_with_timeout` (90s) + `STALE_RUNNING_DEADLINE=10min` + sync 循环捕获 `TimeoutError` | 行 58-71, 751-801, 849-888, 1022-1024 |

### 待完成工作

| 优先级 | 任务 | 类型 |
|--------|------|------|
| P0 | `frontend/src/api/client.ts` 探测超时 60→30s | 前端代码 |
| P0 | `frontend/src/components/AkshareApiManager.tsx` 批量探测并发 + 防重复 | 前端代码 |
| P0 | `frontend/src/components/Discovery.tsx` 卡死检测提示 | 前端代码 |
| P0 | `tests/test_whitebox_akshare_http.py` 追加 4 用例（timeout 守护） | 测试 |
| P0 | `tests/test_whitebox_async_tasks.py` 追加 5 用例（超时+watchdog+24h） | 测试 |
| P0 | `tests/test_whitebox_discovery_timeout.py` 新建 4 用例（集成验证） | 测试 |
| P0 | 运行回归测试 `pytest -m "not slow" -v` | 验证 |
| P1 | `tests/test_blackbox_api_mgmt.py` 新建 17 用例 | 测试 |
| P1 | `docs/uat-checklist.md` 创建 60+ 勾选项 | 文档 |

---

## 提议变更

### 阶段一：前端 P0 保护修复

#### 1.1 `frontend/src/api/client.ts` — 探测超时对齐

**目的**：前端探测超时从 60s 降到 30s，与后端 `_PROBE_TIMEOUT_SECONDS=30` 对齐，避免前端长时间等待。

**修改点**（行 441）：

```typescript
// 现状
probeAkshareApi: (apiKey: string) =>
  requestJson<...>(
    `${API}/external-data/apis/${encodeURIComponent(apiKey)}/probe`,
    { method: "POST", timeoutMs: 60000 },  // ← 改为 30000
  ),
```

改为 `timeoutMs: 30000`。

#### 1.2 `frontend/src/components/AkshareApiManager.tsx` — 批量探测并发 + 防重复

**目的**：现状是串行 `for...of` + `await handleProbe(a.key)`（行 88-97），17 个接口串行最坏 17×30s=510s。改为 3 个一组并发，最坏 6 批×30s=180s。

**修改点 A — `handleProbeAll` 改并发**（行 87-97）：

```typescript
// 批量探测（3 个一组并发，平衡速度与风控）
const handleProbeAll = async () => {
  setProbingAll(true);
  // 禁用所有单个探测按钮（防重复点击）
  setProbingKeys(new Set(apis.map(a => a.key)));
  try {
    const CONCURRENCY = 3;
    for (let i = 0; i < apis.length; i += CONCURRENCY) {
      const batch = apis.slice(i, i + CONCURRENCY);
      // 并发执行本批，全部完成再进入下一批
      await Promise.allSettled(batch.map(a => probeOne(a.key)));
    }
  } finally {
    setProbingAll(false);
    setProbingKeys(new Set());
  }
};
```

**注意**：需提取 `probeOne` 为独立函数（不依赖 `handleProbe` 的 UI 副作用），或在 `handleProbe` 内部加 `probingAll` 守卫避免重复 set。**实施时优先提取 `probeOne` 纯逻辑函数**，`handleProbe` 调用它并处理 UI。

**修改点 B — 单个探测按钮禁用条件**（行 311 附近，按钮渲染处）：

```typescript
// 批量探测时所有单个按钮禁用
loading={probingKeys.has(r.key) || probingAll}
disabled={probingAll || probingKeys.has(r.key)}
```

#### 1.3 `frontend/src/components/Discovery.tsx` — 任务卡死检测提示

**目的**：前端检测 `task.updated_at` 长时间未变化（>2 分钟）时显示"任务可能卡死"警告。

**当前架构**：任务轮询在 `AppContext.tsx`（行 451-470，`discoveryPollRef.current = setInterval`），`Discovery.tsx` 通过 `const task = ctx.discoveryTask;`（行 236）消费。

**修改点 A — 新增卡死检测 state + useEffect**（在 `Discovery.tsx` 行 236 附近）：

```typescript
const task = ctx.discoveryTask;
const [staleWarning, setStaleWarning] = useState(false);

// 卡死检测：任务 running 但 updated_at 2 分钟未变化
useEffect(() => {
  if (!task || task.status !== "running" || !task.updated_at) {
    setStaleWarning(false);
    return;
  }
  const lastUpdate = new Date(task.updated_at).getTime();
  const now = Date.now();
  setStaleWarning(now - lastUpdate > 120000);  // 2 分钟阈值

  // 每 30s 复检一次（与轮询节奏匹配）
  const timer = setInterval(() => {
    const now2 = Date.now();
    setStaleWarning(now2 - lastUpdate > 120000);
  }, 30000);
  return () => clearInterval(timer);
}, [task?.id, task?.status, task?.updated_at]);
```

**修改点 B — UI 显示警告 Alert**（在任务进度展示区域附近）：

```tsx
{staleWarning && (
  <Alert
    type="warning"
    message="任务可能卡死"
    description="已 2 分钟无进度更新，建议点击「取消」后重新开始任务。系统会在 10 分钟后自动中断。"
    showIcon
    style={{ marginBottom: 12 }}
    action={
      <Button size="small" onClick={() => ctx.sendDiscoveryCommand(task.id, "cancel")}>
        取消任务
      </Button>
    }
  />
)}
```

**注意**：需确认 `ctx.sendDiscoveryCommand` 签名与 `task.id` 类型（数字 or 字符串）。实施时读取 AppContext 验证。

---

### 阶段二：白盒测试补强（P0）

#### 2.1 `tests/test_whitebox_akshare_http.py` — 追加 4 用例

**目的**：守护 HTTP timeout 注入逻辑，防止回归。

**追加用例**（在文件末尾追加，不改现有用例）：

| 用例名 | 验证点 |
|--------|--------|
| `test_harden_injects_default_timeout` | `requests.Session().request` 默认 timeout=(5,15)；通过 mock `_orig_request` 验证 kwargs 含 timeout |
| `test_harden_preserves_explicit_timeout` | 调用方传 `timeout=30` → 保留 30，不被覆盖 |
| `test_harden_timeout_env_var_override` | 设置环境变量 `AKSHARE_HTTP_TIMEOUT_CONNECT=10` + `AKSHARE_HTTP_TIMEOUT_READ=30` → `_DEFAULT_TIMEOUT=(10,30)`（需 reimport 模块） |
| `test_call_akshare_with_retry_timeout_raises_no_retry_on_keyerror` | mock func 抛 `KeyError` → 不重试，直接抛出（非瞬时错误） |

**实施要点**：
- `test_harden_injects_default_timeout` 用 `monkeypatch.setattr` 临时替换 `requests.Session._orig_request`（需从 akshare_utils 拿到 `_orig_request` 引用，或在测试中 mock）
- `test_harden_preserves_explicit_timeout` 验证 `kwargs["timeout"]` 保留原值
- `test_harden_timeout_env_var_override` 用 `importlib.reload` 重新加载 akshare_utils 模块
- 第 4 个用例已存在类似逻辑可参考现有用例

#### 2.2 `tests/test_whitebox_async_tasks.py` — 追加 5 用例

**目的**：守护 discovery 任务的超时跳过 + watchdog 心跳 + 24h 窗口边界。

**追加用例**：

| 用例名 | 验证点 |
|--------|--------|
| `test_stale_running_deadline_now_10_minutes` | 断言 `discovery_tasks.STALE_RUNNING_DEADLINE == timedelta(minutes=10)` |
| `test_sync_one_symbol_timeout_constant_is_90` | 断言 `SYNC_ONE_SYMBOL_TIMEOUT_SECONDS == 90` |
| `test_watchdog_heartbeat_constant_is_30` | 断言 `_WATCHDOG_HEARTBEAT_SECONDS == 30` |
| `test_resume_deadline_is_1_day` | 断言 `RESUME_DEADLINE == timedelta(days=1)` |
| `test_terminal_states_constant` | 断言 `_TERMINAL_STATES == ("done","failed","cancelled","expired")` |

**注意**：原计划中的 watchdog 运行时测试（mock 30s 心跳）依赖多线程，**不稳定且慢**。改为**常量值守护测试**——确保关键超时常量不被意外修改。运行时行为由 UAT 0.2 验证。这是更务实的策略。

#### 2.3 `tests/test_whitebox_discovery_timeout.py` — 新建 4 用例

**目的**：集成验证 discovery 任务超时机制。

**新建文件**，用例：

| 用例名 | 验证点 |
|--------|--------|
| `test_sync_one_symbol_with_timeout_raises_on_block` | mock `_sync_one_symbol` 阻塞 → `_sync_one_symbol_with_timeout` 抛 `TimeoutError`（用 `monkeypatch.setattr` 替换 `_sync_one_symbol` 为 `time.sleep(100)`，timeout 调小到 1s） |
| `test_sync_one_symbol_with_timeout_returns_on_success` | mock `_sync_one_symbol` 立即返回 → `_sync_one_symbol_with_timeout` 正常返回结果 |
| `test_watchdog_heartbeat_exits_on_terminal_state` | mock `db.get` 返回 status="done" 的 task → watchdog 线程立即退出（用短 timeout 验证） |
| `test_watchdog_heartbeat_updates_updated_at` | mock db session → watchdog 调用后 `task.updated_at` 被更新（用 `_WATCHDOG_HEARTBEAT_SECONDS=0.1` 临时覆盖） |

**实施要点**：
- 用 `monkeypatch.setattr(discovery_tasks, "SYNC_ONE_SYMBOL_TIMEOUT_SECONDS", 1)` 调小超时
- mock `_sync_one_symbol` 用 lambda + sleep
- watchdog 测试用 `monkeypatch.setattr(discovery_tasks, "_WATCHDOG_HEARTBEAT_SECONDS", 0.1)` 加速
- mock `SessionLocal` 返回内存 session（用 `db_session` fixture 或自建 MagicMock）

---

### 阶段三：回归测试（P0 验证）

#### 3.1 运行回归测试

```bash
pytest -m "not slow" -v
```

**预期**：已有 137 用例 + 新增 13 用例 = 150 用例全部通过。

**关键回归点**（必须验证不破坏）：
- HTTP 加固守护（test_whitebox_akshare_http.py 现有 24 用例）
- universe 刷新守护（test_whitebox_discovery_universe.py 现有 29 用例）
- 接口管理服务层（test_whitebox_api_mgmt.py 现有 42 用例）
- 外部因子（test_whitebox_external_factors.py 现有 31 用例）
- async_tasks 终态保护（test_whitebox_async_tasks.py 现有 11 用例）

---

### 阶段四：黑盒测试（P1）

#### 4.1 `tests/test_blackbox_api_mgmt.py` — 新建 17 用例

**目的**：端到端验证接口管理 API，**重点验证探测端点 30s 超时**。

**前置**：需后端运行。用 `httpx.Client(base_url="http://localhost:8000")` + `/health` 前置检查，失败 `pytest.skip("backend not running")`。

**用例清单**：

| # | 用例名 | 验证点 |
|---|--------|--------|
| 1 | `test_list_apis_zh_returns_all_17` | GET `/external-data/apis?locale=zh-CN` → 17 项 |
| 2 | `test_list_apis_en_returns_english_names` | locale=en-US → 英文名称 |
| 3 | `test_list_apis_contains_required_fields` | 每项含 17 个字段（key/name/category/module/description/default_strategy/enabled/anti_risk_strategy/delay_min_ms/delay_max_ms/last_probe_at/last_probe_success/last_probe_latency_ms/last_probe_error/last_call_at/last_call_success/total_calls/total_failures） |
| 4 | `test_list_apis_locale_fallback` | locale=invalid → 回退 zh-CN（不报错） |
| 5 | `test_list_strategies_returns_five` | GET `/external-data/apis/strategies` → 5 档 |
| 6 | `test_list_strategies_custom_has_null_delay` | custom 档 delay_min_ms/delay_max_ms 为 null |
| 7 | `test_probe_unknown_key_returns_404` | POST `/nonexistent/probe` → 404 |
| 8 | `test_probe_known_key_returns_result` | POST `/stock_info_a_code_name/probe` → ProbeResult 字段完整 |
| 9 | `test_probe_returns_within_30s` | **关键**：探测端点 30s 内必返回（用 `time.time()` 计时） |
| 10 | `test_update_config_unknown_key_returns_404` | PUT `/nonexistent` → 404 |
| 11 | `test_update_config_invalid_strategy_returns_400` | anti_risk_strategy="invalid" → 400 |
| 12 | `test_update_config_custom_min_gt_max_returns_400` | custom + min=2000/max=500 → 400 |
| 13 | `test_update_config_delay_out_of_range_returns_422` | delay_min_ms=70000 → 422 |
| 14 | `test_update_config_negative_delay_returns_422` | delay_min_ms=-1 → 422 |
| 15 | `test_update_config_enabled_takes_effect_immediately` | PUT enabled=False → 缓存即时更新（GET 列表反映） |
| 16 | `test_update_config_strategy_takes_effect_immediately` | PUT strategy=conservative → 缓存更新 |
| 17 | `test_update_config_partial_update` | 仅传 enabled → 其他字段保持原值 |

**关键验证点**：
- 用例 9 `test_probe_returns_within_30s`：**复刻 v3 计划核心验证**。用 `time.time()` 计时，断言 `< 30s`，验证修复 1.2（async + asyncio.wait_for）生效
- 用例 10-14：**不显式设置 Content-Type**（复刻前端 client.ts `updateAkshareApiConfig` 实际行为），验证 FastAPI 是否能正确解析 body —— 守护 v3 计划提到的潜在 bug

**实施要点**：
- `pytest.ini` 已注册 `blackbox` marker，用 `@pytest.mark.blackbox` 标记
- 文件头部加 `pytestmark = pytest.mark.blackbox`
- 用 `httpx.Client` 而非 TestClient（真实 HTTP 调用）

---

### 阶段五：UAT 清单（P1）

#### 5.1 `docs/uat-checklist.md` — 创建 60+ 勾选项

**目的**：手动 UAT 操作清单，**重点验证稳定性修复**。

**结构**（参考 v3 计划阶段三）：

1. **前置准备**：启动后端 + 前端 + 浏览器
2. **稳定性核心验证**（最高优先级，~15 项）
   - 0.1 接口探测不再卡死（7 项）
   - 0.2 机会挖掘全量同步不再卡死（8 项）
3. **接口管理**（~12 项）：显示 + 交互 + 数据
4. **机会挖掘**（~12 项）：显示 + 交互 + 数据
5. **外部数据同步**（~10 项）：显示 + 交互 + 数据
6. **评分配置**（~6 项）：显示 + 交互 + 数据
7. **全局回归**（~6 项）：历史 P0-P3 守护
8. **边界场景**（~10 项）：空数据/网络断开/超时
9. **潜在 Bug 验证**（~2 项）：Content-Type 缺失

**实施要点**：
- 每项用 `- [ ]` Markdown 复选框
- 重点项加粗标注
- 包含具体操作步骤 + 预期结果

---

## 假设与决策

1. **后端修复已完成且正确**：基于代码阅读确认 `_watchdog_heartbeat`、`_sync_one_symbol_with_timeout`、`_patched_request` 已就位。本计划不重新修复后端。
2. **前端卡死检测放 Discovery.tsx 而非 AppContext**：Discovery.tsx 是任务消费方，UI 警告更适合放消费侧。AppContext 只负责轮询数据。
3. **watchdog 运行时测试改为常量守护**：多线程测试不稳定且慢。常量值守护 + UAT 0.2 验证运行时行为，更务实。
4. **黑盒测试不显式设 Content-Type**：复刻前端实际行为，验证潜在 bug。若 FastAPI 解析失败，需修复 client.ts 加 `headers: {"Content-Type": "application/json"}`。
5. **批量探测并发数 3**：平衡速度与风控。17 接口分 6 批，最坏 180s（vs 串行 510s）。
6. **测试不修改业务代码**：所有修复在源码，测试只验证行为。唯一例外是若黑盒测试发现 Content-Type bug，需修复 client.ts。
7. **slow 测试默认跳过**：CI/日常用 `pytest -m "not slow"`，slow 测试手动执行验证 WAF 守护。

---

## 验证步骤

### 步骤 1：前端修复验证
- 启动前端 `cd frontend && npm run dev`
- 接口管理页：单接口探测 30s 内返回
- 批量探测：3 个一组并发，全部 30s 内完成
- 机会挖掘页：任务 running 2 分钟后显示"任务可能卡死"警告

### 步骤 2：白盒测试
```bash
pytest tests/test_whitebox_akshare_http.py tests/test_whitebox_async_tasks.py tests/test_whitebox_discovery_timeout.py -v -m "not slow"
```
预期：新增 4+5+4=13 用例 + 已有用例全通过。

### 步骤 3：全量回归
```bash
pytest -m "not slow" -v
```
预期：150+ 用例全通过，无回归。

### 步骤 4：黑盒测试（需后端运行）
```bash
# 终端 1：启动后端
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# 终端 2：运行黑盒
pytest tests/test_blackbox_api_mgmt.py -v
```
预期：17 用例全通过，重点 `test_probe_returns_within_30s` < 30s。

### 步骤 5：UAT 人工执行
- 启动前后端
- 按 `docs/uat-checklist.md` 逐项勾选
- **重点验证 0.1 探测不再卡死 + 0.2 同步不再卡死**

---

## 实施顺序

1. **前端 P0 修复**（3 文件）：
   - `client.ts` 探测超时 60→30s（1 行改动）
   - `AkshareApiManager.tsx` 批量探测并发 + 防重复（重构 `handleProbeAll` + 提取 `probeOne`）
   - `Discovery.tsx` 卡死检测（新增 state + useEffect + Alert）

2. **白盒测试补强**（3 文件）：
   - `test_whitebox_akshare_http.py` 追加 4 用例
   - `test_whitebox_async_tasks.py` 追加 5 用例
   - 新建 `test_whitebox_discovery_timeout.py` 4 用例

3. **回归测试**：`pytest -m "not slow" -v`

4. **黑盒测试**：新建 `test_blackbox_api_mgmt.py` 17 用例

5. **UAT 清单**：新建 `docs/uat-checklist.md` 60+ 勾选项

6. **若黑盒测试发现 Content-Type bug**：修复 `client.ts` 行 446 添加 `headers: {"Content-Type": "application/json"}`
