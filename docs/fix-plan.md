# 修复计划 — 黑盒/白盒测试与代码审查发现

> 基于 2026-07-02 的黑盒测试 + 白盒测试 + 代码审查结果制定
> 测试基线：55 passed / 2 xfailed(C-3 bug 确认) / 0 failed
> 修复原则：不破坏现有 P0/P1/P2/P3 功能；最小必要改动；先修高风险、低改动量项

---

## 一、修复优先级总览

| 阶段 | 范围 | 问题数 | 风险等级 | 改动量 |
|------|------|--------|----------|--------|
| P0 立即修复 | 数据正确性 / 安全泄露 / DoS | 5 | Critical+High | 小 |
| P1 本迭代修复 | 并发保护 / 一致性 / 前端崩溃 | 8 | High | 中 |
| P2 下个迭代 | 边界防御 / 性能 N+1 / 国际化 | 18 | Medium | 中 |
| P3 技术债周期 | 类型安全 / 代码质量 / 可维护性 | 21 | Low | 大 |

---

## 二、P0 — 立即修复（数据正确性 / 安全 / DoS）

### P0-1 修复 data_credibility 重置 bug（C-3）

- **文件**：[app/services/analysis.py:134](file:///d:/ai_project/dataAanlystNew/app/services/analysis.py#L134)
- **现象**：第 134 行 `data_credibility = 0.0` 无条件重置，覆盖了第 68 行 `<5` 分支设置的 `0.2`；导致 K 线稀疏标的可信度永远为 0。
- **修复方案**：删除第 134 行的无条件重置；改为在 `bar_count >= 5` 分支内部赋初值。
- **改动**：
  ```python
  # 修改前（第 132-143 行）
  data_credibility = 0.0
  bar_count = len(bars)
  if bar_count >= 5:
      ...
      data_credibility = round(...)

  # 修改后
  bar_count = len(bars)
  if bar_count >= 5:
      bar_factor = ...
      freshness_factor = ...
      data_credibility = round(min(1.0, bar_factor * freshness_factor), 2)
  # else 分支保持 <5 时已赋值 0.2，无需重置
  ```
- **验证**：移除 [test_whitebox_analysis.py](file:///d:/ai_project/dataAanlystNew/tests/test_whitebox_analysis.py) 中 2 个 `@pytest.mark.xfail` 标记，测试应直接通过。
- **回归风险**：低。仅影响 `data_credibility` 字段，不影响评分主流程。

### P0-2 修复 MySQL 连接异常泄露明文密码（C-2）

- **文件**：[app/api/routes/db_config.py:96,149](file:///d:/ai_project/dataAanlystNew/app/api/routes/db_config.py#L96)
- **现象**：`str(e)` 直接回传前端，SQLAlchemy 异常含完整 URL（含密码）。
- **修复方案**：
  - 第 96 行：`raise HTTPException(400, "MySQL 连接失败，请检查主机/端口/凭据")` + 服务端 `logger.exception`
  - 第 149 行：`message="连接失败，请检查配置"` + 服务端日志
- **改动量**：2 处单行替换 + 增加 `import logging; logger = logging.getLogger(__name__)`。
- **回归风险**：无。仅修改错误文案。

### P0-3 修复 AST 沙箱 Pow DoS 风险（H-1）

- **文件**：[app/services/backtest.py:495,497,528-531](file:///d:/ai_project/dataAanlystNew/app/services/backtest.py#L495)
- **现象**：`ast.Pow` 在白名单中，`9**9**9` 可构造超大整数导致 OOM。
- **修复方案**：在 `_eval_sandboxed` 的 BinOp 分支中，对 `ast.Pow` 结果设上限。
- **改动**：
  ```python
  # 在 _BIN_OPS 字典下方新增受保护的 pow
  def _safe_pow(a, b):
      try:
          result = operator.pow(a, b)
      except (TypeError, ValueError, OverflowError):
          return None
      # 上限：超过 1e100 直接返回 None（足够覆盖正常指标计算）
      if isinstance(result, (int, float)) and abs(result) > 1e100:
          return None
      return result

  _BIN_OPS = {ast.Add: operator.add, ..., ast.Pow: _safe_pow}
  ```
- **验证**：[test_whitebox_backtest_sandbox.py](file:///d:/ai_project/dataAanlystNew/tests/test_whitebox_backtest_sandbox.py) 的 `test_sandbox_pow_in_allowlist_confirms_dos_risk` 应改为断言漏洞已闭合（修改断言方向）。
- **回归风险**：低。正常指标公式不会产生 1e100 以上结果。

### P0-4 修复迁移先删后插数据丢失风险（C-4）

- **文件**：[app/services/migration.py:147-158](file:///d:/ai_project/dataAanlystNew/app/services/migration.py#L147)
- **现象**：delete 与 insert 分属不同事务，中途失败导致目标表被清空。
- **修复方案**：合并 delete + insert 到同一事务。
- **改动**：
  ```python
  # 修改前
  with mysql_engine.begin() as conn:
      conn.execute(table.delete())
  if rows:
      for batch_start in range(0, len(rows), BATCH_SIZE):
          with mysql_engine.begin() as conn:
              conn.execute(table.insert(), batch)

  # 修改后：单事务
  with mysql_engine.begin() as conn:
      conn.execute(table.delete())
      for batch_start in range(0, len(rows), BATCH_SIZE):
          batch = rows[batch_start:batch_start + BATCH_SIZE]
          conn.execute(table.insert(), batch)
  ```
- **回归风险**：中。事务变长，但避免数据丢失更重要。需测试大表迁移。

### P0-5 修复前端 computeWinRate 返回 Infinity（前端 C-1）

- **文件**：[frontend/src/utils/indicators.ts:516](file:///d:/ai_project/dataAanlystNew/frontend/src/utils/indicators.ts#L516)
- **现象**：无亏损时 `profitFactor = Infinity`，破坏 ECharts 渲染与 JSON 序列化。
- **修复方案**：改为 `null`，并在展示层兜底。
- **改动**：
  ```ts
  // 修改前
  const profitFactor = avgLoss > 0 ? (avgWin * winCount) / (avgLoss * lossCount) : Infinity;

  // 修改后
  const profitFactor = avgLoss > 0 ? (avgWin * winCount) / (avgLoss * lossCount) : null;
  ```
- **类型同步**：函数返回类型中 `profitFactor: number` → `profitFactor: number | null`。
- **展示层**：消费处（如 BacktestResult.tsx）增加 `?? "-"` 兜底。
- **验证**：`npx tsc --noEmit` 通过 + 人工检查回测结果展示。

---

## 三、P1 — 本迭代修复（并发 / 一致性 / 前端崩溃）

### P1-1 为 discovery_tasks._set_task 增加终态保护（H-2）

- **文件**：[app/services/discovery_tasks.py:103-112](file:///d:/ai_project/dataAanlystNew/app/services/discovery_tasks.py#L103)
- **现象**：与 async_tasks 不同，discovery 的 _set_task 无终态保护，cancelled 可被 worker 覆盖为 done。
- **修复方案**：复用 async_tasks 的终态保护逻辑。
- **改动**：
  ```python
  _TERMINAL_STATES = ("done", "failed", "cancelled")

  def _set_task(db: Session, task_id: str, **updates) -> DiscoveryTaskRecord:
      task = db.get(DiscoveryTaskRecord, task_id)
      if task is None:
          raise ValueError("Discovery task not found")
      # 终态保护：已终态的任务不允许覆盖 status/stage
      if task.status in _TERMINAL_STATES:
          updates = {k: v for k, v in updates.items() if k not in ("status", "stage")}
          if not updates:
              return task
      for key, value in updates.items():
          setattr(task, key, value)
      task.updated_at = _now()
      db.commit()
      db.refresh(task)
      return task
  ```
- **补充**：在 `_run_discovery_task` 的关键节点（scan 阶段前后）增加取消检查。

### P1-2 修复 sync_market_data 异常未 rollback（H-4）

- **文件**：[app/services/market_data.py:438-446](file:///d:/ai_project/dataAanlystNew/app/services/market_data.py#L438)
- **现象**：单个标的同步失败后未 `db.rollback()`，脏数据混入后续 commit。
- **修复方案**：在 except 块开头加 `db.rollback()`。
- **改动**：
  ```python
  except Exception as exc:
      db.rollback()  # 新增：清理失败标的的脏数据
      results.append({...})
  ```

### P1-3 修复前端 AppContext 多处空 catch（前端 C-3）

- **文件**：[frontend/src/context/AppContext.tsx:266,424,465,604,794](file:///d:/ai_project/dataAanlystNew/frontend/src/context/AppContext.tsx#L266)
- **现象**：`.catch(() => {})` 完全静默失败。
- **修复方案**：统一改为 Toast + console.warn。
- **改动模式**：
  ```ts
  // 修改前
  .catch(() => {});

  // 修改后
  .catch((err) => {
    console.warn("操作失败", err);
    ctx.showToast("error", t("loadFailed"));
  });
  ```
- **验证**：前端无新增 TS 错误。

### P1-4 修复 Trading.tsx 原生 table 违规（前端 C-2）

- **文件**：[frontend/src/components/Trading.tsx:294-328](file:///d:/ai_project/dataAanlystNew/frontend/src/components/Trading.tsx#L294)
- **现象**：使用原生 `<table>` 违反工程约束。
- **修复方案**：迁移到 Ant Design `<Table>`，参考 PortfolioWorkbench.tsx 实现。
- **改动量**：中等（约 30 行重构）。

### P1-5 修复 AbortError 未区分取消与超时（前端 H-2）

- **文件**：[frontend/src/api/client.ts:38](file:///d:/ai_project/dataAanlystNew/frontend/src/api/client.ts#L38)
- **修复方案**：在超时分支抛 `new Error("timeout")`，catch 中分流。
- **改动**：
  ```ts
  // 超时定时器
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  // catch 中
  if (error.name === "AbortError") {
    // 通过自定义标记区分超时
    if (controller.signal._timedOut) {
      throw new Error(t("requestTimeout"));
    }
    return; // 用户主动取消，静默
  }
  ```

### P1-6 修复前端硬编码文案未走 t()（前端 H-3）

- **文件**：
  - [App.tsx:134](file:///d:/ai_project/dataAanlystNew/frontend/src/App.tsx#L134)（"今日决策"）
  - [Discovery.tsx:247-249,405](file:///d:/ai_project/dataAanlystNew/frontend/src/components/Discovery.tsx#L247)
  - [InvestmentCenter.tsx:480-516](file:///d:/ai_project/dataAanlystNew/frontend/src/components/InvestmentCenter.tsx#L480)
- **修复方案**：迁移到 [i18n/index.ts](file:///d:/ai_project/dataAanlystNew/frontend/src/i18n/index.ts) 新增 key + `t()` 调用。

### P1-7 修复 ECharts 实例未 dispose（前端 H-5）

- **文件**：InvestmentCenter.tsx、BacktestResult.tsx
- **修复方案**：通过 ref 获取实例，在 useEffect cleanup 中 `echartsInstance.dispose()`。

### P1-8 修复 AppContext useEffect 依赖不稳定（前端 H-6）

- **文件**：[AppContext.tsx:292,447-468,783-814](file:///d:/ai_project/dataAanlystNew/frontend/src/context/AppContext.tsx#L292)
- **修复方案**：
  - `detailCache` 改为 useRef
  - Discovery 轮询 effect 用 `setState(prev => ...)` 函数式更新，移除 `discoveryTask` 依赖

---

## 四、P2 — 下个迭代（边界 / 性能 / 国际化）

| 编号 | 问题 | 文件 | 改动量 |
|------|------|------|--------|
| M-1 | _proxy_bypass 跨线程改 os.environ | market_data.py:38-46 | 中 |
| M-2 | discovery_tasks 缺并发保护 | discovery_tasks.py:169-197 | 小 |
| M-3 | _upsert_bars 行级 N+1 | market_data.py:288-317 | 中 |
| M-4 | scans.run_scan N+1 查询 | scans.py:58-66 | 中 |
| M-5 | sim_accounts 未防御 avg_cost=None | sim_accounts.py:250-257 | 小 |
| M-6 | trade_plans stage_limits_json 无防御 | trade_plans.py:504-506 | 小 |
| M-7 | allocation ETF max_pct 假设非空 | allocation.py:119 | 小（已被白盒测试验证） |
| M-8 | backtest 公式异常被静默 | backtest.py:665-679 | 小 |
| M-9 | market_data_sync_task 中段无取消检查 | market_data_sync_task.py:146-219 | 小 |
| M-10 | run_backtest 异常分支二次失败 | backtest.py:1961-1966 | 小 |
| M-11 | alerts.evaluate_all_rules 静默 | alerts.py:259-264 | 小 |
| M-12 | _compute_statistics 0/负权益 | backtest.py:1110-1116 | 小 |
| M-13 | backtest 路由 except 返回 400 | backtest.py:77-78 | 小 |
| 前端 M-1 | format.ts 未处理 NaN/Infinity | utils/format.ts:7-10,44-47 | 小 |
| 前端 M-2 | trade-plan.ts clamp 重复定义 | utils/trade-plan.ts:3 | 小 |
| 前端 M-3 | trade-plan.ts zoneMax 负数 | utils/trade-plan.ts:116 | 小 |
| 前端 M-5 | BacktestConfig 用 message 非 showToast | BacktestConfig.tsx | 中 |
| 前端 M-6 | BacktestConfig/.catch(() => {}) 与 any | BacktestConfig.tsx | 中 |
| 前端 M-11 | useChartDrawings.ts any 与空 catch | hooks/useChartDrawings.ts | 小 |

---

## 五、P3 — 技术债周期（类型安全 / 可维护性）

- 后端 C-1：全站无认证（需架构级设计，单独立项）
- 后端 L 系列：DDL 拼接、全表 fetchall、重试过度、fee 硬编码、注释乱码、LIKE 未转义等 13 项
- 前端 H-1：client.ts `as any` 全面替换为强类型
- 前端 H-7：InvestmentCenter 多处 any[] 替换
- 前端 H-8：TaskCenter 动态 i18n key 与 err: any
- 前端 M-4：BacktestConfig useMemo 依赖修正
- 前端 M-7：InvestmentCenter `<button>` 加 type="button"
- 前端 M-10：PortfolioWorkbench backup: any 链式兜底
- 前端 L 系列：i18n 静默回显、重复常量、locale 不一致等 8 项

---

## 六、执行顺序与验证策略

### 执行顺序

```
P0-1 (C-3)  ──→ 移除 xfail 标记 ──→ 跑白盒测试
P0-2 (C-2)  ──→ 手动测试 MySQL 连接失败场景
P0-3 (H-1)  ──→ 修改沙箱测试断言 ──→ 跑白盒测试
P0-4 (C-4)  ──→ 手动测试迁移流程
P0-5 (前端 C-1) ──→ tsc --noEmit ──→ 前端构建
P1-1..P1-8  ──→ 全量回归
```

### 验证清单

- [ ] `python -m pytest tests/ -v` 全部 passed（无 xfail）
- [ ] `cd frontend && npx tsc --noEmit` 退出码 0
- [ ] `cd frontend && npm run build` 成功
- [ ] 手动验证：评分页 K 线稀疏标的 data_credibility 显示 0.2
- [ ] 手动验证：MySQL 错误配置不泄露密码
- [ ] 手动验证：自定义指标公式 `9**9**9` 不卡死
- [ ] 手动验证：切换投资组合不触发整页刷新

### 回归测试命令

```bash
# 后端全量测试
cd d:\ai_project\dataAanlystNew
python -m pytest tests/ -v --tb=short

# 前端类型检查
cd frontend && npx tsc --noEmit

# 前端构建
cd frontend && npm run build
```

---

## 七、风险与约束

1. **不破坏现有功能**：所有修复需保证 P0/P1/P2/P3 现有功能正常
2. **UI/UX 优化不修改业务逻辑**：前端修复仅改表现层
3. **错误信息国际化**：catch 块错误信息必须用 t() 函数
4. **异步任务并发保护**：所有修改不能引入新的竞态
5. **数据一致性**：迁移/同步类修复需特别测试失败回滚场景

---

## 八、附录：原始审查发现索引

### 后端问题清单

| 编号 | 级别 | 文件:行 | 简述 |
|------|------|---------|------|
| C-1 | Critical | app/main.py | 全站无认证 |
| C-2 | Critical | db_config.py:96,149 | 密码泄露 |
| C-3 | Critical | analysis.py:134 | data_credibility 重置 |
| C-4 | Critical | migration.py:147-158 | 先删后插数据丢失 |
| H-1 | High | backtest.py:495 | AST Pow DoS |
| H-2 | High | discovery_tasks.py:103 | 缺终态保护 |
| H-3 | High | backtest.py:1615-1966 | N+1 查询 |
| H-4 | High | market_data.py:438-446 | 异常未 rollback |
| H-5 | High | migration.py + db_config.py | 并发 TOCTOU |
| M-1..M-13 | Medium | 见 P2 表 | 边界/性能/异常处理 |
| L-1..L-13 | Low | 见 P3 段 | 代码质量 |

### 前端问题清单

| 编号 | 级别 | 文件:行 | 简述 |
|------|------|---------|------|
| C-1 | Critical | indicators.ts:516 | Infinity 破坏渲染 |
| C-2 | Critical | Trading.tsx:294 | 原生 table |
| C-3 | Critical | AppContext.tsx:266等 | 空 catch 静默 |
| H-1 | High | client.ts:19,25,37 | as any 滥用 |
| H-2 | High | client.ts:38 | AbortError 不分流 |
| H-3 | High | App/Discovery/InvestmentCenter | 硬编码文案 |
| H-4 | High | App.tsx:196 | window.location.reload |
| H-5 | High | InvestmentCenter/BacktestResult | ECharts 未 dispose |
| H-6 | High | AppContext.tsx:292,447 | 依赖不稳定 |
| H-7 | High | InvestmentCenter.tsx:274 | any[] 类型 |
| H-8 | High | TaskCenter.tsx:76,106 | 动态 key + any |
| M-1..M-12 | Medium | 见 P2 表 | NaN 兜底/重复实现等 |
| L-1..L-8 | Low | 见 P3 段 | i18n/常量/locale |

---

**计划制定日期**：2026-07-02
**预计完成节点**：P0 立即 / P1 本迭代 / P2 下迭代 / P3 技术债周期
