# Personal Quant Workbench - Code Wiki

> 版本: 0.1.0 | 最后更新: 2026-07-01

---

## 目录

1. [项目概述](#1-项目概述)
2. [项目架构总览](#2-项目架构总览)
3. [目录结构](#3-目录结构)
4. [后端架构](#4-后端架构)
   - 4.1 [核心启动与配置](#41-核心启动与配置)
   - 4.2 [数据层 (Models)](#42-数据层-models)
   - 4.3 [数据库管理](#43-数据库管理)
   - 4.4 [API 路由层](#44-api-路由层)
   - 4.5 [服务层 (Services)](#45-服务层-services)
   - 4.6 [Schema 层 (Pydantic)](#46-schema-层-pydantic)
5. [前端架构](#5-前端架构)
   - 5.1 [技术栈与构建](#51-技术栈与构建)
   - 5.2 [入口与路由](#52-入口与路由)
   - 5.3 [全局状态管理 (AppContext)](#53-全局状态管理-appcontext)
   - 5.4 [API 客户端层](#54-api-客户端层)
   - 5.5 [组件清单](#55-组件清单)
   - 5.6 [类型系统](#56-类型系统)
   - 5.7 [国际化 (i18n)](#57-国际化-i18n)
6. [核心业务流程](#6-核心业务流程)
7. [依赖关系图](#7-依赖关系图)
8. [项目运行方式](#8-项目运行方式)
9. [数据库迁移](#9-数据库迁移)
10. [附录：关键设计决策与约定](#10-附录关键设计决策与约定)

---

## 1. 项目概述

**Personal Quant Workbench** 是一个个人量化投资工作台系统，提供从数据采集、标的评分、信号挖掘、交易计划生成到模拟交易的全链路量化投资辅助能力。

核心功能包括：

- **市场数据管理**：支持 A股/美股/ETF 多源行情数据采集与同步
- **智能评分引擎**：基于多维度因子（趋势/动量/波动/流动性/广度/事件）的综合评分系统
- **标的发掘**：自动化标的发掘与扫描，支持自定义指标筛选
- **交易计划**：基于评分和风控规则自动生成交易计划与分批建仓方案
- **模拟交易**：完整的模拟账户系统，支持委托/成交/持仓/盈亏跟踪
- **回测引擎**：支持条件树(v2)和扁平条件(v1)两种规则配置的回测系统
- **宏观分析**：中美宏观经济数据采集与评分
- **新闻/事件**：市场新闻和事件采集、情绪分类与风险评级
- **工作台视图**：整合持仓、候选池、评分、K线的统一工作台

---

## 2. 项目架构总览

```
┌─────────────────────────────────────────────────────────┐
│                   Frontend (React + Antd)                 │
│  ┌────────┐ ┌──────────┐ ┌─────────┐ ┌──────────────┐  │
│  │ App.tsx│ │AppContext │ │api/     │ │components/   │  │
│  │(路由)  │ │(全局状态) │ │client.ts│ │(18个业务组件) │  │
│  └────┬───┘ └────┬─────┘ └────┬────┘ └──────────────┘  │
│       └──────────┴───────────┬┘                          │
│                              │ HTTP (REST API)           │
└──────────────────────────────┼───────────────────────────┘
                               │
┌──────────────────────────────┼───────────────────────────┐
│                   Backend (FastAPI)                       │
│                              │                            │
│  ┌───────────────────────────┴───────────────────────┐   │
│  │              API Router (/api/v1)                  │   │
│  │  ┌─────────────────────────────────────────────┐  │   │
│  │  │  Routes (20个路由模块, ~60个端点)            │  │   │
│  │  └─────────────┬───────────────────────────────┘  │   │
│  └────────────────┼──────────────────────────────────┘   │
│                   │                                       │
│  ┌────────────────┴──────────────────────────────────┐   │
│  │            Services (21个服务模块)                  │   │
│  │  analysis │ market_data │ backtest │ discovery ... │   │
│  └────────────────┬──────────────────────────────────┘   │
│                   │                                       │
│  ┌────────────────┴──────────────────────────────────┐   │
│  │     ORM Models (25个模型类, 25张数据表)            │   │
│  └────────────────┬──────────────────────────────────┘   │
│                   │                                       │
│  ┌────────────────┴──────────────────────────────────┐   │
│  │     DatabaseManager (SQLite ↔ MySQL 热切换)       │   │
│  └───────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────┘
```

---

## 3. 目录结构

```
dataAanlystNew/
├── app/                          # 后端应用根目录
│   ├── main.py                   # FastAPI 应用入口、生命周期管理
│   ├── core/                     # 核心配置
│   │   ├── config.py             # Settings、数据库配置管理
│   │   └── async_utils.py        # 异步工具（run_sync 桥接）
│   ├── db/                       # 数据库层
│   │   ├── base.py               # SQLAlchemy DeclarativeBase
│   │   ├── manager.py            # DatabaseManager 单例
│   │   ├── session.py            # 会话管理、依赖注入
│   │   ├── dialect.py            # 跨方言函数（days_since）
│   │   └── init_db.py            # 建表与兼容性补丁
│   ├── models/                   # ORM 模型 (25个)
│   ├── schemas/                  # Pydantic 请求/响应模式 (20个)
│   ├── api/                      # API 层
│   │   └── routes/               # 路由模块 (20个)
│   ├── services/                 # 业务服务 (21个)
│   └── web/                      # 前端构建产物挂载点
│       ├── static/               # 旧版静态资源
│       └── dist/                 # React 构建输出（构建后生成）
├── frontend/                     # 前端项目
│   ├── src/
│   │   ├── App.tsx               # 应用主组件（路由/布局）
│   │   ├── main.tsx              # React 入口
│   │   ├── api/                  # API 客户端
│   │   ├── components/           # 业务组件 (18个)
│   │   ├── context/              # 全局状态 AppContext
│   │   ├── hooks/                # 自定义 Hooks
│   │   ├── i18n/                 # 国际化
│   │   ├── types/                # TypeScript 类型定义
│   │   ├── utils/                # 工具函数
│   │   ├── constants/            # 常量定义
│   │   └── styles/               # 样式文件
│   ├── vite.config.ts            # Vite 构建配置
│   ├── package.json              # 前端依赖
│   └── tsconfig.json             # TypeScript 配置
├── alembic/                      # 数据库迁移
│   ├── env.py                    # Alembic 环境配置
│   └── versions/                 # 迁移脚本
├── config/                       # 运行时配置（数据库连接等）
├── alembic.ini                   # Alembic 主配置
├── requirements.txt              # Python 依赖
└── start.bat                     # Windows 启动脚本
```

---

## 4. 后端架构

### 4.1 核心启动与配置

#### `app/main.py` — 应用入口

| 组件 | 说明 |
|------|------|
| `lifespan()` | 异步生命周期管理：初始化数据库 → 启动清理 → 定期清理后台任务 → 关闭连接池 |
| `_periodic_cleanup()` | 每30分钟清理过期发掘结果的异步任务 |
| `app` | FastAPI 实例，标题 "Personal Quant Workbench API"，版本 0.1.0 |
| SPA 兜底路由 | `/{full_path:path}` 处理前端路由，不拦截 `/api/`、`/static/`、`/assets/` |

**启动流程**：
1. 加载数据库配置（SQLite 或 MySQL）
2. 初始化 DatabaseManager 单例
3. 创建数据库表（`init_db()`）
4. 执行启动清理（移除过期发掘结果）
5. 启动定期清理后台任务
6. 挂载 API 路由和静态资源

#### `app/core/config.py` — 配置管理

| 类/函数 | 说明 |
|---------|------|
| `Settings` | 全局配置：`app_name`、`api_prefix="/api/v1"`、`database_url`（默认 SQLite） |
| `load_db_config()` | 从 `config/db_config.json` 读取数据库配置 |
| `save_db_config()` | 保存数据库配置，限制文件权限 |
| `build_mysql_url()` | 构建 MySQL 连接 URL（密码自动 URL 编码） |

#### `app/core/async_utils.py` — 异步工具

| 函数 | 说明 |
|------|------|
| `run_sync(func, *args, **kwargs)` | 在 asyncio 执行器中运行同步函数，避免阻塞事件循环 |

---

### 4.2 数据层 (Models)

共 **25 个 ORM 模型类**，映射 **25 张数据库表**。

#### 核心实体关系

```
symbols (核心标的表)
  ├── daily_bars (1:N, 日线行情)
  ├── scores (1:N, 评分记录)
  ├── factor_values (1:N, 因子值)
  ├── scan_results (1:N, 扫描结果)
  ├── positions (1:N, 持仓)
  ├── sim_orders → sim_trades (1:N, 模拟交易)
  ├── trade_setups → trade_signals (1:N, 交易计划与信号)
  ├── journal_entries (1:N, 交易日志)
  ├── news_events / news_snapshots (1:N, 新闻)
  └── watchlist_items (1:N, 自选)

portfolios (投资组合)
  ├── portfolio_rules (1:N, 风控规则)
  ├── positions (1:N, 持仓)
  ├── backtest_runs → backtest_trades (1:N, 回测)
  ├── scan_runs → scan_results (1:N, 扫描)
  ├── signal_rules (1:N, 信号规则)
  ├── cash_ledger (1:N, 现金流水)
  ├── sim_orders → sim_trades (1:N, 模拟交易)
  ├── trade_setups (1:N, 交易计划)
  └── journal_entries (1:N, 交易日志)

scan_runs (扫描运行)
  └── scan_results (1:N, 扫描结果)
       └── discovery_result_states (1:1, 发掘状态)

custom_indicators (自定义指标)
  └── custom_indicator_versions (1:N, 版本历史)

factors (因子)
  └── factor_values (1:N, 因子值)

watchlists (自选列表)
  └── watchlist_items (1:N, 列表项)
```

#### 模型清单

| 模型类 | 表名 | 说明 | 关键字段 |
|--------|------|------|----------|
| `Symbol` | `symbols` | 标的证券 | symbol(UNIQUE), name, asset_type, market, region |
| `DailyBar` | `daily_bars` | 日线行情 | symbol_id(FK), trade_date, OHLCV, source |
| `Score` | `scores` | 综合评分 | symbol_id(FK), trade_date, quality/timing/priority_score, 6项子评分 |
| `Portfolio` | `portfolios` | 投资组合 | name(UNIQUE), total_capital, investable_ratio, is_default |
| `PortfolioRule` | `portfolio_rules` | 风控规则 | portfolio_id(FK), max_single/sector/stock/etf_position_pct |
| `Position` | `positions` | 持仓 | portfolio_id(FK), symbol_id(FK), quantity, avg_cost |
| `ScanPreset` | `scan_presets` | 扫描预设 | name(UNIQUE), scope_type, filters_json |
| `ScanRun` | `scan_runs` | 扫描运行 | preset_id(FK), status, scope_snapshot |
| `ScanResult` | `scan_results` | 扫描结果 | scan_run_id(FK), symbol_id(FK), result_type, rank_no |
| `TradeSetup` | `trade_setups` | 交易计划 | portfolio/symbol/score_id(FK), entry_min/max, stop_loss, target |
| `TradeSignal` | `trade_signals` | 交易信号 | trade_setup_id(FK), signal_type, signal_level, status |
| `SignalRule` | `signal_rules` | 信号规则 | portfolio_id(FK), mode, quality/timing_tolerance |
| `SimOrder` | `sim_orders` | 模拟委托 | portfolio/symbol_id(FK), side, quantity, filled_price |
| `SimTrade` | `sim_trades` | 模拟成交 | portfolio/symbol/order_id(FK), side, amount, fee |
| `CashLedger` | `cash_ledger` | 现金流水 | portfolio_id(FK), entry_type, amount, balance_after |
| `JournalEntry` | `journal_entries` | 交易日志 | portfolio/symbol/trade_setup/score_id(FK), entry_type, title |
| `Watchlist` | `watchlists` | 自选列表 | name(UNIQUE), list_type |
| `WatchlistItem` | `watchlist_items` | 自选列表项 | watchlist/symbol_id(FK) |
| `MarketEvent` | `market_events` | 市场事件 | title, impact_scope, importance_level(1-5), sentiment |
| `NewsEvent` | `news_events` | 新闻事件 | symbol_id(FK), sentiment, strength, effective_score |
| `NewsSnapshot` | `news_snapshots` | 新闻快照 | portfolio/symbol_id(FK), message_score, confidence |
| `MacroIndicatorValue` | `macro_indicator_values` | 宏观指标 | region, indicator_key, value, score, status |
| `MacroSnapshot` | `macro_snapshots` | 宏观快照 | region, market_score, stance, 5项分项评分 |
| `AsyncTaskRecord` | `async_tasks` | 异步任务 | task_type, status, stage, percent, message |
| `DiscoveryTaskRecord` | `discovery_tasks` | 发掘任务 | status, stage, scope, min_score, batch_size |
| `DiscoveryResultState` | `discovery_result_states` | 发掘结果状态 | scan_result_id(FK,UNIQUE), is_frozen, warning/valid_days |
| `DiscoveryPlan` | `discovery_plans` | 发掘计划 | name(UNIQUE), logic(AND/OR), pool_tab, filters_json |
| `CustomIndicator` | `custom_indicators` | 自定义指标 | name/key(UNIQUE), formula, value_type, scope_json |
| `CustomIndicatorVersion` | `custom_indicator_versions` | 指标版本 | indicator_id(FK), version, formula |
| `BacktestRun` | `backtest_runs` | 回测运行 | portfolio_id(FK), 收益/回撤/夏普/胜率等统计 |
| `BacktestTrade` | `backtest_trades` | 回测交易 | run_id(FK), entry/exit_date/price, pnl |
| `BacktestRuleTemplate` | `backtest_rule_templates` | 回测规则模板 | name(UNIQUE), rule_config |
| `Factor` | `factors` | 因子定义 | code(UNIQUE), category, direction |
| `FactorValue` | `factor_values` | 因子值 | symbol/factor_id(FK), trade_date, raw/normalized_value |

---

### 4.3 数据库管理

#### `app/db/manager.py` — DatabaseManager

| 方法 | 说明 |
|------|------|
| `get()` | 双重检查锁获取单例实例（线程安全） |
| `initialize(url, db_type)` | 创建新引擎 + 会话工厂，释放旧连接池 |
| `engine` | 只读属性，当前 SQLAlchemy Engine |
| `session_factory` | 只读属性，当前 sessionmaker |
| `db_type` / `is_mysql` / `is_sqlite` | 数据库类型判断 |
| `get_session()` | 创建新数据库会话 |
| `dispose()` | 关闭连接池释放资源 |

**SQLite 配置**：`check_same_thread=False`, `timeout=30`

**MySQL 配置**：`pool_pre_ping=True`, `pool_recycle=3600`, `pool_size=5`, `max_overflow=10`

#### `app/db/session.py` — 会话管理

| 组件 | 说明 |
|------|------|
| `get_db()` | FastAPI `Depends()` 注入用的 session 生成器 |
| `get_engine()` | 获取当前引擎 |
| `get_session_local()` | 获取会话工厂 |
| `_EngineProxy` / `_SessionLocalProxy` | 延迟求值代理，确保获取当前活动实例 |

#### `app/db/dialect.py` — 方言适配

| 函数 | 说明 |
|------|------|
| `days_since(date_column)` | 跨方言"距今天数"表达式（SQLite: julianday / MySQL: DATEDIFF） |

#### `app/db/init_db.py` — 数据库初始化

| 函数 | 说明 |
|------|------|
| `init_db()` | 创建表 + 执行兼容性补丁 |
| `_ensure_sqlite_columns()` | 通用 SQLite 补列工具 |
| `_convert_myisam_to_innodb()` | MySQL MyISAM → InnoDB 转换 |

**初始化流程**：创建目录 → MySQL 转引擎 → `create_all()` → SQLite 补列 → `PRAGMA WAL/busy_timeout`

---

### 4.4 API 路由层

所有路由统一注册在 `api_router` 下，前缀 `/api/v1`。

#### 路由端点汇总

| 路由模块 | 标签 | 端点数 | 核心端点 |
|---------|------|--------|---------|
| `market_data` | market-data | 12 | 同步任务(CRUD)、历史初始化、数据修复、日线导入/查询 |
| `discovery` | discovery | 11 | 发掘任务(CRUD+暂停/恢复/取消)、结果管理、指标评估、清理 |
| `portfolios` | portfolios | 8 | 组合CRUD、规则创建、仓位管理、分配计算 |
| `sim_accounts` | sim-accounts | 2 | 模拟账户快照、下单 |
| `dashboard` | dashboard | 3 | 概览、工作台、标的详情 |
| `backtest` | backtest | 8 | 回测运行/列表/详情/删除、规则模板CRUD |
| `scans` | scans | 4 | 扫描运行、结果查询、最新可执行 |
| `scores` | scores | 3 | 评分计算、最新评分、评分历史 |
| `trade_setups` | trade-setups | 3 | 交易计划生成、查询、分批更新 |
| `signal_rules` | signal-rules | 5 | 预设列表、信号统计、规则CRUD、预览 |
| `symbols` | symbols | 3 | 标的列表(分页)、创建、查询 |
| `watchlists` | watchlists | 2 | 列表、创建 |
| `news` | news | 2 | 新闻更新、最新新闻 |
| `macro` | macro | 3 | 宏观概览、数据更新、指标历史 |
| `market_events` | market-events | 7 | 事件CRUD、采集、范围选项 |
| `journals` | journals | 4 | 日志CRUD |
| `system` | system | 5 | 数据健康、备份/恢复、导出 |
| `db_config` | settings | 5 | 数据库配置CRUD、连接测试、迁移 |
| `custom_indicators` | settings | 5 | 自定义指标CRUD、预览 |
| `discovery_plans` | settings | 4 | 发掘计划CRUD |

---

### 4.5 服务层 (Services)

#### 核心服务

| 服务 | 文件 | 核心函数 | 说明 |
|------|------|----------|------|
| **评分引擎** | `analysis.py` | `calculate_symbol_score()` | 综合评分：质量分+时机分+6项子维度，自动判定阶段/动作 |
| **行情同步** | `market_data.py` | `sync_symbol_daily_bars()`, `sync_market_data()` | 多源行情采集（AKShare/Sina/腾讯），历史初始化分阶段任务 |
| **异步同步** | `market_data_sync_task.py` | `create_market_data_sync_task()`, `_run_market_data_sync()` | 四阶段异步同步：准备→同步+评分→扫描→完成 |
| **回测引擎** | `backtest.py` | `run_backtest()`, `_evaluate_condition_tree()` | v1/v2条件树、AST沙箱公式求值、技术指标计算、仓位管理、成本模拟 |
| **发掘系统** | `discovery_tasks.py` | `create_discovery_task()`, `_run_discovery_task()` | 后台发掘任务，支持暂停/恢复/取消 |
| **发掘结果** | `discovery_results.py` | `update_discovery_result()`, `evaluate_discovery_indicators()` | 结果管理、自定义指标批量评估 |
| **发掘清理** | `discovery_cleanup.py` | `cleanup_expired_discovery_results()` | 基于valid_days和is_frozen清理过期结果 |
| **通用异步任务** | `async_tasks.py` | `create_async_task()`, `cancel_async_task()`, `_start_worker()` | 生命周期管理、30分钟超时自动过期、终态保护 |
| **交易计划** | `trade_plans.py` | `upsert_trade_setup()`, `build_trade_setup_view()` | 入场/止损/目标计算、收益场景、分批建仓方案 |
| **仓位分配** | `allocation.py` | `compute_allocation()`, `compute_position_budget()` | 基于规则/评分/风险的仓位预算计算 |
| **扫描引擎** | `scans.py` | `run_scan()` | 基于评分和规则过滤标的，生成ScanRun/ScanResult |
| **模拟交易** | `sim_accounts.py` | `place_sim_order()`, `build_sim_account_summary()` | 买入/卖出、现金流水、持仓管理、A股100股/手 |
| **新闻** | `news.py` | `update_news()`, `get_latest_news()` | AKShare新闻采集、情绪分类、时间衰减权重 |
| **市场事件** | `market_events.py` | `collect_market_events()` | 央视/百度/财新/期货新闻采集，自动分类 |
| **宏观** | `macro.py` | `update_macro_data()`, `get_macro_overview()` | FRED(美)/AKShare(中)采集，指标评分，快照机制 |
| **信号统计** | `signal_stats.py` | `build_similar_signal_stats()` | 历史相似信号查找，前向收益统计 |
| **信号规则** | `signal_rules.py` | `preset_list()`, `upsert_active_signal_rule()` | 预设/自定义规则管理 |
| **迁移** | `migration.py` | `run_migration()` | SQLite→MySQL 拓扑排序迁移，并发保护 |
| **辅助** | `regions.py` | `region_from_market()` | 区域-市场映射 |
| **辅助** | `symbol_names.py` | `refresh_symbol_name()`, `resolve_symbol_name()` | 标的名称解析和刷新 |
| **辅助** | `akshare_utils.py` | `quiet_akshare_output()` | AKShare输出静默 |

#### 服务间调用关系

```
market_data_sync_task ──→ async_tasks, market_data, analysis, allocation, scans, symbol_names, trade_plans
discovery_tasks ──→ market_data, analysis, trade_plans, scans, allocation, symbol_names
discovery_results ──→ analysis, backtest(_resolve_formula_expr)
market_data ──→ akshare_utils, analysis, allocation, symbol_names
dashboard(路由) ──→ allocation, signal_stats, sim_accounts, trade_plans, regions
custom_indicators(路由) ──→ backtest(_resolve_formula_expr, _validate_formula_expr)
```

---

### 4.6 Schema 层 (Pydantic)

共 **20 个 Schema 模块**，约 **80 个 Schema 类**。遵循 Create/Update/Read 三件套模式：

- **Create**：必填字段
- **Update**：全部可选
- **Read**：带 `from_attributes=True` 支持 ORM 转换

| Schema 模块 | 核心模式 | 说明 |
|-------------|----------|------|
| `dashboard` | `DashboardWorkbench` | 工作台聚合响应（组合+规则+市场范围+概览+候选池+持仓+日志等） |
| `market_data` | `MarketDataUpdateRequest`, `HistoryInitializationStatus` | 行情同步请求、历史初始化状态（含阶段进度） |
| `backtest` | `BacktestRunRequest`, `BacktestRunDetail`, `ConditionGroup` | 回测请求(v1/v2)、详情、条件树 |
| `discovery` | `DiscoveryTaskCreate`, `DiscoveryTaskRead` | 发掘任务请求与状态 |
| `portfolio` | `PortfolioCreate`, `AllocationSummary`, `PositionRead` | 组合、仓位分配、持仓 |
| `score` | `ScoreCalculationRequest`, `ScoreRead` | 评分请求与响应 |
| `trade_setup` | `TradeSetupGenerateRequest`, `TradeSetupRead` | 交易计划生成与读取 |
| `signal_rule` | `SignalRuleUpsert`, `SignalRulePreviewRead` | 信号规则与预览 |
| `sim_account` | `SimOrderCreate`, `SimAccountSnapshot` | 模拟委托与账户快照 |
| `news` | `NewsUpdateRequest`, `NewsUpdateResponse` | 新闻更新请求与响应 |
| `macro` | `MacroUpdateRequest`, `MacroOverviewResponse` | 宏观更新与概览 |
| `market_event` | `MarketEventCreate`, `MarketEventListResponse` | 市场事件CRUD与列表 |
| `async_task` | `AsyncTaskRead`, `MarketDataSyncCreate` | 异步任务状态与创建 |
| `db_config` | `DbConfigUpdate`, `MigrationProgress` | 数据库配置与迁移进度 |
| `custom_indicator` | `CustomIndicatorCreate`, `CustomIndicatorPreviewRead` | 自定义指标与预览 |
| `discovery_plan` | `DiscoveryPlanCreate`, `DiscoveryPlanFilter` | 发掘计划与筛选条件 |
| `journal` | `JournalCreate`, `JournalRead` | 交易日志 |
| `scan` | `ScanRunCreate`, `ScanResultRead` | 扫描运行与结果 |
| `symbol` | `SymbolCreate`, `SymbolRead` | 标的 |
| `watchlist` | `WatchlistCreate`, `WatchlistItemRead` | 自选列表 |

---

## 5. 前端架构

### 5.1 技术栈与构建

| 技术 | 版本 | 用途 |
|------|------|------|
| React | ^18.3 | UI 框架 |
| Ant Design | ^5.21 | UI 组件库 |
| ECharts | ^5.5 | 图表库 |
| dayjs | ^1.11 | 日期处理 |
| TypeScript | ^5.6 | 类型安全 |
| Vite | ^5.4 | 构建工具 |

**构建配置** (vite.config.ts)：
- 开发服务器端口 5173，代理 `/api` 和 `/static` 到后端 8000
- 构建输出到 `../app/web/dist`
- 手动分块：vendor-react、vendor-antd、vendor-charts

### 5.2 入口与路由

前端采用**标签页式路由**，无 URL 路由，通过 `activeTab` 状态切换视图：

| Tab | 组件 | 说明 |
|-----|------|------|
| `decision` | `TodayDecision` | 今日决策 |
| `investment` | `InvestmentCenter` | 投资中心（整合视图） |
| `portfolio` | `PortfolioWorkbench` + `Trading` | 工作台（含子标签：工作台/交易） |
| `discovery` | `Discovery` | 标的发掘 |
| `macro` | `MacroData` | 宏观数据 |
| `news` | `MarketNews` | 市场新闻 |
| `settings` | `Settings` | 系统设置 |

### 5.3 全局状态管理 (AppContext)

使用 React Context + useState 实现全局状态管理，无第三方状态库。

**AppState 核心字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `portfolios` / `portfolioId` | `Portfolio[]` / `number` | 投资组合列表与当前选中 |
| `locale` | `"zh-CN" \| "en-US"` | 国际化语言 |
| `marketGroup` | `string` | 市场筛选（all/cn/us） |
| `activeTab` / `activeSubTab` | `string` | 当前标签页 |
| `workbench` | `DashboardWorkbench` | 工作台数据 |
| `detail` / `detailCache` | `SymbolDetail` / `Record` | 标的详情与缓存（最多4个） |
| `signalRule` / `signalRulePresets` | `SignalRule` / `SignalRulePreset[]` | 信号规则配置 |
| `discoveryTask` | `DiscoveryTask` | 当前发掘任务 |
| `newsSnapshot` | `NewsSnapshot` | 新闻快照 |
| `syncTask` / `syncPolling` | `any` / `boolean` | 同步任务与轮询状态 |
| `globalLoading` | `boolean` | 全局加载指示器 |

**核心操作方法**：

| 方法 | 说明 |
|------|------|
| `loadWorkbench()` | 加载工作台数据 |
| `loadSymbolDetail(symbolId)` | 加载标的详情（含缓存） |
| `runSync()` / `cancelSync()` | 触发/取消市场数据同步（2s心跳轮询） |
| `runScan()` | 执行扫描 |
| `runDiscoveryMining(config)` | 启动发掘任务（2s轮询） |
| `runNewsUpdate()` | 更新新闻 |
| `generateTradeSetup(overrides)` | 生成交易计划 |
| `submitSimOrder(side)` | 提交模拟订单 |
| `addSymbolFromInput(code)` | 添加标的到自选 |
| `updateSignalRule(partial)` | 更新信号规则（350ms防抖预览） |
| `saveSignalRule()` | 保存信号规则 |

### 5.4 API 客户端层

`api/client.ts` 封装了所有后端 API 调用，基于 `requestJson()` 统一处理：

- **请求计数**：`activeRequests` 追踪进行中的请求数，驱动全局加载指示器
- **超时控制**：默认20秒，特殊接口60/120秒
- **错误处理**：统一解析 HTTP 错误响应，AbortError 转为超时提示

**API 方法清单（约50+个）**：

| 分类 | 方法 | 超时 |
|------|------|------|
| 组合 | getPortfolios, getWorkbench, getSymbolDetail | 20s |
| 标的 | getSymbols, getAllSymbols, createSymbol | 20s |
| 自选 | getWatchlistItems, addWatchlistItem | 20s |
| 行情 | syncMarketData, repairSymbolMarketData, createMarketDataSyncTask, getMarketDataSyncTask, cancelMarketDataSyncTask | 20-60s |
| 历史 | startHistoryInitialization, getHistoryInitializationStatus, cancelHistoryInitialization, retryHistoryInitializationFailed | 20-30s |
| 扫描 | createScanRun | 60s |
| 评分 | calculateScores | 120s |
| 交易计划 | generateTradeSetup, saveTradeSetupTranches | 60s |
| 信号 | getSignalRulePresets, getSignalRule, saveSignalRule, previewSignalRule | 20s |
| 模拟 | submitSimOrder | 20s |
| 持仓 | getPositions, upsertPosition, deletePosition, upsertPortfolioRule, getAllocation | 20s |
| 新闻 | updateNews, getLatestNews | 20s |
| 宏观 | getMacroOverview, updateMacroData, getMacroIndicatorHistory | 20-60s |
| 发掘 | getDiscoveryTasks, createDiscoveryTask, sendDiscoveryCommand, getDiscoveryScopeStats, updateDiscoveryResult, refreshDiscoveryResult, evaluateDiscoveryIndicators, cleanupDiscoveryResults | 20s |
| 事件 | getMarketEvents, collectMarketEvents, getMarketEventScopes | 20-60s |
| 日志 | getJournals, createJournal, updateJournal, deleteJournal | 20s |
| 回测 | runBacktest, getBacktestRuns, getBacktestRun, deleteBacktestRun, getBacktestTemplates, createBacktestTemplate, updateBacktestTemplate, deleteBacktestTemplate | 20-120s |
| 指标 | getCustomIndicators, createCustomIndicator, updateCustomIndicator, previewCustomIndicator, deleteCustomIndicator | 20s |
| 计划 | getDiscoveryPlans, createDiscoveryPlan, updateDiscoveryPlan, deleteDiscoveryPlan | 20s |
| 系统 | getDataHealth, backupDatabase, listBackups, restoreDatabase, exportData | 20-120s |

### 5.5 组件清单

| 组件 | 文件 | 说明 |
|------|------|------|
| `App` | App.tsx | 主组件：标签页路由、工具栏、全局加载条 |
| `TodayDecision` | TodayDecision.tsx | 今日决策视图 |
| `InvestmentCenter` | InvestmentCenter.tsx | 投资中心整合视图 |
| `PortfolioWorkbench` | PortfolioWorkbench.tsx | 工作台：持仓列表+候选池+标的详情 |
| `Trading` | Trading.tsx | 交易面板：K线图+模拟下单 |
| `TradingPanel` | TradingPanel.tsx | 交易面板子组件 |
| `Discovery` | Discovery.tsx | 标的发掘：任务管理+结果筛选+计划配置 |
| `MacroData` | MacroData.tsx | 宏观经济数据展示 |
| `MarketNews` | MarketNews.tsx | 市场新闻与事件 |
| `Settings` | Settings.tsx | 系统设置：数据库配置+指标+发掘计划+历史初始化 |
| `DetailModal` | DetailModal.tsx | 标的详情弹窗 |
| `MetricModal` | MetricModal.tsx | 指标详情弹窗 |
| `BacktestConfig` | BacktestConfig.tsx | 回测配置面板 |
| `BacktestResult` | BacktestResult.tsx | 回测结果展示 |
| `ConditionBuilder` | ConditionBuilder.tsx | 条件树构建器（回测规则配置） |
| `CustomIndicatorSettings` | CustomIndicatorSettings.tsx | 自定义指标设置 |
| `DbConfigSection` | DbConfigSection.tsx | 数据库配置区域 |
| `DiscoveryPlanSettings` | DiscoveryPlanSettings.tsx | 发掘计划设置 |
| `HistoryInitSection` | HistoryInitSection.tsx | 历史数据初始化区域 |

### 5.6 类型系统

`types/index.ts` 定义了约 **50+ 个 TypeScript 接口**，与后端 Schema 一一对应。核心类型包括：

- `Symbol`, `Score`, `Position`, `Portfolio` — 基础实体
- `DashboardWorkbench`, `SymbolDetail` — 聚合响应
- `TradeSetup`, `SignalStats`, `SignalRule` — 交易与信号
- `BacktestRun`, `BacktestTrade`, `ConditionGroup` — 回测
- `DiscoveryTask`, `DiscoveryPlan`, `CustomIndicator` — 发掘
- `NewsEvent`, `NewsSnapshot`, `MarketEvent` — 新闻与事件
- `MacroOverview`, `MacroIndicator` — 宏观
- `DataHealth`, `DbConfig`, `MigrationProgress` — 系统配置
- `HistoryInitializationTask` — 历史初始化

### 5.7 国际化 (i18n)

- 支持 **中文 (zh-CN)** 和 **英文 (en-US)**
- `t(key)` — 翻译函数
- `template(key, params)` — 带参数的模板翻译
- `setLocale(locale)` — 切换语言
- `regionLongLabel(region)` — 区域长标签

---

## 6. 核心业务流程

### 6.1 市场数据同步流程

```
用户点击"同步" → AppContext.runSync()
  → api.createMarketDataSyncTask() 创建异步任务
  → 启动2s心跳轮询 api.getMarketDataSyncTask()
  → 后端: market_data_sync_task._run_market_data_sync()
    阶段1: 准备(0-5%)  — 解析标的列表
    阶段2: 同步+评分(5-90%) — 逐标同步行情→计算评分→生成交易计划
    阶段3: 扫描(90-98%) — 自动扫描
    阶段4: 完成(98-100%)
  → 轮询检测完成 → showToast + loadWorkbench
```

### 6.2 评分计算流程

```
calculate_symbol_score(symbol_id, trade_date)
  1. 获取最近N天K线数据
  2. 计算技术指标: SMA/EMA/RSI/MACD/布林/ATR/KDJ
  3. 计算子维度评分:
     - trend_score (趋势)
     - momentum_score (动量)
     - volatility_score (波动性)
     - liquidity_score (流动性)
     - breadth_score (广度)
     - event_score (事件)
     - breakout_score (突破)
     - pullback_score (回调)
     - overheat_penalty (过热惩罚)
  4. 综合评分: quality_score (0-100) + timing_score (0-100)
  5. 判定 stage (建仓/持有/减仓/空仓) 和 action (buy/hold/sell/exit)
  6. 计算 data_credibility (数据可信度)
  7. 写入 Score 记录
```

### 6.3 交易计划生成流程

```
upsert_trade_setup(portfolio_id, symbol_id, score_id)
  1. 获取最新评分和K线
  2. 计算入场区间 (entry_min/max)
  3. 计算止损价 (stop_loss)
  4. 计算目标价 (target_price)
  5. 计算风险收益比 (risk_reward_ratio)
  6. 计算推荐仓位 (基于评分+风控规则)
  7. 生成收益场景 (乐观/中性/悲观)
  8. 生成分批建仓计划 (tranche_plan)
  9. 生成未来买入计划 (future_buy_plan)
  10. 写入 TradeSetup 记录
```

### 6.4 回测执行流程

```
run_backtest(portfolio_id, symbol_ids, rule_config, cost_config)
  1. 加载标的日线数据和评分
  2. 逐日模拟:
     a. 检查买入条件 (v1扁平/v2条件树)
        - 评分阈值、阶段过滤、价格规则、量价过滤、自定义指标
     b. 检查卖出条件
        - 止盈/止损/移动止损/最大持有天数/评分动作
     c. 仓位管理
        - 固定比例/金额、最大持仓数
     d. 成本计算
        - 佣金+印花税+滑点
  3. 计算权益曲线
  4. 计算统计指标: 总收益/最大回撤/夏普比率/胜率/盈亏比
  5. 生成条件匹配追踪 (entry/exit_traces)
  6. 写入 BacktestRun + BacktestTrade 记录
```

---

## 7. 依赖关系图

### 后端 Python 依赖

```
fastapi==0.135.1          # Web 框架
uvicorn==0.42.0           # ASGI 服务器
sqlalchemy==2.0.48        # ORM
alembic==1.15.2           # 数据库迁移
akshare==1.18.30          # 中国市场数据源
pandas==2.3.3             # 数据处理
pymysql>=1.1.0            # MySQL 驱动
cryptography>=41.0.0      # 加密库
```

### 前端 NPM 依赖

```
react@^18.3.1             # UI 框架
antd@^5.21.0              # UI 组件库
@ant-design/icons@^5.5.1  # 图标库
echarts@^5.5.1            # 图表库
echarts-for-react@^3.0.2  # ECharts React 封装
dayjs@^1.11.13            # 日期处理
```

### 模块依赖关系

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│  Frontend   │────→│  API Routes  │────→│  Services    │
│  (React)    │     │  (FastAPI)   │     │  (Business)  │
└─────────────┘     └──────┬───────┘     └──────┬───────┘
                           │                     │
                           │    ┌────────────┐   │
                           └───→│  Schemas   │   │
                                │ (Pydantic) │   │
                                └────────────┘   │
                                                  │
                           ┌────────────┐         │
                           │   Models   │←────────┘
                           │  (SQLAlchemy)│
                           └──────┬─────┘
                                  │
                           ┌──────┴─────┐
                           │  Database  │
                           │ SQLite/MySQL│
                           └────────────┘
```

### 外部数据源依赖

| 数据源 | 用途 | 服务模块 |
|--------|------|----------|
| AKShare | A股/ETF行情、央视/百度/财新新闻、中国宏观 | market_data, discovery_tasks, market_events, news, macro |
| FRED | 美国宏观经济数据 | macro |
| 新浪财经 | A股行情备选源 | market_data |
| 腾讯财经 | A股行情备选源 | market_data |

---

## 8. 项目运行方式

### 环境要求

- Python 3.10+
- Node.js 18+
- npm 9+

### 快速启动

**方式一：使用启动脚本（Windows）**

```bash
# 双击运行
start.bat
```

这会启动后端服务（uvicorn）在 `http://localhost:8000`。

**方式二：手动启动**

```bash
# 后端
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 前端开发（另一个终端）
cd frontend
npm install
npm run dev
# 访问 http://localhost:5173（开发模式，API 代理到 8000）

# 前端构建
cd frontend
npm run build
# 构建产物输出到 app/web/dist，后端自动提供静态服务
```

### 访问地址

| 服务 | 地址 |
|------|------|
| 前端界面 | http://localhost:8000 （生产模式） / http://localhost:5173 （开发模式） |
| API 文档 | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/health |

### 数据库配置

默认使用 **SQLite**，数据库文件位于 `%TEMP%/quant_workbench.db`。

可通过前端设置页面切换到 **MySQL**：
1. 进入设置 → 数据库配置
2. 填写 MySQL 连接信息
3. 测试连接
4. 保存并迁移

配置文件存储在 `config/db_config.json`。

---

## 9. 数据库迁移

### Alembic 迁移

```bash
# 生成迁移脚本
alembic revision --autogenerate -m "description"

# 执行迁移
alembic upgrade head

# 回滚
alembic downgrade -1
```

### 注意事项

- `alembic/env.py` 目前缺少 `backtest`, `custom_indicator`, `discovery`, `discovery_plan`, `macro_data` 5个模型的导入，自动迁移可能无法感知这些表
- `init_db.py` 通过 `ALTER TABLE` 补列方式兼容旧版 SQLite 数据库升级
- SQLite 启用 WAL 模式和 30 秒忙等待超时

---

## 10. 附录：关键设计决策与约定

### 架构约定

| 约定 | 说明 |
|------|------|
| **单例模式** | `DatabaseManager` 线程安全单例，所有数据库访问通过 `get()` |
| **热切换** | 支持 SQLite ↔ MySQL 运行时切换，自动处理方言差异 |
| **异步任务** | 长时间运行操作（行情同步/发掘）使用后台线程+轮询模式 |
| **三件套 Schema** | Create(必填)/Update(可选)/Read(from_attributes) |
| **API 前缀** | 所有 API 统一 `/api/v1` 前缀 |
| **前端无路由** | 标签页式路由，无 URL 路由库，通过状态切换视图 |
| **Context 状态管理** | 使用 React Context + useState，无 Redux 等第三方库 |

### 工程约束

| 约束 | 说明 |
|------|------|
| **不破坏现有功能** | 所有新功能不得影响 P0-P3 级别功能 |
| **错误信息国际化** | catch 块中的错误信息必须使用 `t()` 函数 |
| **并发保护** | 异步任务必须实现防重复执行 |
| **用户反馈** | 所有 CRUD 和长时间操作必须有 loading 状态和 Toast 通知 |
| **数据可信度** | 评分计算需处理 bar_count < 5 等边界情况 |
| **渐进式进度** | 异步任务进度更新避免大跳跃（如 75%→90%） |
| **先提交后处理** | 关键数据操作先 commit，再执行后续步骤防回滚 |
| **可访问性** | 交互元素需 ARIA 属性和键盘支持 |
| **Ant Design Table** | 前端表格统一使用 Ant Design Table 组件 |
