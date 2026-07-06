# 专业测试体系完善计划（4 维度全覆盖）

> 目标：针对当前测试体系"后端重、前端零、E2E 散、UAT 全手动"的失衡现状，建立覆盖**功能显示 / 交互正常 / 数据正确性 / UAT 阶段** 4 个维度的专业测试体系，防止"测试不全导致线上问题"（典型案例：v2 修复后仍卡死、第三方接口不稳定未被发现）。

---

## 一、现状分析（基于代码探索）

### 1.1 测试体系现状

| 维度 | 现状 | 关键问题 |
|------|------|---------|
| 后端白盒 | 14 文件 / ~252 用例，覆盖接口管理/机会挖掘/评分/回测沙箱/异步任务 | 含 2 处脆弱的源码字符串断言（`test_whitebox_interaction.py`、`test_whitebox_data_correctness.py`），重构易失效 |
| 后端黑盒 | 2 文件 / ~42 用例，依赖手工启动后端 | `test_blackbox_api.py` 未加 `@pytest.mark.blackbox`，marker 筛选不可靠 |
| 前端测试 | **零测试**，39 个 `.ts/.tsx` 文件无任何覆盖 | 无 Vitest/Jest 配置，`package.json` 无 test 脚本，无测试依赖 |
| E2E 测试 | 2 个 Playwright 脚本（`test_ui_smoke.py`、`test_sim_buy.py`）散落根目录 | 未纳入 pytest 收集，未声明 playwright 依赖，需手工运行 |
| UAT 清单 | `docs/uat-checklist.md` 80+ 项 | 完全手工执行，无自动化守护；缺告警中心/任务中心/投资中心/回测配置/条件构建器/自定义指标/数据库配置等模块 |
| CI 自动化 | 无 `.github/workflows`、无 pre-commit hook | 测试依赖未声明，测试结果散落 `.txt` 文件 |

### 1.2 4 维度覆盖盲区

| 维度 | 自动化覆盖 | 手动覆盖（UAT） | 关键盲区 |
|------|-----------|----------------|---------|
| **维度1 功能显示** | 几乎为零（仅源码字符串断言） | 接口管理/机会挖掘/外部数据/评分配置共 ~20 项 | 表格列、Tag 颜色、Tooltip、Empty 占位、loading skeleton、i18n 切换均无自动化守护 |
| **维度2 交互正常** | 极弱（源码字符串匹配 `CONCURRENCY=3`、`120000` 等） | ~24 项 | 无真实按钮点击、无 Toast 内容验证、无 loading 切换、无并发保护实际效果验证 |
| **维度3 数据正确性** | 后端较好（胜率/评分/percent/边界值），前端零覆盖 | ~12 项 | 前端 `utils/format.ts`（percent/score/withFinalOpportunityScore）、`indicators.ts`、`trade-plan.ts` 计算逻辑零测试；源码断言不验证实际行为 |
| **维度4 UAT 阶段** | 无 | 80+ 项全手动 | 缺 7 个模块的 UAT 项；无执行记录与追踪；无回归守护 |

### 1.3 后端空白模块（完全无测试）

`alerts.py`、`custom_indicators.py`、`db_config.py`、`journals.py`、`market_events.py`、`portfolios.py`（仅 GET 列表）、`signal_rules.py`、`sim_accounts.py`（仅裸脚本）、`trade_setups.py`、`watchlists.py`（仅 GET 列表）+ 对应 services 层。

---

## 二、4 维度覆盖策略与目标

### 维度1：功能显示正常

**策略**：前端组件渲染测试（Vitest + Testing Library）+ UAT 手动补充

**目标**：
- 核心组件（AkshareApiManager / Discovery / ExternalDataSync / ScoringConfigSettings / PortfolioWorkbench / Trading）渲染测试覆盖表格列、Tag 颜色映射、Tooltip 存在性、Empty 占位、loading skeleton
- i18n 中英文切换后文案更新验证
- UAT 清单补全告警中心/任务中心/投资中心/回测配置/条件构建器/自定义指标/数据库配置 7 个模块的显示项

### 维度2：交互正常且合理

**策略**：前端组件交互测试 + E2E 关键链路 + UAT 手动补充

**目标**：
- 按钮点击 → loading → 反馈（Toast 内容/状态变更）全链路验证
- 并发保护实际效果验证（批量探测时单按钮真的 disabled）
- 表单校验错误提示验证（min>max、超限、空值等）
- E2E 覆盖 3 条核心用户链路（接口管理/机会挖掘/评分配置）
- 替换 `test_whitebox_interaction.py` 的源码字符串断言为真实行为测试

### 维度3：数据正确性（计算 + 输入输出）

**策略**：前端 utils 单元测试 + 后端计算链路测试 + API 输入输出一致性测试

**目标**：
- 前端 `utils/format.ts`（percent/score/money/withFinalOpportunityScore/computeSuggestedBuyQuantity 等）100% 覆盖
- 前端 `utils/indicators.ts`、`utils/trade-plan.ts` 核心计算覆盖
- 后端胜率除零保护、评分计算、percent 边界、universe seen=0 中止等数据链路测试
- 替换 `test_whitebox_data_correctness.py` 的源码字符串断言为真实行为测试
- API 输入输出字段一致性测试（响应字段完整性、类型正确性）

### 维度4：UAT 阶段测试

**策略**：UAT 清单补全 + 关键项自动化守护 + 执行记录

**目标**：
- UAT 清单补全 7 个空白模块（告警中心/任务中心/投资中心/回测配置/条件构建器/自定义指标/数据库配置）
- 将 UAT 0.1-0.5 稳定性核心 25 项转化为自动化测试（已部分完成，需补全）
- UAT 执行记录模板（含截图/操作步骤/预期 vs 实际/通过失败标记）
- CI 自动化守护（pre-commit hook + GitHub Actions 可选）

---

## 三、测试基础设施搭建

### 3.1 前端测试基础设施（P0 阶段）

**文件修改**：

1. **`frontend/package.json`** — 新增 devDependencies 和 test 脚本
   ```json
   {
     "scripts": {
       "test": "vitest run",
       "test:watch": "vitest",
       "test:coverage": "vitest run --coverage"
     },
     "devDependencies": {
       "vitest": "^2.1.0",
       "@testing-library/react": "^16.0.0",
       "@testing-library/jest-dom": "^6.4.0",
       "@testing-library/user-event": "^14.5.0",
       "jsdom": "^25.0.0",
       "@vitest/coverage-v8": "^2.1.0"
     }
   }
   ```

2. **`frontend/vitest.config.ts`**（新建）— Vitest 配置
   ```typescript
   import { defineConfig } from "vitest/config";
   import react from "@vitejs/plugin-react";
   
   export default defineConfig({
     plugins: [react()],
     test: {
       environment: "jsdom",
       globals: true,
       setupFiles: ["./src/test/setup.ts"],
       css: false,
     },
   });
   ```

3. **`frontend/src/test/setup.ts`**（新建）— 测试全局 setup
   - 导入 `@testing-library/jest-dom`
   - mock `matchMedia`、`ResizeObserver`、`IntersectionObserver`
   - 配置 antd ConfigProvider 上下文

4. **`frontend/src/test/render.tsx`**（新建）— 自定义 render helper
   - 包裹 `AppProvider` + `ConfigProvider` + `MemoryRouter`
   - 支持传入初始 state

5. **`frontend/tsconfig.json`** — 添加 test 类型
   ```json
   {
     "compilerOptions": {
       "types": ["vitest/globals", "@testing-library/jest-dom"]
     }
   }
   ```

### 3.2 E2E 测试基础设施（P2 阶段）

**文件修改**：

1. **`requirements.txt`** — 新增测试依赖
   ```
   pytest-playwright>=1.4.0
   ```
   （playwright 需 `playwright install chromium`）

2. **`pytest.ini`** — 新增 E2E marker 和 testpaths
   ```ini
   [pytest]
   testpaths = tests tests/e2e
   markers =
       slow: marks tests as slow
       blackbox: black-box API tests requiring running backend
       whitebox: white-box unit tests
       e2e: end-to-end tests requiring frontend+backend running
   ```

3. **`tests/e2e/conftest.py`**（新建）— E2E fixtures
   - `live_backend` fixture（检查后端可达性，否则 skip）
   - `live_frontend` fixture（检查前端可达性，否则 skip）
   - `browser` fixture（复用 playwright page）

4. **迁移根目录 Playwright 脚本**
   - `test_ui_smoke.py` → `tests/e2e/test_ui_smoke.py`（改造为 pytest 格式）
   - `test_sim_buy.py` → `tests/e2e/test_sim_buy.py`（改造为 pytest 格式）

### 3.3 测试标记规范化（P0 阶段）

**文件修改**：所有 `tests/test_*.py` 文件
- 为每个测试文件/类/函数添加 `@pytest.mark.whitebox` 或 `@pytest.mark.blackbox` 标记
- 确保 `pytest -m whitebox` / `pytest -m blackbox` / `pytest -m e2e` 可靠筛选

---

## 四、分阶段实施计划

### P0 阶段：基础设施 + 数据正确性核心守护

> 目标：搭建前端测试基础设施，覆盖前端 utils 计算（维度3 核心），替换脆弱的源码字符串断言，补强后端数据计算链路。

#### P0-1 前端测试基础设施搭建
- 修改 `frontend/package.json`：新增 vitest + testing-library 依赖和 test 脚本
- 新建 `frontend/vitest.config.ts`
- 新建 `frontend/src/test/setup.ts`
- 新建 `frontend/src/test/render.tsx`
- 修改 `frontend/tsconfig.json` 添加 test 类型
- **验证**：`cd frontend && npm install && npm test -- --passWithNoTests` 成功

#### P0-2 前端 utils 单元测试（维度3 - 数据正确性）
- 新建 `frontend/src/utils/__tests__/format.test.ts`（~30 用例）
  - `percent`: null/undefined/NaN/Infinity 返回 "-"；正常值返回 "12.3%"
  - `score`: null/undefined/NaN/Infinity 返回 "-"；digits 参数生效
  - `money`: null/undefined/NaN/Infinity 返回 "-"；currency 缓存生效；非法 currency 降级
  - `clamp`: 边界值
  - `roundPrice`: null 返回 0；正常值四舍五入
  - `withFinalOpportunityScore`: news_adjustment_pct 限制在 [-0.12, 0.12]；final_opportunity_score 限制在 [0, 100]；newsSnapshot 为 null 时降级
  - `computeSuggestedBuyQuantity`: price<=0 或 budget<=0 返回 0；lotSize 取整逻辑（CN=100，US=1）
  - `aggregateWeeklyBars`: 周聚合逻辑正确性
  - `inferSymbolPayload`: 6 位数字代码（CN stock/etf 区分）、字母代码（US stock/etf 区分）、非法输入抛错
  - `discoveryFreshness`: is_frozen / warning_days / 正常态分支
  - `badgeClass` / `pnlClass` / `sentimentClass` / `riskClass`: 各分支映射
- 新建 `frontend/src/utils/__tests__/indicators.test.ts`（~15 用例）
  - MA/EMA/MACD/RSI/BOLL 等技术指标计算正确性
  - 空数组、单元素、NaN 输入边界
- 新建 `frontend/src/utils/__tests__/trade-plan.test.ts`（~10 用例）
  - 交易计划计算（tranche_plan、remaining_stage_amount 等）
  - 边界：position=0、price=0、quantity=0
- **验证**：`npm test` 全部通过，utils 覆盖率 ≥ 80%

#### P0-3 替换源码字符串断言为真实行为测试（维度3 - 数据正确性）
- 重写 `tests/test_whitebox_data_correctness.py`（保留文件名，重写内容）
  - `test_sync_one_symbol_returns_ok_dict`: 改为真实调用 `_sync_one_symbol`（mock akshare），验证返回 dict 含 `status` 键
  - `test_sync_timeout_records_failed_count`: 改为真实触发 TimeoutError（mock `_sync_one_symbol` 阻塞），验证 `task.failed_count += 1` 和 errors_json 含"同步超时"
  - `test_universe_refresh_returns_seen_and_created`: 改为真实调用（mock akshare 返回 DataFrame），验证返回 dict 含 seen/created
  - `test_fetch_history_retries_only_once_per_source`: 改为 mock akshare 失败，验证调用次数 = 1 次/源
  - `test_sync_one_symbol_uses_independent_db_session`: 改为真实调用（mock akshare），验证子线程 Session 与主线程 Session 不是同一对象
  - 保留 `test_probe_result_fields_complete`、`test_probe_latency_ms_is_int_or_null`（已是真实行为测试）
  - 保留 `test_universe_refresh_timeout_returns_none_and_records_error`（已是真实行为测试）
  - 保留 `test_watchdog_does_not_overwrite_terminal_status` 但改为真实调用 watchdog，验证终态任务 updated_at 不变
  - 保留 `test_probe_akshare_api_content_type_header`（已是真实行为测试）
- 重写 `tests/test_whitebox_interaction.py` → 改为前端组件测试（移到 P1 阶段）
  - 删除源码字符串断言，改为 Vitest 组件测试（见 P1-2）
- **验证**：`pytest tests/test_whitebox_data_correctness.py -v` 全部通过，无 `inspect.getsource()` 调用

#### P0-4 后端数据计算链路测试补强（维度3 - 数据正确性）
- 新建 `tests/test_whitebox_data_calc_chain.py`（~15 用例）
  - 胜率除零保护：`total_trades=0` 时 win_rate 不为 Infinity（验证 `_compute_win_rate` 或类似函数）
  - 评分计算链路：`calculate_symbol_score` 输入空 bars / 不足 5 bars / 正常 bars，验证输出 score 在 [0, 100]
  - percent 计算链路：`TASK_STAGE_PERCENT` 各阶段映射正确性
  - universe seen=0 中止：mock `_refresh_discovery_universe` 返回 `{"seen": 0}`，验证任务立即 failed
  - data_credibility 边界：`bar_count < 5` 时不抛 NameError（已有测试，补强边界）
  - priority_score 加权：`timing*0.4 + quality*0.3 + liquidity*0.2 + breadth*0.1` 验证
  - _clamp_score 边界：负数/超 100/NaN 输入
  - _grade 分级映射：90=A/80=B/70=C/60=D/else=F
- **验证**：`pytest tests/test_whitebox_data_calc_chain.py -v` 全部通过

#### P0-5 测试标记规范化
- 为所有 `tests/test_*.py` 文件添加 marker
  - `test_blackbox_api.py`、`test_blackbox_api_mgmt.py` → `@pytest.mark.blackbox`
  - 其余 `test_whitebox_*.py` → `@pytest.mark.whitebox`
- **验证**：`pytest -m whitebox` 仅收集白盒，`pytest -m blackbox` 仅收集黑盒

---

### P1 阶段：功能显示 + 交互自动化

> 目标：覆盖维度1（功能显示）和维度2（交互正常），建立前端组件测试体系，补强后端空白模块 API 测试。

#### P1-1 核心组件渲染测试（维度1 - 功能显示）
- 新建 `frontend/src/components/__tests__/AkshareApiManager.test.tsx`（~12 用例）
  - 渲染表格 18 行接口记录
  - 列完整：名称/分类/状态/策略/延时/累计调用/最后探测/最后调用/操作
  - 分类 Tag 颜色映射正确（行情/基本面/资金流/ETF/通用）
  - 状态列：未探测=灰、成功=绿、失败=红、禁用=灰
  - 策略列 Select 下拉含 5 选项
  - 延时列非 custom 显示固定区间；custom 显示两个 InputNumber
  - Card header 含「批量探测」「刷新」按钮
  - 含 QuestionCircleOutlined Tooltip
  - Empty 占位（无数据时）
  - loading skeleton（首次加载时）
- 新建 `frontend/src/components/__tests__/Discovery.test.tsx`（~15 用例）
  - 任务列表/当前任务区域可见
  - 标的列含 code + name + 维度强项标签
  - 维度得分 Tooltip 颜色按分数区分（绿/蓝/橙/红）
  - 步骤指示器 5 步
  - 候选池 Tab 颜色区分（all 灰/highQuality 绿/highTiming 蓝/actionable 橙/overheatRisk 红/lowCredibility 紫）
  - 进度条显示百分比 + processed/total
  - 卡死检测 Alert 显示/消失
  - Empty 占位
- 新建 `frontend/src/components/__tests__/ExternalDataSync.test.tsx`（~8 用例）
  - 顶部数据源下拉 3 选项
  - 北向复选框默认勾选
  - 3 个同步卡片
  - 结果 Alert 显示 total/success/skipped/failed
- 新建 `frontend/src/components/__tests__/ScoringConfigSettings.test.tsx`（~6 用例）
  - 预设列表可见（4 股票 + 5 ETF）
  - 激活预设高亮
  - 维度配置表显示趋势/动量/波动/流动性/题材
  - 含 Tooltip 说明
- 新建 `frontend/src/components/__tests__/PortfolioWorkbench.test.tsx`（~8 用例）
  - K 线图加载
  - 候选池表格渲染
  - 评分看板数字显示
  - 切换语言后文案更新
- 新建 `frontend/src/components/__tests__/Trading.test.tsx`（~6 用例）
  - 模拟交易买入/卖出表单
  - 持仓列表
  - 胜率显示不为 Infinity
- **验证**：`npm test` 全部通过，组件渲染覆盖率 ≥ 60%

#### P1-2 核心组件交互测试（维度2 - 交互正常）
- 在上述组件测试文件中追加交互用例
  - `AkshareApiManager.test.tsx` 追加（~10 用例）
    - Switch 启用/禁用 → 立即提交，Toast 成功
    - 策略切换为「保守」→ 立即提交，延时显示「1000-2000ms」
    - 策略切换为「自定义」→ 显示 InputNumber + 保存按钮
    - 自定义 min=500/max=2000 → 提交成功
    - 自定义 min=2000/max=500 → 400 错误提示
    - 单接口探测 → loading → 30s 内反馈
    - 批量探测 → 3 个一组并发 → 全部 180s 内完成
    - 批量探测期间单按钮 disabled（真实点击验证，非源码断言）
    - 批量探测完成后按钮恢复可点击
    - 批量探测汇总 Toast 内容验证
  - `Discovery.test.tsx` 追加（~12 用例）
    - 创建任务（scope=cn-etf, limit=5）→ queued → running → done
    - 暂停 → 状态 paused
    - 恢复（24h 内）→ 继续 processing
    - 恢复（超 24h）→ 标记 expired
    - 取消 → 状态 cancelled
    - 重试 → 重新 queued
    - 卡死检测：2 分钟无进度 → 显示 Alert
    - Alert 内取消按钮可点击
    - 错误详情 Modal 可打开
    - 步骤指示器前进
    - 进度条更新
    - 候选池 Tab 切换
  - `ExternalDataSync.test.tsx` 追加（~5 用例）
    - 同步股质 → loading → 结果 Alert
    - 同步资金流 → loading → 结果 Alert
    - 同步 ETF 指标 → loading → 结果 Alert
    - 切换数据源 → 后续同步用新源
    - 单 symbol 卡死 90s 超时跳过
  - `ScoringConfigSettings.test.tsx` 追加（~3 用例）
    - 切换激活预设 → Toast 成功
    - 编辑维度权重 → 保存成功
    - 评分详情显示维度分 + 外部因子分
- **验证**：`npm test` 全部通过，交互用例覆盖核心场景

#### P1-3 后端空白模块 API 测试（维度3 - 输入输出一致性）
- 新建 `tests/test_whitebox_alerts.py`（~8 用例）
  - GET /alerts 列表返回字段完整
  - POST /alerts 创建告警
  - PATCH /alerts/{id} 更新状态
  - DELETE /alerts/{id} 删除
  - 边界：不存在的 id 返回 404
- 新建 `tests/test_whitebox_task_center.py`（~6 用例）
  - GET /async-tasks 列表
  - GET /async-tasks/{id} 详情
  - DELETE /async-tasks/{id} 清理
  - 边界：过期任务清理逻辑
- 新建 `tests/test_whitebox_investment_center.py`（~8 用例）
  - GET /portfolios 列表
  - POST /portfolios 创建组合
  - GET /sim-accounts 模拟账户
  - POST /sim-accounts 买入/卖出
  - 胜率计算不出现 Infinity（除零保护）
- 新建 `tests/test_whitebox_signal_rules.py`（~6 用例）
  - GET /signal-rules 列表
  - POST /signal-rules 创建
  - PUT /signal-rules/{id} 更新
  - DELETE /signal-rules/{id} 删除
- 新建 `tests/test_whitebox_trade_setups.py`（~5 用例）
  - GET /trade-setups 列表
  - POST /trade-setups 创建
  - 边界：无效 symbol_id 返回 404
- 新建 `tests/test_whitebox_watchlists_portfolios.py`（~8 用例）
  - GET /watchlists 列表
  - POST /watchlists 添加
  - DELETE /watchlists/{id} 移除
  - GET /portfolios 持仓列表
  - 边界：重复添加观察池
- **验证**：`pytest tests/test_whitebox_alerts.py tests/test_whitebox_task_center.py ... -v` 全部通过

#### P1-4 UAT 清单补全空白模块（维度4 - UAT 阶段）
- 修改 `docs/uat-checklist.md` 新增 7 节
  - 8. 告警中心（显示/交互/数据 ~6 项）
  - 9. 任务中心（显示/交互/数据 ~5 项）
  - 10. 投资中心（显示/交互/数据 ~8 项）
  - 11. 回测配置（显示/交互/数据 ~5 项）
  - 12. 条件构建器（显示/交互/数据 ~4 项）
  - 13. 自定义指标（显示/交互/数据 ~4 项）
  - 14. 数据库配置（显示/交互/数据 ~4 项）
- **验证**：UAT 清单总项数从 80+ 增至 110+，覆盖全部模块

---

### P2 阶段：E2E 关键链路 + 端到端验证

> 目标：覆盖维度2 和维度3 的端到端链路，验证"输入→处理→输出"全链路正确性。

#### P2-1 E2E 基础设施搭建
- 修改 `requirements.txt` 新增 `pytest-playwright`
- 修改 `pytest.ini` 新增 `e2e` marker 和 testpaths
- 新建 `tests/e2e/conftest.py`（live_backend / live_frontend / browser fixtures）
- 迁移 `test_ui_smoke.py` → `tests/e2e/test_ui_smoke.py`（改造为 pytest 格式）
- 迁移 `test_sim_buy.py` → `tests/e2e/test_sim_buy.py`（改造为 pytest 格式）
- **验证**：`playwright install chromium` 成功；`pytest tests/e2e/test_ui_smoke.py -v` 通过

#### P2-2 E2E 关键链路测试（维度2 + 维度3 端到端）
- 新建 `tests/e2e/test_discovery_flow.py`（~5 用例）
  - 完整链路：创建机会挖掘任务（cn-etf, limit=5）→ 等待完成 → 查看候选池 → 加入观察池 → 模拟买入
  - 完整链路：创建 cn-stock 任务 → universe 刷新成功（不报 Excel 错误）→ 同步完成
  - 卡死检测：任务 2 分钟无进度 → Alert 显示 → 点击取消
  - 重试：failed 任务重试 → 保留进度
  - 暂停/恢复：暂停 → 24h 内恢复 → 继续
- 新建 `tests/e2e/test_api_mgmt_flow.py`（~4 用例）
  - 完整链路：接口管理 → 探测 → 禁用 → 评分流程跳过禁用接口
  - 批量探测：3 个一组并发 → 全部完成 → 汇总 Toast
  - 自定义延时：设置 min=500/max=2000 → 保存 → 验证生效
  - 禁用接口后仍可探测（禁用不影响探测）
- 新建 `tests/e2e/test_scoring_flow.py`（~3 用例）
  - 完整链路：配置评分预设 → 运行扫描 → 查看评分详情
  - 维度权重调整 → 重新评分 → 分数变化
  - 外部因子缺失 → 按 policy 降级（neutral=50/penalty=30）
- 新建 `tests/e2e/test_external_data_sync_flow.py`（~3 用例）
  - 完整链路：外部数据同步 → 股质 + 资金流 + ETF 指标 → 结果 Alert
  - 单 symbol 卡死 90s 超时跳过 → 继续下一个
  - 空观察池同步 → total=0，不报错
- **验证**：`pytest tests/e2e/ -v` 全部通过（需前后端运行）

#### P2-3 后端空白模块补全（维度3 - 输入输出）
- 新建 `tests/test_whitebox_custom_indicators.py`（~5 用例）
- 新建 `tests/test_whitebox_db_config.py`（~4 用例）
- 新建 `tests/test_whitebox_journals.py`（~4 用例）
- 新建 `tests/test_whitebox_market_events.py`（~4 用例）
- **验证**：`pytest tests/test_whitebox_custom_indicators.py ... -v` 全部通过

---

### P3 阶段：UAT 执行 + CI 自动化

> 目标：执行 UAT 清单，建立 CI 自动化守护，输出测试报告。

#### P3-1 UAT 清单手动执行（维度4 - UAT 阶段）
- 按 `docs/uat-checklist.md` 逐项执行
  - 0.1-0.5 稳定性核心 25 项（最高优先级）
  - 1-5 功能回归 ~50 项
  - 6 边界场景 ~18 项
  - 7 潜在 Bug 验证 ~6 项
  - 8-14 新增模块 ~36 项
- 新建 `docs/uat-execution-record-v1.md`（执行记录模板）
  - 每项含：操作步骤 / 预期 / 实际 / 通过失败标记 / 截图路径 / 备注
- **验证**：UAT 清单 110+ 项全部执行，通过率 ≥ 95%

#### P3-2 UAT 关键项自动化守护（维度4 - UAT 阶段）
- 将 UAT 0.1-0.5 稳定性核心 25 项中尚未自动化的项转化为自动化测试
  - 已自动化：探测超时、universe 超时、watchdog 心跳、单 symbol 超时
  - 待自动化：批量探测 3 个一组并发（前端组件测试已覆盖，补 E2E）、批量探测 180s 内完成、卡死检测 Alert 含取消按钮
- 新建 `tests/test_blackbox_stability_guard.py`（~8 用例）
  - 批量探测 180s 内完成（黑盒）
  - 连续探测不退化（黑盒）
  - 卡死任务自动 failed（黑盒）
  - universe seen=0 中止（黑盒）
- **验证**：`pytest tests/test_blackbox_stability_guard.py -v` 全部通过

#### P3-3 CI 自动化守护
- 新建 `.github/workflows/test.yml`（或 `.gitlab-ci.yml`）
  - 后端白盒测试：`pytest -m whitebox`（无需后端运行）
  - 前端测试：`cd frontend && npm ci && npm test`
  - 测试覆盖率报告
- 新建 `.pre-commit-config.yaml`
  - 运行 `pytest -m whitebox`（快速白盒）
  - 运行 `npm test`（前端）
- 修改 `requirements.txt` 新增测试依赖分组
  ```
  # 测试依赖
  pytest>=8.0.0
  httpx>=0.27.0
  pytest-playwright>=1.4.0
  ```
- **验证**：pre-commit hook 触发时自动运行测试

#### P3-4 测试报告与文档
- 新建 `docs/test-architecture.md`（测试架构文档）
  - 测试分层：单元（白盒）/ 集成（黑盒）/ E2E / UAT
  - 运行命令速查
  - 覆盖率目标
- 修改 `README.md` 新增测试章节
  - 如何运行各类测试
  - 如何编写新测试
- **验证**：文档完整，新人可按文档运行测试

---

## 五、文件清单总览

### 5.1 新建文件（~30 个）

**前端测试基础设施**：
- `frontend/vitest.config.ts`
- `frontend/src/test/setup.ts`
- `frontend/src/test/render.tsx`

**前端单元测试**：
- `frontend/src/utils/__tests__/format.test.ts`
- `frontend/src/utils/__tests__/indicators.test.ts`
- `frontend/src/utils/__tests__/trade-plan.test.ts`

**前端组件测试**：
- `frontend/src/components/__tests__/AkshareApiManager.test.tsx`
- `frontend/src/components/__tests__/Discovery.test.tsx`
- `frontend/src/components/__tests__/ExternalDataSync.test.tsx`
- `frontend/src/components/__tests__/ScoringConfigSettings.test.tsx`
- `frontend/src/components/__tests__/PortfolioWorkbench.test.tsx`
- `frontend/src/components/__tests__/Trading.test.tsx`

**后端白盒测试**：
- `tests/test_whitebox_data_calc_chain.py`
- `tests/test_whitebox_alerts.py`
- `tests/test_whitebox_task_center.py`
- `tests/test_whitebox_investment_center.py`
- `tests/test_whitebox_signal_rules.py`
- `tests/test_whitebox_trade_setups.py`
- `tests/test_whitebox_watchlists_portfolios.py`
- `tests/test_whitebox_custom_indicators.py`
- `tests/test_whitebox_db_config.py`
- `tests/test_whitebox_journals.py`
- `tests/test_whitebox_market_events.py`

**E2E 测试**：
- `tests/e2e/conftest.py`
- `tests/e2e/test_ui_smoke.py`（迁移）
- `tests/e2e/test_sim_buy.py`（迁移）
- `tests/e2e/test_discovery_flow.py`
- `tests/e2e/test_api_mgmt_flow.py`
- `tests/e2e/test_scoring_flow.py`
- `tests/e2e/test_external_data_sync_flow.py`

**黑盒稳定性守护**：
- `tests/test_blackbox_stability_guard.py`

**文档**：
- `docs/uat-execution-record-v1.md`
- `docs/test-architecture.md`

**CI**：
- `.github/workflows/test.yml`
- `.pre-commit-config.yaml`

### 5.2 修改文件（~10 个）

- `frontend/package.json` — 新增依赖和 test 脚本
- `frontend/tsconfig.json` — 添加 test 类型
- `pytest.ini` — 新增 e2e marker 和 testpaths
- `requirements.txt` — 新增测试依赖
- `tests/test_whitebox_data_correctness.py` — 重写为真实行为测试
- `tests/test_whitebox_interaction.py` — 删除源码断言（迁移到前端组件测试）
- `tests/test_blackbox_api.py` — 添加 @pytest.mark.blackbox
- `docs/uat-checklist.md` — 补全 7 个空白模块
- `README.md` — 新增测试章节

### 5.3 删除文件（~2 个）

- 根目录 `test_api_smoke.py`（迁移到 tests/e2e/ 或删除）
- 根目录 `test_investment_center.py`（迁移到 tests/test_whitebox_investment_center.py）

---

## 六、验证标准

### 6.1 各阶段验收标准

| 阶段 | 验收标准 | 用例数 |
|------|---------|--------|
| P0 | 前端测试基础设施可用；utils 单元测试覆盖率 ≥ 80%；源码字符串断言全部替换为真实行为测试；后端数据计算链路测试通过 | ~70 新增 |
| P1 | 核心组件渲染测试覆盖率 ≥ 60%；交互测试覆盖核心场景；后端空白模块 API 测试通过；UAT 清单补全 7 模块 | ~110 新增 |
| P2 | E2E 关键链路（4 条）通过；后端空白模块补全；Playwright 集成到 pytest | ~25 新增 |
| P3 | UAT 清单 110+ 项执行完毕，通过率 ≥ 95%；CI 自动化守护建立；测试文档完整 | ~8 新增 |

### 6.2 4 维度最终覆盖对照

| 维度 | 自动化覆盖 | 手动覆盖 | 验收标准 |
|------|-----------|---------|---------|
| 维度1 功能显示 | 前端组件渲染测试 ~55 用例 | UAT ~40 项 | 核心组件渲染测试通过 + UAT 显示项全通过 |
| 维度2 交互正常 | 前端组件交互测试 ~30 用例 + E2E ~15 用例 | UAT ~30 项 | 交互测试通过 + E2E 链路通过 + UAT 交互项全通过 |
| 维度3 数据正确性 | 前端 utils ~55 用例 + 后端计算链路 ~15 用例 + API 输入输出 ~50 用例 | UAT ~15 项 | utils 覆盖率 ≥ 80% + 计算链路测试通过 + API 测试通过 |
| 维度4 UAT 阶段 | 稳定性核心自动化守护 ~8 用例 | UAT 110+ 项 | UAT 全部执行 + 通过率 ≥ 95% + CI 守护建立 |

### 6.3 整体测试用例数目标

| 类型 | 现有 | 新增 | 总计 |
|------|------|------|------|
| 后端白盒 | ~252 | ~90 | ~342 |
| 后端黑盒 | ~42 | ~8 | ~50 |
| 前端单元 | 0 | ~55 | ~55 |
| 前端组件 | 0 | ~85 | ~85 |
| E2E | 0（散落） | ~15 | ~15 |
| **总计** | **~294** | **~253** | **~547** |

---

## 七、假设与决策

### 7.1 关键决策

1. **前端测试框架选 Vitest + Testing Library**
   - 理由：Vite 项目原生支持，配置简单；Testing Library 是 React 组件测试事实标准
   - 替代方案：Jest（配置复杂，与 Vite 不兼容需额外处理）

2. **E2E 框架选 Playwright + pytest-playwright**
   - 理由：项目已有 2 个 Playwright 脚本，复用现有投资；pytest-playwright 集成到现有 pytest 体系
   - 替代方案：Cypress（需额外安装，与 Python 测试体系分离）

3. **源码字符串断言全部替换为真实行为测试**
   - 理由：源码字符串断言只验证"代码写了什么"，不验证"代码做了什么"，重构易失效
   - 决策：P0 阶段重写 `test_whitebox_data_correctness.py`；P1 阶段删除 `test_whitebox_interaction.py` 的源码断言，迁移到前端组件测试

4. **UAT 清单保留手动执行 + 关键项自动化守护**
   - 理由：UAT 80+ 项全自动化成本高且维护难；稳定性核心 25 项自动化已足够守护关键场景
   - 决策：P3 阶段手动执行 UAT 全量，同时将稳定性核心转化为 `test_blackbox_stability_guard.py`

5. **测试标记规范化在 P0 阶段完成**
   - 理由：marker 不规范导致 `pytest -m blackbox` 不可靠，影响后续阶段筛选
   - 决策：P0-5 统一添加 marker

### 7.2 假设

- 前端依赖安装（`npm install vitest @testing-library/react jsdom`）不会与现有 antd/echarts 冲突
- Playwright chromium 安装在 Windows 环境可用
- 后端空白模块的 API 端点行为与现有 route 实现一致（需在 P1 阶段探索确认）
- UAT 清单执行时前后端环境可用

### 7.3 风险与缓解

| 风险 | 缓解措施 |
|------|---------|
| 前端组件测试因 antd ConfigProvider 上下文复杂导致 render 失败 | 提供自定义 `render` helper 包裹必要 Provider |
| E2E 测试依赖真实后端 + 数据，环境搭建复杂 | conftest.py 检查可达性，不可达时 skip 而非 fail |
| 后端空白模块 API 行为未知，测试用例可能不准 | P1 阶段先 Read route 文件确认行为再写测试 |
| UAT 执行耗时（110+ 项手动） | 分批执行，优先 0.1-0.5 稳定性核心 25 项 |
| CI 环境与本地环境差异 | 使用 `requirements.txt` 锁定依赖，Docker 化（可选）|

---

## 八、实施顺序与依赖

```
P0-1 前端基础设施
  ↓
P0-2 utils 单元测试  ←  P0-3 替换源码断言（并行）
  ↓                    ↓
P0-4 后端计算链路     P0-5 测试标记规范化（并行）
  ↓
P1-1 组件渲染测试
  ↓
P1-2 组件交互测试  ←  P1-3 后端空白模块（并行）
  ↓                    ↓
P1-4 UAT 清单补全      ↓
  ↓                    ↓
P2-1 E2E 基础设施      ↓
  ↓                    ↓
P2-2 E2E 关键链路  ←  P2-3 后端空白模块补全（并行）
  ↓
P3-1 UAT 手动执行  ←  P3-2 稳定性自动化守护（并行）
  ↓                    ↓
P3-3 CI 自动化         P3-4 测试文档（并行）
```

---

## 九、备注

- 本计划为"完善计划"阶段，不立即执行测试
- 用户确认后按 P0 → P1 → P2 → P3 顺序实施
- 每个阶段完成后输出阶段性报告，确认无回归后再进入下一阶段
- 第三方接口稳定性（v2 修复）已在前序会话完成验证，本计划聚焦测试体系完善，不重复稳定性修复工作
