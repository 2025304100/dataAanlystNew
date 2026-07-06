# 稳定性修复 + 测试验证计划（v3 聚焦版）

## 核心目标

用户报告两个核心稳定性问题：
1. **第三方接口探测多次没响应** — 点击探测按钮后无响应/卡死
2. **机会挖掘全量同步多次卡在中间** — discovery 任务全量同步阶段卡住不前进

**根因诊断**：整个 akshare 调用链从上到下**没有任何 HTTP timeout 保护**。`requests` 库默认 `timeout=None` 即永久阻塞。当目标数据源（东财/新浪/腾讯/深交所）接受 TCP 连接但不返回响应体时，所有调用都会永久卡死，导致：
- 探测端点耗尽 FastAPI 线程池（默认 40 线程）
- 单 symbol 卡死阻塞整个 discovery 任务
- worker 线程无法被强制中断，即使任务标记 failed 仍后台运行

本计划聚焦**稳定性修复 + 修复后测试验证 + UAT 阶段验证**，覆盖用户 4 个维度：
1. **功能显示正常** — UI 元素、字段、状态正确渲染
2. **功能交互合理** — 操作反馈、即时生效、边界校验、**不再卡死**
3. **数据正确性** — 输入输出、计算逻辑、跨模块数据流
4. **UAT 阶段** — 真实环境人工验证清单

---

## 当前状态分析

### 已有测试基础设施

- pytest + httpx（黑盒）/ SQLAlchemy 直连（白盒）
- `tests/conftest.py` 提供 `db_session` fixture（SQLite 隔离，function scope）
- `pytest.ini` 注册 `slow`/`blackbox`/`whitebox` marker
- 已完成 5 个测试文件 137 个用例（HTTP 加固、universe 刷新、接口管理服务层、外部因子、async_tasks 基础）

### 永久卡死的代码点（必须修复）

| 序号 | 文件:行号 | 代码 | 风险 |
|------|-----------|------|------|
| 1 | `app/api/routes/akshare_apis.py:193` | `result = func(**probe_args)` | 探测端点永久阻塞，耗尽线程池 |
| 2 | `app/services/discovery_tasks.py:789` | `_sync_one_symbol(db, symbol, payload, portfolio_id)` | 单 symbol 卡死阻塞整个任务 |
| 3 | `app/services/market_data.py:221,233,245,257,268,274` | `ak.stock_zh_a_hist()` 等直接调用 | `_fetch_history` 内部无 timeout |
| 4 | `app/services/akshare_utils.py:141` | `result = func(*args, **kwargs)` | `call_akshare_with_retry` 无 timeout |
| 5 | `app/services/akshare_utils.py:48-77` | `_harden_requests_session` | 未注入 requests timeout |
| 6 | `app/services/discovery_tasks.py:340` | `threading.Thread(target=_run_discovery_task, daemon=True)` | worker 线程无法被强制中断 |
| 7 | `app/services/discovery_tasks.py:163-184` | `_expire_stale_tasks` 被动触发 | 依赖前端轮询，无主动 watchdog |

### 现有保护机制（不足）

| 保护层 | 现状 | 不足 |
|--------|------|------|
| HTTP 请求超时 | **无** | akshare 内部 requests 调用全部 `timeout=None` |
| `_harden_requests_session` | 只注入 UA + Connection: close | 未 patch timeout |
| `call_akshare_with_retry` | 重试 3-5 次 + 指数退避 | 每次调用无 timeout，重试放大卡死时间 |
| `_fetch_history` | 3 次重试 + sleep | 每次 akshare 调用无 timeout |
| `STALE_RUNNING_DEADLINE` | 30 分钟被动清理 | 太长 + 依赖前端轮询触发 |
| 前端探测超时 | 60s AbortController | 后端线程仍运行，无法中断 |
| 前端任务轮询 | 2s 间隔 | 无卡死检测，无进度不变提示 |

---

## 提议变更

### 阶段一：稳定性修复（P0 — 必须先做）

#### 1.1 `app/services/akshare_utils.py` — HTTP 层全局 timeout（根治方案）

**目的**：在 `_harden_requests_session` 中 patch `requests.Session.request`，注入默认 timeout，让所有 akshare 内部的 requests 调用都获得超时保护。

**修改点**（行 48-77 附近）：

```python
# 在 _harden_requests_session 中新增 patch
_DEFAULT_TIMEOUT = (5, 15)  # (连接 5s, 读取 15s)

_orig_request = requests.Session.request
def _patched_request(self, method, url, **kwargs):
    if "timeout" not in kwargs or kwargs["timeout"] is None:
        kwargs["timeout"] = _DEFAULT_TIMEOUT
    return _orig_request(self, method, url, **kwargs)
requests.Session.request = _patched_request
```

**效果**：
- 所有 akshare 内部 `requests.get/post` 调用自动获得 (5, 15) timeout
- 探测端点最多阻塞 20s（连接 5s + 读取 15s）
- 单 symbol 同步最多阻塞 20s × 重试次数
- 不需要修改 akshare 源码

**配置化**：timeout 值可从环境变量读取（`AKSHARE_HTTP_TIMEOUT_CONNECT` / `AKSHARE_HTTP_TIMEOUT_READ`），默认 (5, 15)。

#### 1.2 `app/api/routes/akshare_apis.py` — 探测端点超时 + 异步化

**目的**：将 `probe_api` 改为 `async def`，用 `asyncio.wait_for` + `asyncio.to_thread` 包装同步 akshare 调用，加 30s 总体超时。

**修改点**（行 179-210 附近）：

```python
import asyncio

@router.post("/external-data/apis/{api_key}/probe", response_model=ProbeResult)
async def probe_api(api_key: str, db: Session = Depends(get_db)):
    # ... 现有 registry 查找逻辑 ...
    try:
        # 用 asyncio.wait_for 包装同步调用，30s 超时
        result = await asyncio.wait_for(
            asyncio.to_thread(func, **probe_args),
            timeout=30.0,
        )
        # ... 现有成功处理 ...
    except asyncio.TimeoutError:
        # 超时处理：记录 probe 失败
        record_probe_result(db, api_key, success=False, error="probe timeout (30s)", latency_ms=30000)
        return ProbeResult(success=False, error="probe timeout (30s)", latency_ms=30000, ...)
    except Exception as exc:
        # ... 现有异常处理 ...
```

**效果**：
- 探测端点最多 30s 返回（即使 akshare 调用永久阻塞）
- 不阻塞 FastAPI 事件循环
- 超时记录到 DB，前端可见

#### 1.3 `app/services/discovery_tasks.py` — 单 symbol 同步超时 + 主动心跳

**目的**：在 `_run_discovery_task` 的 sync 循环中包装 `_sync_one_symbol`，单 symbol 超时跳过；新增 watchdog 线程检测任务卡死。

**修改点 A — 单 symbol 超时**（行 789 附近）：

```python
import concurrent.futures

# 在 sync 循环中
for index, symbol in enumerate(symbols, start=1):
    # ... 现有进度更新 ...
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_sync_one_symbol, db, symbol, payload, portfolio_id)
            result, scored = future.result(timeout=90)  # 单 symbol 90s 超时
    except concurrent.futures.TimeoutError:
        # 超时跳过，记录失败
        task.failed_count += 1
        task.message = f"symbol {symbol.symbol} 同步超时（90s），跳过"
        _set_task(db, task)
        continue
    # ... 现有成功处理 ...
```

**修改点 B — 缩短 STALE_RUNNING_DEADLINE**（行 59）：

```python
# 从 30 分钟降到 10 分钟
STALE_RUNNING_DEADLINE = timedelta(minutes=10)
```

**修改点 C — 主动心跳 watchdog**（新增到 `_run_discovery_task` 内）：

```python
# 启动 watchdog daemon 线程
def _watchdog(task_id: int, stop_event: threading.Event):
    while not stop_event.wait(timeout=30):
        # 每 30s 检查任务状态
        db = SessionLocal()
        try:
            task = db.get(DiscoveryTask, task_id)
            if task and task.status in ("done", "failed", "cancelled", "expired"):
                return
            # 更新心跳时间（仅更新 updated_at，不改状态）
            task.updated_at = _now()
            db.commit()
        finally:
            db.close()

stop_event = threading.Event()
wd = threading.Thread(target=_watchdog, args=(task.id, stop_event), daemon=True)
wd.start()
try:
    # ... 主循环 ...
finally:
    stop_event.set()
```

**注意**：watchdog 只更新 `updated_at` 防止误判 stale；真正的卡死由单 symbol 超时（90s）兜底。

#### 1.4 `frontend/src/api/client.ts` — 前端超时对齐

**目的**：探测接口超时从 60s 降到 30s（与后端对齐），避免前端长时间等待。

**修改点**（行 441 附近）：

```typescript
probeAkshareApi: (apiKey: string) =>
    requestJson<ProbeResult>(
      `${API}/external-data/apis/${encodeURIComponent(apiKey)}/probe`,
      { method: "POST", timeoutMs: 30000 },  // 从 60000 降到 30000
    ),
```

#### 1.5 `frontend/src/components/AkshareApiManager.tsx` — 批量探测并发 + 防重复

**目的**：批量探测改为有限并发（3 个一组），避免串行卡死；批量探测期间禁用所有单个探测按钮。

**修改点 A — 批量探测并发**（行 88-97 附近）：

```typescript
const handleProbeAll = async () => {
    setProbingAll(true);
    setProbingKeys(new Set(apis.map(a => a.key)));  // 禁用所有单个按钮
    try {
        // 3 个一组并发，避免串行卡死
        const CONCURRENCY = 3;
        for (let i = 0; i < apis.length; i += CONCURRENCY) {
            const batch = apis.slice(i, i + CONCURRENCY);
            await Promise.allSettled(batch.map(a => handleProbe(a.key)));
        }
    } finally {
        setProbingAll(false);
        setProbingKeys(new Set());
    }
};
```

**修改点 B — 单个探测按钮禁用条件**（行 311 附近）：

```typescript
loading={probingKeys.has(r.key) || probingAll}  // 批量探测时全部禁用
disabled={probingAll}  // 批量探测时禁用
```

#### 1.6 `frontend/src/components/Discovery.tsx` — 任务卡死检测提示

**目的**：前端检测 `updated_at` 长时间未变化时显示"任务可能卡死"提示。

**修改点**（在任务轮询逻辑附近）：

```typescript
// 新增卡死检测状态
const [staleWarning, setStaleWarning] = useState(false);

// 在轮询回调中
if (task && task.status === "running") {
    const lastUpdate = new Date(task.updated_at).getTime();
    const now = Date.now();
    setStaleWarning(now - lastUpdate > 120000);  // 2 分钟无更新提示
}

// UI 显示
{staleWarning && (
    <Alert
        type="warning"
        message="任务可能卡死，已 2 分钟无进度更新"
        description="建议点击「取消」后重新开始任务"
        showIcon
    />
)}
```

---

### 阶段二：测试验证（P0 — 修复后立即执行）

#### 2.1 补充白盒测试：`tests/test_whitebox_akshare_http.py`（追加）

**目的**：守护 HTTP timeout 注入。

**新增用例**：

| 用例 | 验证点 |
|------|--------|
| `test_harden_injects_default_timeout` | patch 后 `requests.Session().request` 默认 timeout=(5, 15) |
| `test_harden_preserves_explicit_timeout` | 调用方显式传 timeout=30 → 保留 30，不被覆盖 |
| `test_call_akshare_with_retry_timeout_raises` | mock func 抛 `requests.exceptions.ReadTimeout` → 重试或失败记录 |
| `test_call_akshare_with_retry_timeout_no_retry_on_connect_timeout` | 连接超时视为瞬时错误，重试 |

#### 2.2 补充白盒测试：`tests/test_whitebox_async_tasks.py`（追加）

**目的**：守护单 symbol 超时 + watchdog 心跳。

**新增用例**：

| 用例 | 验证点 |
|------|--------|
| `test_sync_one_symbol_timeout_skips_and_continues` | mock `_sync_one_symbol` 阻塞 100s → 90s 超时跳过，任务继续下一个 symbol |
| `test_watchdog_updates_heartbeat` | watchdog 线程每 30s 更新 `updated_at`，防止误判 stale |
| `test_stale_running_deadline_now_10_minutes` | 验证 `STALE_RUNNING_DEADLINE = timedelta(minutes=10)`（从 30 降到 10） |
| `test_discovery_task_cancelled_not_overwritten_by_worker` | 任务 cancelled → worker 尝试更新 → 状态保持 cancelled |
| `test_discovery_task_resume_expired_after_24h` | paused_at = now-24h01m → resume 时标记 expired |

#### 2.3 新增黑盒测试：`tests/test_blackbox_api_mgmt.py`（待创建）

**目的**：接口管理 API 端点黑盒测试，**重点验证探测端点超时**。

**前置**：需要运行后端（`httpx.Client(base_url="http://localhost:8000")` + `/health` 前置检查，失败 `pytest.skip`）。

**测试用例**（17 个）：

| 用例 | 验证点 |
|------|--------|
| `test_list_apis_zh_returns_all_17` | GET `/external-data/apis?locale=zh-CN` → 17 项 |
| `test_list_apis_en_returns_english_names` | locale=en-US → 英文名称 |
| `test_list_apis_contains_required_fields` | 每项含 17 个字段 |
| `test_list_apis_locale_fallback` | locale=invalid → 回退 zh-CN |
| `test_list_strategies_returns_five` | GET `/external-data/apis/strategies` → 5 档 |
| `test_list_strategies_custom_has_null_delay` | custom 档 delay 为 null |
| `test_probe_unknown_key_returns_404` | POST `/nonexistent/probe` → 404 |
| `test_probe_known_key_returns_result` | POST `/stock_info_a_code_name/probe` → ProbeResult 字段完整 |
| `test_probe_returns_within_30s` | **关键**：探测端点 30s 内必返回（即使 akshare 卡死） |
| `test_update_config_unknown_key_returns_404` | PUT `/nonexistent` → 404 |
| `test_update_config_invalid_strategy_returns_400` | anti_risk_strategy="invalid" → 400 |
| `test_update_config_custom_min_gt_max_returns_400` | custom + min>max → 400 |
| `test_update_config_delay_out_of_range_returns_422` | delay_min_ms=70000 → 422 |
| `test_update_config_negative_delay_returns_422` | delay_min_ms=-1 → 422 |
| `test_update_config_enabled_takes_effect_immediately` | PUT enabled=False → 缓存即时更新 |
| `test_update_config_strategy_takes_effect_immediately` | PUT strategy=conservative → 缓存更新 |
| `test_update_config_partial_update` | 仅传 enabled → 其他字段保持原值 |

**关键验证点**：
- 用 `httpx.Client` 真实 HTTP 调用，**不显式设置 Content-Type**（复刻前端 client.ts 实际行为），验证 FastAPI 是否能正确解析 body（守护潜在 bug）
- `test_probe_returns_within_30s` 用 `time.time()` 计时，断言 `< 30s`（验证修复 1.2 生效）

#### 2.4 新增集成测试：`tests/test_whitebox_discovery_timeout.py`（待创建）

**目的**：验证 discovery 任务在单 symbol 卡死时能继续推进，不永久阻塞。

**测试用例**：

| 用例 | 验证点 |
|------|--------|
| `test_discovery_sync_skips_timeout_symbol` | mock `_sync_one_symbol` 阻塞 100s → 任务在 90s 超时后跳过，继续处理下一个 symbol |
| `test_discovery_sync_records_failed_on_timeout` | 超时的 symbol 计入 failed_count，message 含"同步超时" |
| `test_discovery_watchdog_updates_heartbeat` | watchdog 线程运行时 `updated_at` 每 30s 前进 |
| `test_discovery_stale_deadline_10_minutes` | running 任务 10 分钟后变为 failed（验证常量值） |

---

### 阶段三：UAT 手动验证清单（P1）

#### 3.1 `docs/uat-checklist.md`（待创建）

**目的**：手动 UAT 操作清单，**重点验证稳定性修复**。

**结构**：

```markdown
# UAT 手动测试清单（v3 稳定性聚焦）

## 前置准备
- 启动后端：python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
- 启动前端：cd frontend && npm run dev
- 浏览器打开 http://localhost:5173

## 0. 稳定性核心验证（最高优先级）
### 0.1 接口探测不再卡死
- [ ] 点击单接口「探测」→ 30s 内必返回（成功或失败）
- [ ] 即使数据源不响应，探测在 30s 内超时返回错误
- [ ] 探测超时时 Alert 显示"probe timeout (30s)"
- [ ] 探测超时后 DB 记录 last_probe_success=false, last_probe_error="probe timeout"
- [ ] 点击「批量探测」→ 3 个一组并发，全部 30s 内完成
- [ ] 批量探测期间所有单个探测按钮禁用
- [ ] 批量探测期间可点击「取消」中止（待评估，可选）
### 0.2 机会挖掘全量同步不再卡死
- [ ] 创建 cn-stock 全量同步任务（symbol_limit=10000）
- [ ] 任务运行中，单 symbol 卡死时 90s 后跳过，继续下一个
- [ ] 卡死的 symbol 计入 failed_count，message 含"同步超时"
- [ ] 任务进度条持续前进，不永久停滞
- [ ] watchdog 心跳：updated_at 每 30s 前进（前端轮询可见）
- [ ] 任务卡死 2 分钟后前端显示"任务可能卡死"警告 Alert
- [ ] 任务 running 超 10 分钟（STALE_RUNNING_DEADLINE）→ 自动标记 failed
- [ ] 点击「取消」→ 任务状态变 cancelled，worker 不再更新

## 1. 接口管理（设置 → 接口管理）
### 1.1 显示正常
- [ ] 表格显示 17 行接口记录（9 列）
- [ ] 分类 Tag 显示正确
- [ ] 状态列：未探测=灰、成功=绿、失败=红、禁用=灰
- [ ] 策略列 Select 下拉含 5 选项
- [ ] 延时列非 custom 显示固定区间；custom 显示两个 InputNumber（min=0, max=60000）
- [ ] Card header 含「批量探测」「刷新」按钮
### 1.2 交互正常
- [ ] Switch 启用/禁用 → 立即提交，Toast 成功
- [ ] 策略切换为「保守」→ 立即提交，延时显示「1000-2000ms」
- [ ] 策略切换为「自定义」→ 显示 InputNumber + 保存按钮
- [ ] 自定义 min=500/max=2000 → 成功
- [ ] 自定义 min=2000/max=500 → 400 错误
- [ ] 单接口探测 → loading → 30s 内反馈
- [ ] 批量探测 → 并发（3 个一组）→ 全部 30s 内完成
### 1.3 数据正确
- [ ] 禁用接口后评分流程跳过
- [ ] 探测结果 latency_ms/error 正确显示
- [ ] 累计调用数/失败数递增

## 2. 机会挖掘（机会挖掘 Tab）
### 2.1 显示正常
- [ ] 任务列表表格列完整
- [ ] 标的列含 code + name + 维度强项标签
- [ ] 维度得分 Tooltip 颜色按分数区分
- [ ] 步骤指示器 5 步（prepare → sync → scan → news → done）
- [ ] 候选池 Tab 颜色：all 灰/highQuality 绿/highTiming 蓝/actionable 橙/overheatRisk 红
### 2.2 交互正常
- [ ] 创建任务（scope=cn-etf, limit=5）→ queued → running → done
- [ ] 暂停 → 状态 paused，message="任务已暂停，1天内可继续"
- [ ] 恢复（24h 内）→ 继续 processing
- [ ] 恢复（超 24h）→ 标记 expired
- [ ] 取消 → 状态 cancelled
- [ ] 重试 → 重新 queued
- [ ] **卡死检测**：任务 2 分钟无进度 → 显示"任务可能卡死"警告
### 2.3 数据正确
- [ ] 任务完成后 total/processed/ok/failed/empty/scored/executable/cleanup 数字合理
- [ ] **关键回归**：cn-stock 任务 universe 拉取成功（不报 "Excel file format" 错误）
- [ ] 候选列表含评分明细
- [ ] 综合机会分 = 各维度加权计算结果

## 3. 外部数据同步（设置 → 外部数据）
### 3.1 显示正常
- [ ] 顶部数据源下拉 3 选项（观察池/持仓/全部）
- [ ] 北向复选框默认勾选（仅资金流生效）
- [ ] 3 个同步卡片（基本面/资金流/ETF 指标）
- [ ] 结果 Alert 显示 total/success/skipped/failed
### 3.2 交互正常
- [ ] 同步股质 → loading → 结果 Alert
- [ ] 同步资金流 → loading → 结果 Alert（含北向）
- [ ] 同步 ETF 指标 → loading → 结果 Alert
- [ ] 切换数据源 → 后续同步用新源
### 3.3 数据正确
- [ ] 同步结果数字合理
- [ ] ETF 同步跳过 stock（skipped+1）
- [ ] 股质同步跳过 ETF（skipped+1）
- [ ] 空 watchlist 同步 → total=0，不报错
- [ ] **稳定性**：单个 symbol 卡死时 90s 超时跳过，继续下一个

## 4. 评分配置（设置 → 评分配置）
### 4.1 显示正常
- [ ] 预设列表可见（4 个股票 + 5 个 ETF）
- [ ] 激活预设高亮
- [ ] 维度配置表显示趋势/动量/波动/流动性/题材
### 4.2 交互正常
- [ ] 切换激活预设 → Toast 成功
- [ ] 编辑维度权重 → 保存成功
### 4.3 数据正确
- [ ] 评分详情显示维度分 + 外部因子分
- [ ] 外部因子缺失时按 policy 处理（neutral=50/penalty=30）

## 5. 全局回归（历史 P0-P3）
### 5.1 显示正常
- [ ] 工作台 K 线图加载
- [ ] 候选池表格渲染
- [ ] 评分看板数字显示
### 5.2 交互正常
- [ ] 切换语言（中/英）→ 全部文案更新
- [ ] 切换市场（CN/US）→ 标的列表刷新
- [ ] 模拟交易买入/卖出 → 持仓更新
### 5.3 数据正确
- [ ] 胜率计算不出现 Infinity（除零保护）
- [ ] 回测结果含总收益/最大回撤/胜率/交易次数
- [ ] AST 沙箱拒绝 `2**99999`（Pow 限制）

## 6. 边界场景
- [ ] 空观察池同步 → total=0，不报错
- [ ] 空持仓同步 → total=0
- [ ] 网络断开时同步 → failed 递增，30s 内必返回（不卡死）
- [ ] 任务暂停超 24h → 标记 expired
- [ ] 任务 running 超 10 分钟 → 自动 failed（stale 清理，从 30min 降到 10min）
- [ ] 接口管理 custom 模式 min=0/max=60000 → 成功（边界值）
- [ ] 接口管理 delay_min_ms=60001 → 422 错误
- [ ] 机会挖掘 symbol_limit=10001 → 422 错误
- [ ] 机会挖掘 min_score=101 → 422 错误

## 7. 潜在 Bug 验证
- [ ] **前端 Content-Type 验证**：接口管理切换策略/启用禁用 → 提交成功（验证 updateAkshareApiConfig 缺少 Content-Type 是否影响 FastAPI 解析）
- [ ] 若上述失败 → 修复 client.ts 行 446 添加 `headers: { "Content-Type": "application/json" }`
```

---

## 回归守护映射表

每个历史 bug 必须有对应测试用例守护。

| 历史 Bug | 根因 | 守护测试 | 状态 |
|---------|------|---------|------|
| Referer 全局注入破坏深交所接口 | `_harden_requests_session` 注入 Referer | `test_harden_does_not_inject_referer` + slow 联网 | ✅ 已守护 |
| HTTP timeout 缺失导致永久卡死 | `_harden_requests_session` 未注入 timeout | `test_harden_injects_default_timeout` + `test_probe_returns_within_30s` | ⬜ 待实施 |
| 探测端点永久阻塞耗尽线程池 | `probe_api` 同步调用无超时 | `test_probe_returns_within_30s` | ⬜ 待实施 |
| 单 symbol 卡死阻塞整个任务 | `_sync_one_symbol` 无超时包装 | `test_sync_one_symbol_timeout_skips_and_continues` | ⬜ 待实施 |
| STALE_RUNNING_DEADLINE 太长（30min） | 30 分钟才清理卡死任务 | `test_stale_running_deadline_now_10_minutes` | ⬜ 待实施 |
| 脏数据兜底遗漏 | `get_or_sync_*` 返回 None | `test_compute_external_factors_none_score_not_included` | ✅ 已守护 |
| Lock vs RLock 死锁 | `market_data._proxy_lock = Lock()` | `test_proxy_lock_is_reentrant` | ✅ 已守护 |
| 异步任务终态被 worker 覆盖 | cancelled/failed 被旧 worker 标记 done | `test_set_task_protects_cancelled` + 补强用例 | ✅（基础）+ ⬜（补强） |
| 暂停超 24h 仍可恢复 | `can_resume` 计算错误 | 补强 `test_discovery_task_resume_expired_after_24h` | ⬜ 补强 |
| 前端 Content-Type 缺失 | `updateAkshareApiConfig` 未设置 Content-Type | UAT 7 + 黑盒 `test_update_config_*` | ⬜ 验证 |
| 批量探测串行卡死 | `handleProbeAll` 串行 await | UAT 0.1 + 代码审查 | ⬜ 验证 |

---

## 边界测试矩阵

### 稳定性边界（新增）

| 边界场景 | 输入 | 预期输出 | 守护方式 |
|---------|------|---------|---------|
| 数据源接受连接不响应 | mock socket.recv 永久阻塞 | 15s 读取超时抛 ReadTimeout | `test_harden_injects_default_timeout` |
| 数据源连接超时 | mock socket.connect 阻塞 | 5s 连接超时抛 ConnectTimeout | 白盒 |
| 探测端点 30s 超时 | mock func 阻塞 > 30s | asyncio.wait_for 超时返回 | `test_probe_returns_within_30s` |
| 单 symbol 90s 超时 | mock `_sync_one_symbol` 阻塞 100s | 90s 超时跳过，继续下一个 | `test_sync_one_symbol_timeout_skips_and_continues` |
| watchdog 心跳 30s | watchdog 线程运行 | updated_at 每 30s 前进 | `test_watchdog_updates_heartbeat` |
| STALE_RUNNING_DEADLINE | running 超 10 分钟 | 自动 failed | `test_stale_running_deadline_now_10_minutes` |
| 前端卡死检测 2 分钟 | updated_at 2 分钟未变 | 显示"任务可能卡死"警告 | UAT 0.2 |

### 接口管理边界

| 边界场景 | 输入 | 预期输出 | 守护方式 |
|---------|------|---------|---------|
| 策略档位无效 | `anti_risk_strategy="invalid"` | 400 错误 | 黑盒 |
| custom 模式 min=max | `min=500, max=500` | 成功 | 白盒 ✅ |
| custom 模式 min>max | `min=2000, max=500` | 400 错误 | 黑盒 + 白盒 ✅ |
| custom 模式 max=60000 | `min=59000, max=60000` | 成功（le=60000） | 黑盒 |
| custom 模式 max=60001 | `min=0, max=60001` | 422 错误 | 黑盒 |
| delay 为负数 | `delay_min_ms=-1` | 422 错误 | 黑盒 |
| api_key 不存在 | `PUT /nonexistent` | 404 | 黑盒 |
| 部分更新 | 只传 enabled | 其他字段保持原值 | 黑盒 |

### 机会挖掘边界

| 字段 | ge | le | 默认 | 边界测试 |
|------|----|----|------|---------|
| `min_score` | 0 | 100 | 55 | min_score=0/100/-1/101 |
| `symbol_limit` | 1 | 10000 | None | symbol_limit=1/10000/10001 |
| `batch_size` | 1 | 100 | 20 | batch_size=1/100/101 |
| `delay_seconds` | 0 | 5 | 0.25 | delay_seconds=0/5/6 |
| `news_limit` | 0 | 100 | 30 | news_limit=0/100/101 |
| `warning_days` | 1 | 60 | 3 | warning_days=1/60/0/61 |
| `valid_days` | 1 | 365 | 5 | valid_days=1/365/0/366 |

### 异步任务边界

| 边界场景 | 输入 | 预期输出 | 守护方式 |
|---------|------|---------|---------|
| 暂停 23h59m 后恢复 | paused_at = now-23h59m | can_resume=True | ⬜ 补强 |
| 暂停 24h00m 后恢复 | paused_at = now-24h00m | can_resume=True（`<=`） | ⬜ 补强 |
| 暂停 24h01m 后恢复 | paused_at = now-24h01m | can_resume=False, expired | ⬜ 补强 |
| running 超 10min | updated_at = now-11min | 标记 failed | ⬜ 新增 |
| running 9min | updated_at = now-9min | 不视为 stale | ⬜ 新增 |

---

## 测试数据准备策略

### 共享 Fixture（已在 conftest.py 实现）

- `tmp_sqlite_url`（function scope）— 每个测试独立 SQLite 文件
- `db_session`（function scope）— 全新 DatabaseManager + 表创建
- 15 个模型显式导入修复 `NoReferencedTableError`

### Mock 副作用隔离

- 每个测试函数独立 SQLite
- monkeypatch 自动还原
- `_config_cache` 全局字典清理
- slow 测试不 mock，用 `@pytest.mark.slow` 隔离

---

## 测试执行顺序与依赖

```
┌─────────────────────────────────────────────────────────────┐
│ 阶段 0: 稳定性修复（P0 必须先做）                            │
│ 1. akshare_utils.py: HTTP 层全局 timeout                    │
│ 2. akshare_apis.py: 探测端点 async + 30s 超时               │
│ 3. discovery_tasks.py: 单 symbol 90s 超时 + watchdog        │
│ 4. client.ts: 前端探测超时 60s → 30s                        │
│ 5. AkshareApiManager.tsx: 批量探测并发 + 防重复              │
│ 6. Discovery.tsx: 卡死检测提示                              │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 阶段 1: 白盒测试（无网络，无后端）                           │
│ pytest tests/test_whitebox_akshare_http.py -m "not slow"   │
│ pytest tests/test_whitebox_async_tasks.py                  │
│ pytest tests/test_whitebox_discovery_timeout.py            │
│ + 已有 137 用例回归                                         │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 阶段 2: 黑盒 API（需后端运行）                              │
│ pytest tests/test_blackbox_api_mgmt.py                     │
│ 重点: test_probe_returns_within_30s                        │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 阶段 3: slow 联网（手动，验证 WAF 守护）                     │
│ pytest tests/test_whitebox_akshare_http.py -m slow         │
│ pytest tests/test_whitebox_discovery_universe.py -m slow   │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 阶段 4: UAT 人工验证（前端浏览器）                          │
│ 按 docs/uat-checklist.md 逐项勾选                          │
│ 重点: 0.1 探测不再卡死 + 0.2 同步不再卡死                  │
└─────────────────────────────────────────────────────────────┘
```

---

## 假设与决策

1. **HTTP timeout 默认值 (5, 15)**：连接 5s + 读取 15s = 单次请求最多 20s。重试 3 次最多 60s+退避。可从环境变量覆盖。
2. **探测端点 30s 超时**：覆盖单次 akshare 调用（20s）+ 余量。`asyncio.wait_for` + `asyncio.to_thread` 实现。
3. **单 symbol 90s 超时**：覆盖 `_fetch_history` 3 次重试（每次 20s + 退避）+ 余量。`concurrent.futures.ThreadPoolExecutor` + `future.result(timeout=90)`。
4. **STALE_RUNNING_DEADLINE 从 30min 降到 10min**：卡死任务更快被清理。但配合 watchdog 心跳，正常任务不会误判。
5. **watchdog 只更新 updated_at**：不改状态，避免与 worker 冲突。真正卡死由单 symbol 超时兜底。
6. **批量探测并发数 3**：避免串行卡死，也避免并发过高触发风控。
7. **前端卡死检测阈值 2 分钟**：比单 symbol 超时（90s）略长，避免误报。
8. **测试不修改业务代码**：所有修复在源码，测试只验证行为。
9. **slow 测试默认跳过**：CI/日常运行用 `pytest -m "not slow"`。
10. **黑盒测试需后端运行**：`httpx.Client` + `/health` 前置检查，失败 `pytest.skip`。
11. **UAT 清单零依赖**：Markdown 格式可打印勾选。
12. **补强现有测试文件**：`test_whitebox_async_tasks.py` 用 Edit 追加用例，不重写。
13. **修复后必须验证 137 个已有用例不回归**：`pytest -m "not slow" -v` 全部通过。

---

## 验证步骤

### 修复完成后验证

1. **稳定性修复验证**（P0）：
   ```bash
   # 启动后端
   python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
   # 探测一个不存在/卡死的接口，验证 30s 内返回
   time curl -X POST http://localhost:8000/external-data/apis/stock_info_sz_name_code/probe
   ```
   预期：30s 内返回（即使数据源卡死）。

2. **白盒测试**（修复后必须全部通过）：
   ```bash
   pytest tests/test_whitebox_akshare_http.py tests/test_whitebox_async_tasks.py tests/test_whitebox_discovery_timeout.py -v
   ```
   预期：新增 4+5+4=13 个用例 + 已有 137 用例全部通过。

3. **已有用例回归**（修复不能破坏现有功能）：
   ```bash
   pytest -m "not slow" -v
   ```
   预期：全部通过。

4. **黑盒 API 测试**（需后端运行）：
   ```bash
   pytest tests/test_blackbox_api_mgmt.py -v
   ```
   预期：17 个用例全部通过，重点 `test_probe_returns_within_30s` < 30s。

5. **slow 联网测试**（手动，验证 WAF + timeout 守护）：
   ```bash
   pytest tests/test_whitebox_akshare_http.py tests/test_whitebox_discovery_universe.py -m slow -v
   ```
   预期：真实 akshare 接口可达，timeout 生效。

6. **UAT 清单人工执行**：
   - 启动前后端
   - 按 `docs/uat-checklist.md` 逐项勾选
   - **重点验证 0.1 探测不再卡死 + 0.2 同步不再卡死**

7. **回归守护验证**（关键）：
   - 临时移除 `_harden_requests_session` 的 timeout patch → `test_harden_injects_default_timeout` 应失败
   - 加回 patch → 测试应通过
   - 这证明测试能守护 timeout 不再缺失

---

## 工作量预估

| 任务 | 类型 | 状态 |
|------|------|------|
| **阶段一：稳定性修复** | | |
| akshare_utils.py HTTP timeout patch | 代码修改 | ⬜ P0 |
| akshare_apis.py 探测端点 async + 30s 超时 | 代码修改 | ⬜ P0 |
| discovery_tasks.py 单 symbol 90s 超时 + watchdog | 代码修改 | ⬜ P0 |
| discovery_tasks.py STALE_RUNNING_DEADLINE 30→10min | 代码修改 | ⬜ P0 |
| client.ts 前端探测超时 60→30s | 代码修改 | ⬜ P0 |
| AkshareApiManager.tsx 批量探测并发 + 防重复 | 代码修改 | ⬜ P0 |
| Discovery.tsx 卡死检测提示 | 代码修改 | ⬜ P0 |
| **阶段二：测试验证** | | |
| test_whitebox_akshare_http.py 追加 4 用例 | 测试 | ⬜ P0 |
| test_whitebox_async_tasks.py 追加 5 用例 | 测试 | ⬜ P0 |
| test_whitebox_discovery_timeout.py 新增 4 用例 | 测试 | ⬜ P0 |
| test_blackbox_api_mgmt.py 新增 17 用例 | 测试 | ⬜ P1 |
| **阶段三：UAT 清单** | | |
| docs/uat-checklist.md 创建 60+ 勾选项 | 文档 | ⬜ P1 |
| **合计** | **7 代码修改 + 30 测试用例 + 60+ UAT 项** | |

---

## 优先级

1. **P0**（必须先做）：稳定性修复 7 处代码修改 — 解决卡死根因
2. **P0**：白盒测试追加 13 用例 — 守护修复
3. **P1**：黑盒测试 17 用例 — 端到端验证
4. **P1**：UAT 清单 60+ 项 — 人工验证
5. **P2**：全量回归测试 — `pytest -m "not slow" -v`

---

## 实施顺序

1. **先修复后端稳定性**（P0 代码修改）：
   - `akshare_utils.py` HTTP timeout patch（根治）
   - `akshare_apis.py` 探测端点 async + 30s 超时
   - `discovery_tasks.py` 单 symbol 90s 超时 + watchdog + STALE 10min

2. **再修复前端保护**（P0 代码修改）：
   - `client.ts` 前端探测超时 60→30s
   - `AkshareApiManager.tsx` 批量探测并发 + 防重复
   - `Discovery.tsx` 卡死检测提示

3. **补强白盒测试**（P0 测试）：
   - `test_whitebox_akshare_http.py` 追加 4 用例（timeout 守护）
   - `test_whitebox_async_tasks.py` 追加 5 用例（超时 + watchdog + 24h 窗口）
   - 新建 `test_whitebox_discovery_timeout.py` 4 用例（集成验证）

4. **运行回归测试**：`pytest -m "not slow" -v` 确保 137+13 用例全通过

5. **创建黑盒测试**（P1）：`test_blackbox_api_mgmt.py` 17 用例

6. **创建 UAT 清单**（P1）：`docs/uat-checklist.md` 60+ 勾选项

7. **手动 UAT 验证**：启动前后端，按清单逐项勾选，重点验证 0.1 + 0.2
