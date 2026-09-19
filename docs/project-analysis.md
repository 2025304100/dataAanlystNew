# 个人量化工作台 · 项目分析

> Personal Quant Workbench — 数据采集 → 评分/因子 → 机会发掘 → 组合决策 → 模拟交易/回测  
> 整理日期：2026-09-16

## 一句话定位

面向个人使用的 A 股 / 美股 / ETF 量化投资工作台：工程化程度较高，组合交易与机会中心接近可用；因子自动挖掘 v2 仍处早期骨架。

## 技术栈

| 层 | 技术 |
|----|------|
| 前端 | React 18 · TypeScript · Vite 5 · Ant Design 5 · ECharts · Vitest |
| 后端 | FastAPI · Uvicorn · SQLAlchemy 2 · Alembic · Pydantic |
| 存储 | SQLite（默认）/ MySQL（可热切）· DuckDB（因子仓） |
| 数据 / 量化 | akshare · pandas · numpy · scipy · scikit-learn · vectorbt |
| 工程 | GitHub Actions · pre-commit · 四层测试（白盒 / 前端 / 黑盒 / E2E） |

**规模概览**：约 6 个一级业务 Tab · 50+ API 路由模块 · ~550 测试用例 · 54+ Alembic 迁移。

## 系统架构

```
UI Tabs → api/client.ts → /api/v1 → services → DB / DuckDB
```

- **开发**：Vite 将 `/api/v1` 代理到 `http://localhost:8000`
- **生产**：前端构建到 `app/web/dist`，由 FastAPI 静态托管 + SPA 兜底
- **入口**：`app/main.py`、`frontend/src/App.tsx`
- **路由聚合**：`app/api/router.py`（`prefix=/api/v1`）
- **鉴权**：本地弱鉴权；可选 `STRICT_AUTH`（无完整用户登录体系）

## 一级功能与成熟度

| 模块 | 前端入口 | 成熟度 | 说明 |
|------|----------|--------|------|
| 今日决策 | `TodayDecision.tsx` | 可用 | 决策引擎 + 证据链联动 |
| 组合交易 | `portfolio-trading/*` | 高 | 概览 / 策略 / 回测 / 治理 G3–G7 |
| 机会中心 | `OpportunityCenter.tsx` | 高 | 候选 / 观察 / 排除 / 扫描 |
| 宏观 / 新闻 | `MacroData` / `MarketNews` | 可用 | 中美宏观 + 市场消息 |
| 设置中心 | `Settings.tsx` | 可用 | 数据 / 因子 / 调度 / AI / 通知 |
| 因子中心 | `components/factors/*` | 可用 | 库 / 评估 / Shadow / 模型 |
| 因子挖掘 v2 | `factor_mining` 路由 | 骨架 | 大量 `NotImplemented` / TODO |
| 标的研究 | `InvestmentCenter` 薄壳 | 兼容 | 一级入口已移除，深链接保留 |

前端无 `react-router`，使用 `activeTab` + `?tab=` 深链接；旧 `discovery` → `opportunity`，`investment` 仍兼容渲染。

## 数据闭环

### 采集入库

1. AKShare / 多源网关（限流 · 熔断 · 缓存）
2. 业务库：`symbols`、`daily_bars`、`universe_*`、评分 / 发掘 / 组合等
3. 因子仓：DuckDB（`FACTOR_WAREHOUSE_PATH`）

### 分析闭环

1. 评分 → 扫描发掘 → 候选状态流转
2. 因子流水线 → 决策引擎 → 订单计划
3. 模拟成交 / 回测 / 通知 Outbox / 定时调度

## 目录速览

| 路径 | 职责 |
|------|------|
| `app/` | 后端：API / services / models / db |
| `frontend/` | React 源码与 Vitest |
| `alembic/` | 数据库迁移链 |
| `config/` | `db_config` / `ai_config` / 密钥 |
| `docs/` | 设计、验收、测试架构文档 |
| `tests/` | 白盒 / 黑盒 / E2E |
| `scripts/` | 启停、备份、bootstrap |

## API 域一览

| 域 | 代表路由 / 模块 |
|----|-----------------|
| 行情 / 标的 | `market_data`、`symbols`、`universe`、`external_data` |
| 评分 / 扫描 / 发掘 | `scores`、`scans`、`discovery`、`scoring_facade` |
| 因子 | `factors`、`factor_pipeline`、`factor_evaluation`、`factor_mining` |
| 组合 / 交易 | `portfolios`、`sim_accounts`、`auto_trade`、`backtest`、`decision_engine` |
| 宏观 / 新闻 | `macro`、`news`、`market_events`、`investment_themes` |
| 系统 / AI | `system`、`scheduled_tasks`、`notifications`、`ai_*` |

## 关键配置与启动

| 项 | 路径 / 说明 |
|----|-------------|
| Python 依赖 | `requirements.txt` |
| 前端依赖 | `frontend/package.json` |
| Alembic | `alembic.ini`、`alembic/versions/*` |
| DB 配置 | `config/db_config.json` |
| AI 配置 | `config/ai_config.json` |
| 启动 | `start.bat` / `start.sh` → `scripts/dev_services*.py` |
| 测试说明 | `docs/test-architecture.md`、`README.md` |
| CI | `.github/workflows/test.yml` |

当前无 Docker / 标准 `.env.example`。常见环境变量见 `app/core/config.py`（如 `DATABASE_URL`、`FACTOR_*`、`STRICT_AUTH` 等）。

## 工程化与风险

### 做得好的

- 四层测试 + CI + pre-commit
- 统一错误协议、评分防腐层（`scoring_facade`）
- 组合治理与双跑门禁（G0–G7）
- 生命周期内调度 / 清理 / 异步 worker

### 待改进

- 因子挖掘 v2 端点大量未实现
- 旧组件与双轨 schema（Alembic + `init_db` 对齐）仍并存
- 根目录临时日志 / 调试脚本较多
- 无 Docker；本地 DB 凭据需防泄露到公开远端
- 仓库名 `dataAanlystNew` 拼写有误；`CODE_WIKI.md` 相对现状偏旧

## 测试快速命令

```bash
# 后端白盒（无需后端运行）
pytest -m whitebox

# 前端
cd frontend && npm test

# 黑盒（需先启动后端）
python -m uvicorn app.main:app --port 8000
pytest -m blackbox

# E2E
pytest tests/e2e/ -m e2e
```

## 参考

- `README.md` — 测试体系入口
- `CODE_WIKI.md` — 历史代码百科（偏旧）
- `docs/test-architecture.md` — 四层测试架构
- `docs/factor-mining-v2-skeleton/` — 因子挖掘 v2 迁入后存档（勿再改）
