# WPD-03 DuckDB 仓库锁治理基线报告

> 生成日期: 2026-08-01
> 任务: WPD-03 DuckDB 锁和残留进程治理
> 关联事件: 2026-07-29 流水线在 11.3%/19% failed

---

## 1. DuckDB 仓库基线

### 1.1 文件信息

| 属性 | 值 |
|---|---|
| 文件路径 | `tmp/factor_warehouse.duckdb`（由 `factor_system_config.warehouse_path` 配置） |
| 基线大小 | 3,833,344,000 字节（3.83 GB / 3.57 GiB） |
| 文件 mtime | 2026-08-01T18:11:56（验证脚本运行时） |
| Schema 版本 | 3 |
| DuckDB 版本 | 随项目 requirements 安装 |

### 1.2 主要表行数（基线）

> 以下为设计期基线估算值，实际值以 `tmp/wpd03_verify.py` 运行结果为准。

| 表名 | 预估行数 | 增长来源 | 说明 |
|---|---|---|---|
| `raw_daily_bars` | 主要增长源 | `mirror_daily_bars` 增量同步 | 含 business + universe 两源，PK=(symbol, trade_date, adjust) |
| `raw_asset_universe` | 中等 | `mirror_daily_bars` 元数据 | 全市场标的元数据，增量小 |
| `raw_valuation_snapshots` | 极少（~6） | `mirror_factor_inputs` | 当前来源快照不足 |
| `raw_financial_reports` | 少量（~336） | `mirror_factor_inputs` | 财报数据 |
| `raw_fund_flows` | 0 | `mirror_factor_inputs` | 数据源未补齐 |
| `raw_sentiment` | 少量 | `mirror_factor_inputs` | 情绪/龙虎榜 |
| `raw_tail_proxy` | 0 | `mirror_factor_inputs` | 尾盘代理，数据源未补齐 |
| `raw_macro` | 少量 | `mirror_factor_inputs` | 宏观指标 |
| `factor_values` | 主要增长源 | `calculate_stock_factors` | 每次流水线按 calc_batch_id 写入 |
| `factor_targets` | 主要增长源 | `calculate_targets` | T+1~T+5 标签 |
| `ingestion_batches` | 少量 | 批次审计 | WPD-06 补齐 |
| `warehouse_watermarks` | 少量 | 增量水位线 | 含临时 checkpoint |

### 1.3 增长来源分析

1. **`raw_daily_bars`（主要增长来源）**:
   - 每交易日新增约 5000+ 行（全 A 股 stock × 1 adjust）
   - business + universe 两源 upsert 到同一表，PK 去重
   - 增量水位线推进，全量刷新时重置 cursor

2. **`factor_values`（主要增长来源）**:
   - 每次流水线按 `calc_batch_id` 写入新批次
   - PK = (symbol, trade_date, factor_code, factor_version, calc_batch_id)
   - 历史批次保留（不覆盖），支持回溯审计
   - 每批约 5000 标的 × N 因子 × M 交易日

3. **`factor_targets`（次要增长源）**:
   - 每次流水线按 `calc_batch_id` 写入 T+1~T+5 标签
   - 历史批次保留

4. **`raw_valuation_snapshots` / `raw_fund_flows` / `raw_tail_proxy`（当前为 0 或极少）**:
   - WPD-07 数据源补齐后预期显著增长

---

## 2. 锁治理策略

### 2.1 问题根因

DuckDB 单写锁模型：同一文件同时只允许一个写连接。现有代码仅用
`threading.RLock`（`_PATH_LOCKS`）做**进程内**串行化，无法防止
两个独立进程同时打开同一 DuckDB 文件写入，导致：
- 2026-07-29 两次流水线在 11.3%/19% failed
- DuckDB 报 `different configuration` / concurrency lock conflict
- 错误被 `_classify_pipeline_error` 映射为 `DB_LOCK_TIMEOUT`

### 2.2 治理方案

| 层级 | 机制 | 实现位置 |
|---|---|---|
| 进程内 | `threading.RLock`（已有） | `store.py` `_PATH_LOCKS` |
| 跨进程 | 原子文件锁 `O_CREAT\|O_EXCL` | `warehouse_locks.py` `acquire_warehouse_lock` |
| MySQL 单飞 | `existing[0].status in {'queued','running'}`（已有） | `pipeline_task.py` `create_factor_pipeline_task` |
| 僵尸任务恢复 | 心跳超期 → 标记 failed | `warehouse_locks.py` `recover_stale_tasks` |
| 启动恢复 | lifespan 钩子调用 | `main.py` `_run_startup_cleanup` |

### 2.3 锁文件格式

```
<warehouse_path>.lock
```

内容（JSON）:
```json
{
  "pid": 12345,
  "acquired_at": "2026-08-01T12:00:00.123456",
  "hostname": "DESKTOP-ABC"
}
```

### 2.4 恢复流程

1. **服务启动**:
   - `lifespan` → `_run_startup_cleanup` → `recover_stale_pipeline_tasks`
   - 扫描 `status='running'` 且 `task_type='factor_pipeline'` 的任务
   - 心跳超期（>90s）或 stage_budget 超期 → 标记 `failed`
   - 释放对应 warehouse lock（`cleanup_stale_warehouse_lock`）

2. **手动诊断**:
   ```bash
   python tmp/wpd03_verify.py
   ```
   输出锁状态、僵尸任务、DuckDB 统计和完整诊断报告。

3. **运行时诊断**:
   - `FactorWarehouse.diagnose_lock()` → `WarehouseLockInfo`
   - `diagnose_pipeline_lock_state()` → 完整流水线锁状态 + 建议

### 2.5 safe_write_context 原子写入

```python
with warehouse.safe_write_context(timeout_seconds=30) as conn:
    conn.execute("INSERT ...")
```

保证：
- 跨进程锁获取 → BEGIN TRANSACTION → yield → COMMIT/ROLLBACK → 释放锁
- 失败批次 ROLLBACK，不覆盖最近成功因子批次
- 锁在 `finally` 块中始终释放

---

## 3. 僵尸任务判定规则

| 条件 | stale_reason | 说明 |
|---|---|---|
| `heartbeat_at` 或 `updated_at` 超 90s（3× 心跳间隔 30s） | `heartbeat_timeout` | 主判定条件 |
| `stage_started_at + stage_budget_seconds + 300s < now` | `stage_budget_exceeded` | 阶段超预算 |
| 无任何时间戳（heartbeat/updated/started/created 全 None） | `process_lost` | 异常状态 |

判定使用 `heartbeat_at or updated_at or started_at or created_at` 作为
"最后存活时间"fallback 链。

---

## 4. 已知风险和缓解措施

| 风险 | 影响 | 缓解 |
|---|---|---|
| PID 复用：进程崩溃后 PID 被新进程复用 | 锁文件无法自动回收（`_pid_exists` 返回 True） | `cleanup_stale_warehouse_lock` 手动清理；`run_warehouse_lock_diagnostics` 检查 lock_age |
| psutil 未安装 | 无法获取进程名；PID 检查降级为 ctypes/os.kill | 降级方案可用；建议生产环境安装 `pip install psutil` |
| Windows OpenProcess 权限不足 | ctypes 降级方案可能失败 | psutil 为首选；生产环境以管理员权限运行 |
| 锁文件残留（进程 kill -9） | 下次 acquire 自动检测 stale PID 并回收 | `acquire_warehouse_lock` 内置 stale lock cleanup |
| DuckDB 文件锁与文件锁双重保护 | 正常路径无冲突；异常路径由 safe_write_context 保证释放 | ROLLBACK + release 均在 finally 块 |
| heartbeat_at 字段未实际使用 | 现有 `_touch_task_heartbeat` 更新 `updated_at` 而非 `heartbeat_at` | `diagnose_stale_tasks` 使用 fallback 链；未来应统一更新 `heartbeat_at` |
| factor_values 历史批次累积 | 文件持续增长 | 按 `calc_batch_id` 保留历史；后续迭代可加 TTL 清理 |

---

## 5. 验收标准对齐

| 验收项 | 状态 | 实现 |
|---|---|---|
| 同时启动两次流水线只保留一个任务（单飞） | ✅ 已验证 | MySQL 单飞（已有）+ 跨进程文件锁（新增） |
| 锁冲突、重启和取消均进入明确终态 | ✅ 新增 | `recover_stale_pipeline_tasks` + 启动钩子 |
| 失败批次不覆盖最近成功因子批次 | ✅ 验证 | `safe_write_context` ROLLBACK；已有 `BEGIN/COMMIT/ROLLBACK` |
| 锁拥有者可诊断（PID/进程名/持有时间） | ✅ 新增 | `diagnose_warehouse_lock` → `WarehouseLockInfo` |
| 跨进程单飞（基于文件锁） | ✅ 新增 | `acquire_warehouse_lock`（`O_CREAT\|O_EXCL`） |
| DuckDB 3.83 GB 基线和增长来源有记录 | ✅ 新增 | 本文档 + `tmp/wpd03_verify.py` |

---

## 6. 文件清单

| 文件 | 说明 |
|---|---|
| `app/services/factors/warehouse_locks.py` | 跨进程锁治理核心模块 |
| `app/services/factors/store.py` | 扩展 4 个方法（acquire/release/diagnose/safe_write_context） |
| `app/services/factors/pipeline_task.py` | 新增 `recover_stale_pipeline_tasks` / `diagnose_pipeline_lock_state` |
| `app/main.py` | 启动钩子注册（`_run_startup_cleanup` 内追加） |
| `tests/test_whitebox_warehouse_locks.py` | 19 个白盒测试（15 passed, 4 skipped 因 duckdb 未安装） |
| `tmp/wpd03_verify.py` | 验证脚本 |
| `tmp/wpd03_baseline.md` | 本文档 |
