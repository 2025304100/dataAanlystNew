# 测试架构文档

## 一、测试分层

本项目采用四层测试体系，覆盖功能显示、交互正常、数据正确性、UAT 阶段四个维度。

### 1. 单元测试（白盒，`@pytest.mark.whitebox`）

- **位置**：`tests/test_whitebox_*.py`
- **特点**：直接调用服务层/路由函数，用 `db_session` fixture（独立 SQLite 文件），无需后端运行
- **运行**：`pytest -m whitebox`
- **覆盖**：数据计算链路、API 路由逻辑、服务层函数、稳定性常量守护
- **文件清单**：
  - `test_whitebox_alerts.py` — 告警中心 API
  - `test_whitebox_akshare_http.py` — akshare HTTP 层加固
  - `test_whitebox_allocation.py` — 组合配置
  - `test_whitebox_analysis.py` — 评分分析
  - `test_whitebox_api_mgmt.py` — 接口管理服务层
  - `test_whitebox_async_tasks.py` — 异步任务终态保护
  - `test_whitebox_backtest_sandbox.py` — 回测沙箱
  - `test_whitebox_custom_indicators.py` — 自定义指标 + AST 沙箱
  - `test_whitebox_data_calc_chain.py` — 数据计算链路（胜率/可信度/分级）
  - `test_whitebox_data_correctness.py` — 数据正确性（真实行为测试）
  - `test_whitebox_db_config.py` — 数据库配置 + 密码脱敏
  - `test_whitebox_discovery.py` — 机会挖掘任务
  - `test_whitebox_discovery_timeout.py` — 超时保护
  - `test_whitebox_discovery_universe.py` — 全市场标的列表刷新
  - `test_whitebox_external_factors.py` — 外部因子
  - `test_whitebox_investment_center.py` — 投资中心 API
  - `test_whitebox_interaction.py` — 前端交互（源码结构守护）
  - `test_whitebox_journals.py` — 日志 API
  - `test_whitebox_probe_thread_pool.py` — 探测线程池
  - `test_whitebox_scoring_config.py` — 评分配置
  - `test_whitebox_signal_rules.py` — 信号规则 API
  - `test_whitebox_task_center.py` — 任务中心 API
  - `test_whitebox_trade_setups.py` — 交易计划 API
  - `test_whitebox_universe_refresh.py` — universe 刷新超时
  - `test_whitebox_watchlists_portfolios.py` — 观察池 API

### 2. 集成测试（黑盒，`@pytest.mark.blackbox`）

- **位置**：`tests/test_blackbox_*.py`
- **特点**：用 `httpx.Client` 直连运行中的后端（http://localhost:8000），验证端到端 HTTP 行为
- **运行**：`pytest -m blackbox`（需先启动后端）
- **覆盖**：API 端点可达性、字段完整性、错误处理、稳定性守护
- **文件清单**：
  - `test_blackbox_api.py` — 核心 API 烟雾测试
  - `test_blackbox_api_mgmt.py` — 接口管理端到端
  - `test_blackbox_stability_guard.py` — 稳定性自动化守护（P3-2）

### 3. E2E 测试（`@pytest.mark.e2e`）

- **位置**：`tests/e2e/test_*.py`
- **特点**：用 Playwright 驱动浏览器，验证前后端全链路
- **运行**：`pytest tests/e2e/ -m e2e`（需前后端同时运行 + `playwright install chromium`）
- **覆盖**：UI 烟雾、机会挖掘流程、接口管理流程、评分流程、外部数据同步流程
- **文件清单**：
  - `conftest.py` — fixtures（live_backend/live_frontend/browser/page）
  - `test_ui_smoke.py` — Tab 切换烟雾测试
  - `test_sim_buy.py` — 模拟买入流程
  - `test_discovery_flow.py` — 机会挖掘端到端
  - `test_api_mgmt_flow.py` — 接口管理端到端
  - `test_scoring_flow.py` — 评分配置端到端
  - `test_external_data_sync_flow.py` — 外部数据同步端到端

### 4. 前端测试（Vitest + Testing Library）

- **位置**：`frontend/src/**/*.{test,spec}.{ts,tsx}`
- **特点**：jsdom 环境，mock API/context/i18n，验证组件渲染和交互
- **运行**：`cd frontend && npm test`
- **覆盖**：utils 工具函数、6 个核心组件渲染 + 交互
- **文件清单**：
  - `src/utils/__tests__/format.test.ts` — 格式化工具（66 用例）
  - `src/utils/__tests__/indicators.test.ts` — 技术指标（38 用例）
  - `src/utils/__tests__/trade-plan.test.ts` — 交易计划（21 用例）
  - `src/components/__tests__/AkshareApiManager.test.tsx` — 接口管理组件
  - `src/components/__tests__/Discovery.test.tsx` — 机会挖掘组件
  - `src/components/__tests__/ExternalDataSync.test.tsx` — 外部数据同步组件
  - `src/components/__tests__/ScoringConfigSettings.test.tsx` — 评分配置组件
  - `src/components/__tests__/PortfolioWorkbench.test.tsx` — 组合工作台组件
  - `src/components/__tests__/Trading.test.tsx` — 模拟交易组件
  - `src/test/factories.ts` — mock 工厂

### 5. UAT 手动测试

- **位置**：`docs/uat-checklist.md`（167 项清单）
- **执行记录**：`docs/uat-execution-record-v1.md`
- **覆盖**：0-14 节，含稳定性核心、功能回归、边界场景、新增模块

---

## 二、运行命令速查

```bash
# 后端白盒测试（最快，无需后端运行）
pytest -m whitebox

# 前端测试
cd frontend && npm test

# 后端黑盒测试（需启动后端）
python -m uvicorn app.main:app --port 8000 &
pytest -m blackbox

# E2E 测试（需前后端 + 浏览器）
python -m uvicorn app.main:app --port 8000 &
cd frontend && npm run dev &
python -m playwright install chromium
pytest tests/e2e/ -m e2e

# 全量测试（按 marker 分组）
pytest -m whitebox    # 白盒
pytest -m blackbox    # 黑盒
pytest -m e2e         # E2E
pytest -m "not slow"  # 排除慢测试

# 覆盖率
cd frontend && npm run test:coverage
```

---

## 三、测试覆盖率目标

| 层级 | 目标 | 当前 |
|------|------|------|
| 后端白盒 | ≥ 80% 服务层函数 | ~252 用例 |
| 前端组件 | ≥ 60% 核心组件 | 6/6 核心组件 |
| 黑盒 API | ≥ 90% 端点可达 | ~47 用例 |
| E2E | 关键链路全覆盖 | 4 条链路 |
| UAT | 167 项清单 | 167 项 |

---

## 四、编写新测试指南

### 后端白盒测试

1. 文件命名：`tests/test_whitebox_<module>.py`
2. 开头添加：`pytestmark = pytest.mark.whitebox`
3. 用 `db_session` fixture（来自 `tests/conftest.py`）
4. 直接 import 路由函数，传 `db=db_session` 调用
5. 对于不依赖 db 的路由，用 `unittest.mock.patch` mock 服务层

### 前端组件测试

1. 文件命名：`frontend/src/components/__tests__/<Component>.test.tsx`
2. 用 `renderWithProviders`（来自 `src/test/render.tsx`）
3. 用 `makeMockApi`/`makeMockContext`（来自 `src/test/factories.ts`）
4. 优先用 `screen.getByText(t("key"))` 查询元素

### E2E 测试

1. 文件命名：`tests/e2e/test_<flow>_flow.py`
2. 开头添加：`pytestmark = pytest.mark.e2e`
3. 用 `page` fixture（自动跳到前端首页）
4. 优先用 `data-*` 属性或文本定位元素

---

## 五、CI 自动化

- **配置文件**：`.github/workflows/test.yml`
- **4 个 Job**：backend-whitebox / frontend / backend-blackbox / e2e
- **触发**：push/PR 到 main/master
- **pre-commit hook**：`.pre-commit-config.yaml`（提交前自动跑白盒 + 前端测试）
