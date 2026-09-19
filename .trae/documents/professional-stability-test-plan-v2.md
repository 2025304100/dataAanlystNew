# 专业化稳定性测试与修复计划 v2

> **承接前序工作**：v1 计划已实现 5 层 timeout 防护（HTTP/单symbol/探测/任务/前端），但用户反馈"修复后仍卡住"。
> **本次范围**：诊断根因 + 修复 3 个致命失效点 + 4 维度全面测试补强 + UAT。
> **核心目标**：让 akshare 探测和全量同步真正不卡死，且问题可观测、可诊断、可恢复。

---

## 一、当前状态分析（Phase 1 探索结论）

### 1.1 已完成的修复（v1）

| 层级 | 文件 | 状态 |
|------|------|------|
| L1 HTTP 层 | `app/services/akshare_utils.py` `_harden_requests_session` 注入 `(5,15)` timeout | ✅ 生效 |
| L2 单 symbol | `app/services/discovery_tasks.py` `_sync_one_symbol_with_timeout` 90s | ✅ 生效（但仅限 sync 循环内） |
| L3 探测端点 | `app/api/routes/akshare_apis.py` `asyncio.wait_for` 30s | ✅ 生效（但子线程泄漏） |
| L4 任务级 | `discovery_tasks.py` watchdog 30s 心跳 + 10min stale 清理 | ⚠️ 心跳反而掩盖卡死 |
| L5 前端 | `client.ts` 30s timeoutMs + `Discovery.tsx` 2min Alert | ✅ 生效 |

### 1.2 排查发现的 3 个致命失效点

#### 失效点 1：universe 刷新路径完全无 timeout 保护（最高概率）

**位置**：`app/services/discovery_tasks.py:819-826`（prepare 阶段）

```
_run_discovery_task
  └─ _refresh_discovery_universe(db, payload)   ← 无 _sync_one_symbol_with_timeout 包装！
       ├─ _refresh_cn_stock_universe
       │    └─ _cn_stock_universe_frame
       │         └─ call_akshare_with_retry(ak.stock_info_a_code_name, max_attempts=2)
       │              └─ time.sleep 重试，无 timeout，akshare 内部 pd.read_excel 永久阻塞
       └─ _refresh_cn_etf_universe
            ├─ ak.fund_etf_spot_em()              ← 完全无保护直接调用！
            └─ ak.fund_etf_category_sina()        ← 备用源同样无保护！
```

**后果**：
- 任务卡在 `stage="prepare"`, `percent=8%`，永不超时
- watchdog 每 30s 更新 `updated_at` → `_expire_stale_tasks`（10min 阈值）永不触发
- 完美匹配用户描述"全量同步多次卡在中间"

#### 失效点 2：探测端点子线程泄漏耗尽线程池（高概率）

**位置**：`app/api/routes/akshare_apis.py:209-212`

```python
result = await asyncio.wait_for(
    asyncio.to_thread(_run_probe, func, probe_args),  # 超时后子线程无法 kill
    timeout=_PROBE_TIMEOUT_SECONDS,
)
```

**后果**：
- `asyncio.wait_for` 超时只取消 await，子线程继续运行
- 若 akshare 走 `pd.read_excel` 路径，子线程永久泄漏
- anyio 默认线程池 40 线程，多次"探测全部"后累积耗尽
- 后续所有 `asyncio.to_thread` 调用排队 → "探测多次没响应"

#### 失效点 3：_fetch_history 多层回退总耗时远超 90s（中概率）

**位置**：`app/services/market_data.py:206-286`

- 3 个数据源 × 3 次重试 = 最多 9 次 akshare 调用
- 每次 20s（patched timeout），9 次 = 180s
- `_sync_one_symbol_with_timeout` 仅 90s → 必然超时
- 超时后子线程继续跑剩余回退，泄漏线程 + 占用 db 连接

### 1.3 日志观测性严重缺失

| 位置 | 期望日志 | 实际 |
|------|----------|------|
| `probe_api` | 开始/结束/超时 | **完全没有 logger 调用** |
| `_sync_one_symbol_with_timeout` | 进入/超时/完成 | **完全没有 logger 调用** |
| `_watchdog_heartbeat` | 心跳/退出/失败 | **完全没有 logger 调用，异常被 `pass` 静默吞** |
| `call_akshare_with_retry` 重试 | 重试事件 | DEBUG 级别，`main.py:18` 默认 INFO 不输出 |
| `_fetch_history` 数据源切换 | 源切换 | DEBUG 级别，默认不输出 |

**结果**：用户报告"卡住"时，日志中几乎没有任何线索。

---

## 二、阶段 1：日志增强与根因诊断（P0，先做）

> **目的**：让问题可观测，再用真实环境验证根因，避免盲目修复。

### 2.1 补全关键日志点（10 处）

#### P0 日志（必须立即添加）

| # | 文件 | 行号 | 级别 | 日志内容 |
|---|------|------|------|----------|
| L1 | `app/api/routes/akshare_apis.py` | 207 后 | INFO | `probe %s start` |
| L2 | `app/api/routes/akshare_apis.py` | 213 后 | INFO | `probe %s done: success=%s latency=%dms` |
| L3 | `app/api/routes/akshare_apis.py` | 223 except | WARNING | `probe %s TIMEOUT after %ds` |
| L4 | `app/api/routes/akshare_apis.py` | 230 except | WARNING | `probe %s EXC: %s` |
| L5 | `app/services/discovery_tasks.py` | 775 with 前 | INFO | `sync_one %s start (timeout=%ds)` |
| L6 | `app/services/discovery_tasks.py` | 852 except | WARNING | `sync_one %s TIMEOUT after %ds` |
| L7 | `app/services/discovery_tasks.py` | 751-772 | DEBUG/INFO | watchdog 心跳 + 退出 + 失败（替换 `except: pass`） |
| L8 | `app/services/discovery_tasks.py` | 426/476 入口 | INFO | `refresh cn-stock/etf universe start` |
| L9 | `app/services/discovery_tasks.py` | 426/476 返回前 | INFO | `refresh universe done: seen=%d created=%d` |
| L10 | `app/services/discovery_tasks.py` | 175-196 | WARNING | `_expire_stale_tasks` 触发时记录 |

#### P1 日志（强烈建议）

| # | 文件 | 行号 | 改动 |
|---|------|------|------|
| L11 | `app/services/akshare_utils.py` | 201-208 | `logger.debug` → `logger.info`（重试事件默认可见） |
| L12 | `app/services/market_data.py` | 230,243,266,278 | `logger.debug` → `logger.info`（数据源切换可见） |
| L13 | `app/services/discovery_tasks.py` | 849 try 前 | INFO `discovery %s: sync %s (%d/%d)` |

### 2.2 临时提升日志级别

- `app/main.py:18` 临时改为 `level=logging.DEBUG`
- 验证完根因后恢复 INFO

### 2.3 真实环境根因验证（4 选 1）

| 方法 | 命令 | 适用场景 |
|------|------|----------|
| py-spy 抓栈 | `py-spy dump --pid <python_pid>` | 卡住时直接看线程栈，最精准 |
| 线程池监控 | probe_api 入口记录 `anyio.to_thread.current_default_thread_limiter().borrowed_tokens` | 验证线程池耗尽 |
| 浏览器 DevTools | Network 面板观察请求频率 + 响应时间 | 验证前端轮询行为 |
| akshare 路径验证 | `python -c "import inspect; print(inspect.getsourcefile(ak.stock_info_a_code_name))"` | 确认是否走 pd.read_excel |

**验收**：能从日志/栈中明确看到卡在哪个 akshare 调用、是否在 `pd.read_excel` / `urlopen` / `socket.recv`。

---

## 三、阶段 2：3 个致命失效点修复（P0）

### 3.1 修复失效点 1：universe 刷新路径加 timeout 包装

**文件**：`app/services/discovery_tasks.py`

**改动 1**：新增 `UNIVERSE_REFRESH_TIMEOUT_SECONDS = 120` 常量（行 66 附近）

**改动 2**：新增 `_refresh_universe_with_timeout(db, payload)` 函数（行 775 附近）

```python
def _refresh_universe_with_timeout(db, payload):
    """universe 刷新加 timeout 保护，防止 prepare 阶段永久阻塞。"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_refresh_discovery_universe, db, payload)
        try:
            return future.result(timeout=UNIVERSE_REFRESH_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            logger.error(
                "universe refresh TIMEOUT after %ds, payload=%s",
                UNIVERSE_REFRESH_TIMEOUT_SECONDS, payload,
            )
            return None
```

**改动 3**：行 819-826 调用点改为 `_refresh_universe_with_timeout`

**改动 4**：`_refresh_cn_etf_universe`（行 476-512）的 `ak.fund_etf_spot_em()` / `ak.fund_etf_category_sina()` 用 `call_akshare_with_retry` 包装（max_attempts=2）

**改动 5**：watchdog 在 prepare 阶段不更新 `updated_at`（见阶段 3）

### 3.2 修复失效点 2：探测端点线程池保护

**文件**：`app/api/routes/akshare_apis.py`

**改动 1**：降低并发探测的线程占用——`probe_api` 改用独立 `ThreadPoolExecutor`（max_workers=1）替代 `asyncio.to_thread`

```python
from concurrent.futures import ThreadPoolExecutor
_PROBE_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="akshare-probe")

async def probe_api(api_key: str, ...):
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(_PROBE_EXECUTOR, _run_probe, func, probe_args),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        ...
```

**改动 2**：独立线程池限制 8 个并发，避免耗尽 anyio 默认池（40）

**改动 3**：超时后 `logger.warning` 记录，便于追踪泄漏

**注意**：子线程仍无法 kill，但独立池限制最大泄漏数为 8，且不阻塞其他异步路由。

### 3.3 修复失效点 3：_fetch_history 超时预算调整

**文件**：`app/services/market_data.py`

**改动 1**：`_fetch_history` 每个数据源仅重试 1 次（从 3 次降为 1 次），3 源 × 1 次 = 3 次 × 20s = 60s < 90s

**改动 2**：新增 `FETCH_HISTORY_TOTAL_TIMEOUT_SECONDS = 75` 常量，`_sync_one_symbol_with_timeout` 调用时作为内层 timeout

**文件**：`app/services/discovery_tasks.py`

**改动 3**：`_sync_one_symbol_with_timeout` 中 `_sync_one_symbol` 在子线程中使用**独立的 db Session**（避免主子线程并发操作同一 Session）

```python
def _sync_one_symbol_with_timeout(db, symbol, payload, portfolio_id):
    from app.db.manager import DatabaseManager
    SessionLocal = DatabaseManager.get().session_factory
    def _run():
        sub_db = SessionLocal()
        try:
            return _sync_one_symbol(sub_db, symbol, payload, portfolio_id)
        finally:
            sub_db.close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_run)
        try:
            return future.result(timeout=SYNC_ONE_SYMBOL_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            logger.warning("sync_one %s TIMEOUT", symbol.symbol)
            return {"ok": False, "error": "同步超时（90s），跳过"}
```

---

## 四、阶段 3：watchdog 逻辑修正（P0）

### 4.1 问题

`discovery_tasks.py:758` watchdog 每 30s 更新 `task.updated_at` → `_expire_stale_tasks`（10min 阈值）永不触发 → universe 刷新卡死时任务永不中断。

### 4.2 修复方案

**改动 1**：新增 `task.heartbeat_at` 字段（或复用 `updated_at` 但加 `last_progress_at` 字段）

> **决策**：为避免 DB schema 变更风险，改用**阶段感知心跳**：
> - prepare 阶段（universe 刷新）：watchdog 不更新 `updated_at`，仅记录日志
> - sync 阶段（symbol 循环）：watchdog 正常更新 `updated_at`
> - `_expire_stale_tasks` 10min 阈值在 prepare 阶段生效，sync 阶段由单 symbol 90s 兜底

**改动 2**：`_watchdog_heartbeat` 增加 stage 判断

```python
def _watchdog_heartbeat(task_id, stop_event):
    while not stop_event.wait(timeout=_WATCHDOG_HEARTBEAT_SECONDS):
        try:
            db = SessionLocal()
            try:
                task = db.query(DiscoveryTaskRecord).filter_by(id=task_id).first()
                if task is None or task.status in _TERMINAL_STATES:
                    logger.info("watchdog %s exit: status=%s", task_id, task.status if task else "None")
                    return
                # 关键：prepare 阶段不更新 updated_at，让 _expire_stale_tasks 能兜底
                if task.stage != "prepare":
                    task.updated_at = datetime.utcnow()
                    db.commit()
                    logger.debug("watchdog %s heartbeat (stage=%s)", task_id, task.stage)
                else:
                    logger.debug("watchdog %s skip heartbeat in prepare stage", task_id)
            finally:
                db.close()
        except Exception:
            logger.warning("watchdog %s heartbeat failed", task_id, exc_info=True)
```

**改动 3**：`_expire_stale_tasks` 增加日志（阶段 1 L10）

---

## 五、阶段 4：4 维度测试补强

### 5.1 维度 1：功能显示测试

#### 5.1.1 自动化测试（白盒补强）

**文件**：`tests/test_whitebox_universe_refresh.py`（**新建**）

| 用例 | 验证点 |
|------|--------|
| `test_refresh_universe_with_timeout_returns_none_on_block` | mock `_refresh_discovery_universe` 阻塞 → 120s 超时返回 None |
| `test_refresh_universe_with_timeout_passes_through_on_success` | 正常返回透传 |
| `test_refresh_cn_etf_universe_wraps_fund_etf_spot_em_with_retry` | 验证 `ak.fund_etf_spot_em` 被重试包装 |
| `test_refresh_cn_etf_universe_falls_back_to_sina_on_failure` | 主源失败后回退到 sina |
| `test_watchdog_skips_heartbeat_in_prepare_stage` | prepare 阶段不更新 updated_at |
| `test_watchdog_updates_heartbeat_in_sync_stage` | sync 阶段正常更新 |
| `test_watchdog_logs_failure_instead_of_silent_swallow` | 异常时记录 WARNING 日志 |

**文件**：`tests/test_whitebox_probe_thread_pool.py`（**新建**）

| 用例 | 验证点 |
|------|--------|
| `test_probe_uses_independent_executor` | 验证使用 `_PROBE_EXECUTOR` 而非 anyio 默认池 |
| `test_probe_executor_max_workers_is_8` | 常量守护：`_PROBE_EXECUTOR._max_workers == 8` |
| `test_probe_logs_start_and_done` | 验证 INFO 日志输出 |
| `test_probe_logs_timeout` | 超时时 WARNING 日志输出 |

#### 5.1.2 UAT 人工测试（功能显示）

| # | 场景 | 预期 |
|---|------|------|
| F1 | 打开接口管理页 | 列表显示所有 akshare 接口，字段完整（key/name/last_probe_at/last_probe_success/last_probe_latency_ms） |
| F2 | 打开机会挖掘页 | 任务列表显示，状态/阶段/进度/updated_at 字段可见 |
| F3 | 任务 running 时 | 进度条正常推进，stage 标签中文显示 |
| F4 | 任务卡死时 | 黄色 Alert 显示"任务可能卡死"+取消按钮 |
| F5 | 探测按钮 loading 状态 | 单个/批量探测时按钮显示 loading，禁用点击 |
| F6 | 批量探测进度 | 3 个一组并发，进度可见 |
| F7 | 后端日志输出 | 探测/同步/心跳日志可见，级别正确 |

### 5.2 维度 2：交互测试

#### 5.2.1 自动化测试

**文件**：`tests/test_whitebox_interaction.py`（**新建**）

| 用例 | 验证点 |
|------|--------|
| `test_batch_probe_disables_all_single_buttons` | 批量探测时所有单个按钮 disabled |
| `test_batch_probe_releases_buttons_after_done` | 完成后按钮恢复可点击 |
| `test_batch_probe_concurrency_is_3` | 验证 CONCURRENCY 常量 |
| `test_stale_warning_appears_after_2min` | mock updated_at 2min 前 → staleWarning=true |
| `test_stale_warning_disappears_on_cancel` | 取消后 staleWarning=false |
| `test_probe_all_shows_summary_message` | 完成后显示 `OK (N)` 或 `N OK, M failed` |

#### 5.2.2 UAT 人工测试（交互）

| # | 场景 | 预期 |
|---|------|------|
| I1 | 快速点击单个探测按钮 5 次 | 仅触发 1 次，按钮 disabled |
| I2 | 批量探测期间点击单个探测 | 按钮禁用，无响应 |
| I3 | 批量探测期间再次点击"探测全部" | 按钮禁用，无响应 |
| I4 | 任务 running 时点击"取消" | 任务状态变为 cancelled，UI 更新 |
| I5 | 任务卡死 Alert 显示时点击"取消" | 调用 handleCancel，Alert 消失 |
| I6 | 任务卡死后 10min | 任务自动变为 failed，原因含"expired" |
| I7 | 暂停任务 24h 后 | 任务自动变为 expired |
| I8 | 探测超时 30s | 前端显示错误提示，不卡死 |

### 5.3 维度 3：数据正确性测试

#### 5.3.1 自动化测试

**文件**：`tests/test_whitebox_data_correctness.py`（**新建**）

| 用例 | 验证点 |
|------|--------|
| `test_probe_result_fields_complete` | ProbeResult 包含 success/latency_ms/error/last_probe_at |
| `test_probe_latency_ms_is_int_or_null` | latency_ms 类型正确 |
| `test_sync_one_symbol_returns_ok_dict` | 返回 `{"ok": bool, "error": str, ...}` |
| `test_sync_timeout_records_failed_count` | 超时后 failed_count += 1 |
| `test_universe_refresh_returns_seen_and_created` | 返回 dict 含 seen/created/symbols |
| `test_universe_refresh_timeout_returns_none_and_records_error` | 超时返回 None，task.message 含错误 |
| `test_watchdog_does_not_overwrite_terminal_status` | 终态任务 watchdog 立即退出 |
| `test_fetch_history_retries_only_once_per_source` | 每个数据源最多重试 1 次 |
| `test_sync_one_symbol_uses_independent_db_session` | 子线程使用独立 Session |
| `test_probe_akshare_api_content_type_header` | 验证 PUT 请求 Content-Type |

**文件**：`tests/test_blackbox_api_mgmt.py`（**补强**，已有 17 用例）

| 新增用例 | 验证点 |
|----------|--------|
| `test_probe_returns_within_30s_with_real_backend` | 真实后端探测 < 30s |
| `test_universe_refresh_does_not_hang_prepare_stage` | 全量同步 prepare 阶段 < 120s |
| `test_thread_pool_not_exhausted_after_20_probes` | 20 次探测后线程池水位正常 |
| `test_discovery_task_completes_within_reasonable_time` | 5 symbol 任务 < 10min |

#### 5.3.2 UAT 人工测试（数据正确性）

| # | 场景 | 预期 |
|---|------|------|
| D1 | 探测成功后 | last_probe_success=true, last_probe_latency_ms=合理值, last_probe_at=当前时间 |
| D2 | 探测失败后 | last_probe_success=false, last_probe_error=错误信息 |
| D3 | 全量同步完成后 | ok_count + failed_count + skipped_count = total |
| D4 | 单 symbol 超时 | failed_count += 1, errors_json 含超时记录 |
| D5 | universe 刷新失败 | task.status=failed, message 含"全市场标的列表拉取失败" |
| D6 | universe 刷新超时 | task.status=failed, message 含"universe refresh TIMEOUT" |
| D7 | 评分计算 | quality_score + timing_score + priority_score 逻辑正确 |
| D8 | 候选展示 | latest-candidates 返回全量候选（含 hold/reduce） |
| D9 | 数据源切换 | 日志可见"东财失败，尝试新浪"等切换记录 |
| D10 | 线程池水位 | 日志可见 borrowed_tokens，未接近 40 |

### 5.4 维度 4：UAT 阶段测试

#### 5.4.1 UAT 执行清单（基于 `docs/uat-checklist.md` 补强）

**0. 稳定性核心验证（15 项 → 25 项）**

| # | 场景 | 步骤 | 预期 |
|---|------|------|------|
| U0.1 | 单接口探测 | 点击单个接口的"探测"按钮 | 30s 内返回结果，UI 更新 |
| U0.2 | 探测全部 | 点击"探测全部" | 3 个一组并发，全部完成 < 180s |
| U0.3 | 探测超时接口 | 探测已知慢接口（如东财实时行情） | 30s 超时，前端显示错误，不卡死 |
| U0.4 | 探测线程池 | 连续 5 次"探测全部" | 后端线程池水位日志正常，不耗尽 |
| U0.5 | 全量同步 cn-stock | 启动 cn-stock 全量同步 | prepare 阶段 < 120s，sync 阶段进度推进 |
| U0.6 | 全量同步 cn-etf | 启动 cn-etf 全量同步 | 同上 |
| U0.7 | universe 刷新超时 | 模拟 akshare 慢响应 | 120s 超时，task.status=failed，message 含 TIMEOUT |
| U0.8 | 单 symbol 超时 | 模拟某 symbol 卡死 | 90s 超时，failed_count += 1，继续下一个 |
| U0.9 | 任务卡死检测 | 任务 running 且 updated_at 2min 无变化 | 前端显示黄色 Alert + 取消按钮 |
| U0.10 | 任务自动中断 | 任务 running 且 updated_at 10min 无变化 | task.status=failed，message 含 expired |
| U0.11 | watchdog prepare 阶段 | 任务在 prepare 阶段 | watchdog 不更新 updated_at（日志可见 skip） |
| U0.12 | watchdog sync 阶段 | 任务在 sync 阶段 | watchdog 每 30s 更新 updated_at |
| U0.13 | 任务取消 | running 任务点击取消 | status=cancelled，worker 不覆盖 |
| U0.14 | 任务暂停/恢复 | 暂停后 24h 内恢复 | 恢复成功，超 24h 标记 expired |
| U0.15 | 日志可见性 | 后端日志 | 探测/同步/心跳/超时日志清晰可见 |
| U0.16 | 线程泄漏监控 | 多次任务后 | 线程数不单调上升 |
| U0.17 | DB 连接泄漏 | 多次 symbol 超时 | DB 连接池不耗尽 |
| U0.18 | _fetch_history 回退 | 单 symbol 数据源切换 | 日志可见源切换，总耗时 < 90s |
| U0.19 | 探测并发保护 | 批量探测时点击单个 | 按钮禁用 |
| U0.20 | 前端轮询容错 | 后端 down 时 | 前端不卡死，console.warn 但不停止轮询 |
| U0.21 | 探测结果持久化 | 探测后刷新页面 | last_probe_at/success/latency 持久化 |
| U0.22 | 任务列表字段 | GET /discovery/tasks | updated_at/can_retry/cleanup_count 完整 |
| U0.23 | retry 端点 | POST /retry | 终态任务重新 queued |
| U0.24 | latest-candidates | GET /latest-candidates | 返回全量候选含 hold/reduce |
| U0.25 | Content-Type 验证 | PUT 接口配置 | FastAPI 能正确解析 body |

**1-7 类**：复用已有 `docs/uat-checklist.md` 的 80 项

#### 5.4.2 边界场景测试（15 项）

| # | 场景 | 预期 |
|---|------|------|
| B1 | akshare 全部接口 down | 探测全部失败，UI 显示错误，不卡死 |
| B2 | 网络断开 | timeout 生效，错误提示清晰 |
| B3 | akshare 返回 HTML（WAF 拦截） | 错误提示"Excel file format cannot be determined" |
| B4 | DB 连接池耗尽 | watchdog 日志 WARNING，不静默吞 |
| B5 | 任务队列堆积 | 5 个并发任务，均能完成或超时 |
| B6 | symbol 数量 7000+ | 全量同步进度推进，不卡在中间 |
| B7 | 极端重试档位（extreme） | max_retries=5，总耗时 < 90s |
| B8 | 同时探测 + 全量同步 | 独立线程池，互不影响 |
| B9 | 任务取消后 worker 仍运行 | 终态保护，status 不被覆盖 |
| B10 | 暂停任务 23h59min 恢复 | 恢复成功 |
| B11 | 暂停任务 24h01min 恢复 | 标记 expired |
| B12 | universe seen=0 | task.status=failed，message 清晰 |
| B13 | 单 symbol 超时 5 次 | failed_count=5，任务继续 |
| B14 | 批量探测 17 接口 | 全部完成 < 180s |
| B15 | 前端 2min Alert 显示后任务恢复进度 | Alert 自动消失 |

---

## 六、阶段 5：测试执行与验收

### 6.1 自动化测试执行顺序

```bash
# 1. 白盒回归（不含 slow/blackbox）
python -m pytest -m "not slow and not blackbox" -v

# 2. 新增白盒测试单独运行
python -m pytest tests/test_whitebox_universe_refresh.py tests/test_whitebox_probe_thread_pool.py tests/test_whitebox_interaction.py tests/test_whitebox_data_correctness.py -v

# 3. 慢测试（含真实 akshare 调用）
python -m pytest -m "slow" -v

# 4. 黑盒测试（需后端运行）
python -m pytest -m "blackbox" -v

# 5. 全量回归
python -m pytest -v
```

### 6.2 验收标准

| 维度 | 验收项 | 标准 |
|------|--------|------|
| 功能显示 | 所有 UI 元素正常显示 | UAT F1-F7 全部通过 |
| 交互 | 所有交互符合预期 | UAT I1-I8 全部通过 |
| 数据正确性 | 数据计算和输入输出正确 | UAT D1-D10 + 自动化测试全通过 |
| UAT | 25 项稳定性核心 + 80 项常规 + 15 项边界 | 全部通过 |
| 自动化 | 250 + 30 新增 = 280 用例 | 全通过，无回归 |
| 性能 | 单 symbol 同步 < 90s | 99 分位 |
| 性能 | universe 刷新 < 120s | 99 分位 |
| 性能 | 探测 < 30s | 99 分位 |
| 性能 | 线程池水位 < 35/40 | 持续监控 |
| 日志 | 关键事件可见 | L1-L13 全部输出 |

---

## 七、假设与决策

1. **修复后端代码优先于测试**：3 个致命失效点必须先修复，否则测试用例会卡死
2. **watchdog stage 感知方案**：避免 DB schema 变更风险，用 stage 判断替代新字段
3. **独立线程池方案**：探测用 `_PROBE_EXECUTOR(max_workers=8)`，不影响 anyio 默认池
4. **_fetch_history 降重试**：3 次 → 1 次，总耗时 180s → 60s，留余量给 90s 超时
5. **子线程独立 Session**：避免主子线程并发操作同一 Session 导致死锁
6. **测试用例策略**：常量守护 + 集成验证 + 真实环境黑盒，三层覆盖
7. **日志优先**：先补全日志，验证根因后再修复，避免盲目改动
8. **不修改业务逻辑**：仅修复稳定性问题，不改动评分/选股逻辑
9. **保留已有测试**：250 个已有用例不删除，仅补强
10. **UAT 清单复用**：已有 80 项 UAT 保留，新增 25 项稳定性核心 + 15 项边界

---

## 八、实施顺序与优先级

| 优先级 | 阶段 | 内容 | 预计用例数 |
|--------|------|------|------------|
| P0 | 阶段 1 | 日志增强（13 处） | - |
| P0 | 阶段 2 | 3 个失效点修复 | - |
| P0 | 阶段 3 | watchdog 逻辑修正 | - |
| P0 | 阶段 4.1+4.3 | 功能显示 + 数据正确性自动化测试 | 17 新建 |
| P1 | 阶段 4.2 | 交互测试自动化 | 6 新建 |
| P1 | 阶段 5.1 | 自动化测试执行 | - |
| P1 | 阶段 4.4 | UAT 清单补强 | 25+15 新增 |
| P2 | 阶段 5.2 | UAT 人工执行 | - |

**新增测试用例总计**：17（功能显示+数据）+ 6（交互）+ 4（黑盒补强）= **27 个**

---

## 九、关键文件清单

### 需修改的文件

| 文件 | 改动类型 |
|------|----------|
| `app/services/akshare_utils.py` | L11 日志级别调整 |
| `app/api/routes/akshare_apis.py` | L1-L4 日志 + 独立线程池 |
| `app/services/discovery_tasks.py` | L5-L10 日志 + universe timeout + watchdog stage 感知 + 子线程独立 Session |
| `app/services/market_data.py` | L12 日志 + _fetch_history 降重试 |
| `app/main.py` | 临时 DEBUG 级别（验证后恢复） |

### 需新建的测试文件

| 文件 | 用例数 |
|------|--------|
| `tests/test_whitebox_universe_refresh.py` | 7 |
| `tests/test_whitebox_probe_thread_pool.py` | 4 |
| `tests/test_whitebox_interaction.py` | 6 |
| `tests/test_whitebox_data_correctness.py` | 10 |

### 需补强的测试文件

| 文件 | 新增用例 |
|------|----------|
| `tests/test_blackbox_api_mgmt.py` | +4 |

### 需补强的 UAT 文档

| 文件 | 新增项 |
|------|--------|
| `docs/uat-checklist.md` | +25 稳定性核心 + 15 边界 |

---

## 十、风险与回滚

1. **watchdog stage 感知方案风险**：若 prepare 阶段耗时 > 10min（正常情况 60s），会被误判 stale
   - 缓解：prepare 阶段加日志，监控实际耗时；必要时调整 `STALE_RUNNING_DEADLINE` 为 15min
2. **独立线程池风险**：`_PROBE_EXECUTOR` max_workers=8 可能限制批量探测速度
   - 缓解：17 接口 3 组并发，最坏 6 批 × 30s = 180s，可接受
3. **_fetch_history 降重试风险**：数据源临时抖动时成功率下降
   - 缓解：3 个数据源仍有 3 次尝试，足够覆盖临时故障
4. **子线程独立 Session 风险**：增加 DB 连接数
   - 缓解：连接池默认 20，单 symbol 同步串行，不会耗尽
5. **回滚方案**：所有改动通过 git commit 隔离，可单独 revert
