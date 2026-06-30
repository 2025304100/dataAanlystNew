# 市场数据同步异步化 + 全局超时修复

## Context

`/market-data/update` 接口同步阻塞处理所有标的，当前端 `syncMarketData` 使用默认 20s 超时，标的数量多时必定超时。同样 `createScanRun`、`calculateScores` 等也使用默认 20s，存在超时风险。

用户要求：改为异步请求 + 心跳轮询模式，参考现有 Discovery 任务机制。

---

## 方案概要

1. 新建通用异步任务基础设施（`AsyncTaskRecord` 模型 + `async_tasks` 服务层）
2. 将市场数据同步转为异步任务，前端通过轮询获取进度
3. 修复其他慢接口的前端超时设置

---

## Step 1: 后端 — 通用异步任务模型

**新建** `app/models/async_task.py`

```
AsyncTaskRecord 表 (async_tasks):
  id          : String(64) PK  — uuid4.hex
  task_type   : String(32)     — "market_data_sync"
  status      : String(16)     — queued|running|done|failed|cancelled
  stage       : String(32)     — prepare|sync|score|scan|done
  percent     : Float          — 0~100
  message     : Text
  total       : Integer
  processed   : Integer
  ok_count    : Integer
  failed_count: Integer
  current_item: String(64) nullable — 当前处理标的
  payload_json: Text nullable        — 请求参数
  result_json : Text nullable        — 最终结果摘要
  errors_json : Text nullable        — 最近 20 条错误
  created_at / started_at / finished_at / updated_at
```

**修改** `app/models/__init__.py` — 注册新模型

---

## Step 2: 后端 — 通用异步任务服务

**新建** `app/schemas/async_task.py`

- `AsyncTaskRead` — 响应模型，含 result/errors 解析
- `MarketDataSyncCreate` — 复用 MarketDataUpdateRequest 字段

**新建** `app/services/async_tasks.py`

核心函数：
- `create_async_task(task_type, payload)` → 创建 DB 记录
- `get_async_task(task_id)` → 查询任务
- `list_async_tasks(task_type, limit)` → 列表
- `cancel_async_task(task_id)` → 取消
- `_set_task(db, task_id, **updates)` → 内部进度更新
- `_append_error(task, error)` → 追加错误
- `_expire_stale_tasks(db)` → 30 分钟无更新自动 failed
- `_start_worker(task_id, worker_func)` → threading.Thread(daemon=True)

---

## Step 3: 后端 — 市场数据同步任务 Worker

**新建** `app/services/market_data_sync_task.py`

- `create_market_data_sync_task(payload)` — 创建任务 + 启动 worker
- `_run_market_data_sync(task_id)` — 后台线程执行体

阶段进度映射：
```
prepare: 0~5%
sync:    5~75%  (按 processed/total 线性)
score:   75~90% (按 processed/total 线性)
scan:    90~98%
done:    100%
```

核心逻辑复用 `sync_market_data` 中的：
- `_resolve_sync_symbols()`
- `sync_symbol_daily_bars()`
- `calculate_symbol_score()`
- `upsert_trade_setup()`
- `run_scan()`

每个 symbol 处理后更新 progress，支持取消检查。

**修改** `app/api/routes/market_data.py` — 新增 3 个端点

- `POST /market-data/sync-tasks` — 创建异步同步任务
- `GET /market-data/sync-tasks/{task_id}` — 查询任务状态
- `POST /market-data/sync-tasks/{task_id}/cancel` — 取消任务

保留原有 `POST /market-data/update` 不变（内部/兼容用）。

---

## Step 4: 前端 — API + 超时修复

**修改** `frontend/src/api/client.ts`

新增 API：
- `createMarketDataSyncTask(payload)` → POST /market-data/sync-tasks
- `getMarketDataSyncTask(taskId)` → GET /market-data/sync-tasks/{id}
- `cancelMarketDataSyncTask(taskId)` → POST .../cancel

修复超时：
- `createScanRun` → timeoutMs: 60000
- `calculateScores` → timeoutMs: 120000
- `generateTradeSetup` → timeoutMs: 60000
- `backupDatabase` → timeoutMs: 60000
- `restoreDatabase` → timeoutMs: 120000

---

## Step 5: 前端 — 异步轮询 + UI 反馈

**修改** `frontend/src/types/index.ts`
- 新增 `AsyncTaskRecord` 接口

**修改** `frontend/src/context/AppContext.tsx`
- 新增状态: `syncTask`, `syncPolling`, `syncPollRef`
- 新增 `startSyncPolling()` — 2s 间隔轮询，终态时停止
- 重写 `runSync()` — 创建异步任务 → 启动轮询
- App 启动时检查未完成的 sync task 并恢复轮询
- cleanup 清理 interval

**修改** `frontend/src/App.tsx`
- 同步按钮显示进度：`同步中 12/50...`（基于 syncTask.processed/total）
- 同步完成/失败自动刷新 workbench

**修改** `frontend/src/i18n/index.ts`
- 新增: `syncStarted`, `syncCancelled`, `syncProgress`

---

## 验证方式

1. 启动后端 → 确认 `async_tasks` 表自动创建
2. 点击"同步"按钮 → 观察任务创建 → 轮询进度更新 → 完成后 workbench 刷新
3. 测试取消：同步过程中点击取消
4. 测试页面刷新：任务运行中刷新页面，确认恢复轮询
5. 测试其他接口超时修复生效（扫描、评分计算不再 20s 超时）
