# 个人量化研究工作台数据库设计草案

## 1. 设计目标

本设计以 MVP 需求文档为基础，优先支撑以下核心链路：

```text
标的管理
-> 行情入库
-> 因子计算
-> 评分生成
-> 机会扫描
-> 组合约束判断
-> 交易计划输出
-> 复盘记录
```

数据库设计遵循以下原则：

- 优先保证 MVP 可开发
- 结构清晰，避免过度抽象
- 关键结果可追溯
- 兼顾后续扩展到回测、公式版本化和 AI 消息面

MVP 推荐使用：

- 结构化数据：`SQLite`
- 行情明细和中间结果：`Parquet`

## 2. 分层建议

建议把数据分成 4 层：

### 2.1 基础主数据层

- `symbols`
- `watchlists`
- `watchlist_items`
- `portfolios`
- `portfolio_rules`

### 2.2 市场与计算结果层

- `daily_bars`
- `factors`
- `factor_values`
- `scores`

### 2.3 扫描与交易计划层

- `scan_presets`
- `scan_runs`
- `scan_results`
- `positions`
- `trade_setups`
- `trade_signals`
- `allocation_snapshots`

### 2.4 复盘与日志层

- `journal_entries`

## 3. 核心表设计

### 3.1 `symbols`

用途：保存标的基础信息，覆盖股票、ETF、指数等。

关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 主键 |
| `symbol` | TEXT UNIQUE | 代码，如 `600519` |
| `name` | TEXT | 名称 |
| `asset_type` | TEXT | `stock` / `etf` / `index` |
| `market` | TEXT | `sh` / `sz` / `bj` |
| `board` | TEXT | 主板、创业板、科创板等 |
| `industry` | TEXT | 行业 |
| `theme` | TEXT | 赛道 / 主题 |
| `is_st` | INTEGER | 是否 ST |
| `is_active` | INTEGER | 是否有效 |
| `listed_at` | DATE | 上市日期 |
| `created_at` | DATETIME | 创建时间 |
| `updated_at` | DATETIME | 更新时间 |

索引建议：

- `UNIQUE(symbol)`
- `INDEX(asset_type, market)`
- `INDEX(theme)`

### 3.2 `watchlists`

用途：保存标的池定义。

关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 主键 |
| `name` | TEXT UNIQUE | 如“观察池” |
| `list_type` | TEXT | `watch` / `trade` / `eliminate` / `custom` |
| `description` | TEXT | 说明 |
| `created_at` | DATETIME | 创建时间 |
| `updated_at` | DATETIME | 更新时间 |

### 3.3 `watchlist_items`

用途：保存标的池成员关系。

关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 主键 |
| `watchlist_id` | INTEGER FK | 关联 `watchlists.id` |
| `symbol_id` | INTEGER FK | 关联 `symbols.id` |
| `note` | TEXT | 备注 |
| `added_at` | DATETIME | 加入时间 |

约束建议：

- `UNIQUE(watchlist_id, symbol_id)`

### 3.4 `daily_bars`

用途：保存日线行情主索引数据。

说明：

- 如果数据量较大，可将完整 OHLCV 明细保存在 Parquet
- SQLite 中保留索引型字段或最近数据

关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 主键 |
| `symbol_id` | INTEGER FK | 关联 `symbols.id` |
| `trade_date` | DATE | 交易日 |
| `open` | REAL | 开盘价 |
| `high` | REAL | 最高价 |
| `low` | REAL | 最低价 |
| `close` | REAL | 收盘价 |
| `volume` | REAL | 成交量 |
| `amount` | REAL | 成交额 |
| `turnover_rate` | REAL | 换手率 |
| `source` | TEXT | 数据来源 |
| `created_at` | DATETIME | 入库时间 |

约束建议：

- `UNIQUE(symbol_id, trade_date)`

索引建议：

- `INDEX(trade_date)`
- `INDEX(symbol_id, trade_date DESC)`

### 3.5 `factors`

用途：保存因子定义。

关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 主键 |
| `code` | TEXT UNIQUE | 因子编码 |
| `name` | TEXT | 因子名称 |
| `category` | TEXT | 趋势、动量、波动等 |
| `direction` | TEXT | `positive` / `negative` / `neutral` |
| `status` | TEXT | `draft` / `testing` / `active` / `deprecated` |
| `description` | TEXT | 说明 |
| `formula_expr` | TEXT | 计算表达式或标识 |
| `created_at` | DATETIME | 创建时间 |
| `updated_at` | DATETIME | 更新时间 |

### 3.6 `factor_values`

用途：保存因子计算结果。

关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 主键 |
| `symbol_id` | INTEGER FK | 关联 `symbols.id` |
| `factor_id` | INTEGER FK | 关联 `factors.id` |
| `trade_date` | DATE | 交易日 |
| `raw_value` | REAL | 原始值 |
| `normalized_value` | REAL | 标准化值 |
| `calc_batch_id` | TEXT | 计算批次标识 |
| `created_at` | DATETIME | 生成时间 |

约束建议：

- `UNIQUE(symbol_id, factor_id, trade_date)`

索引建议：

- `INDEX(factor_id, trade_date)`
- `INDEX(symbol_id, trade_date DESC)`

### 3.7 `scores`

用途：保存评分结果，是 MVP 的核心结果表。

关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 主键 |
| `symbol_id` | INTEGER FK | 关联 `symbols.id` |
| `trade_date` | DATE | 评分日期 |
| `quality_score` | REAL | 股质评分 |
| `quality_grade` | TEXT | `A` / `B` / `C` / `D` |
| `timing_score` | REAL | 时点评分 |
| `stage` | TEXT | `start` / `accel` / `overheat` / `cooldown` |
| `action` | TEXT | `open` / `buy_dip` / `hold` / `reduce` / `exit` |
| `priority_score` | REAL | 执行优先级 |
| `trend_score` | REAL | 趋势维度分 |
| `momentum_score` | REAL | 动量维度分 |
| `volatility_score` | REAL | 波动维度分 |
| `liquidity_score` | REAL | 流动性维度分 |
| `breadth_score` | REAL | 赛道 / 宽度分 |
| `event_score` | REAL | 事件分 |
| `calc_batch_id` | TEXT | 计算批次 |
| `created_at` | DATETIME | 生成时间 |

约束建议：

- `UNIQUE(symbol_id, trade_date, calc_batch_id)`

索引建议：

- `INDEX(trade_date, priority_score DESC)`
- `INDEX(symbol_id, trade_date DESC)`
- `INDEX(stage, action)`

### 3.8 `scan_presets`

用途：保存扫描预设条件。

关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 主键 |
| `name` | TEXT UNIQUE | 预设名称 |
| `scope_type` | TEXT | `stock` / `etf` / `mixed` |
| `markets` | TEXT | JSON 字符串 |
| `boards` | TEXT | JSON 字符串 |
| `filters_json` | TEXT | 过滤条件 |
| `sort_mode` | TEXT | 排序模式 |
| `description` | TEXT | 说明 |
| `created_at` | DATETIME | 创建时间 |
| `updated_at` | DATETIME | 更新时间 |

说明：

- SQLite 中用 `TEXT` 存 JSON 即可，MVP 不必提前复杂拆表

### 3.9 `scan_runs`

用途：保存每次扫描执行记录。

关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 主键 |
| `preset_id` | INTEGER FK | 关联 `scan_presets.id` |
| `run_name` | TEXT | 扫描名称 |
| `scope_snapshot` | TEXT | 扫描范围快照 |
| `filters_snapshot` | TEXT | 条件快照 |
| `portfolio_id` | INTEGER FK | 关联 `portfolios.id` |
| `portfolio_rule_id` | INTEGER FK | 关联 `portfolio_rules.id` |
| `status` | TEXT | `pending` / `running` / `done` / `failed` |
| `started_at` | DATETIME | 开始时间 |
| `finished_at` | DATETIME | 完成时间 |
| `created_at` | DATETIME | 创建时间 |

### 3.10 `scan_results`

用途：保存扫描结果明细。

关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 主键 |
| `scan_run_id` | INTEGER FK | 关联 `scan_runs.id` |
| `symbol_id` | INTEGER FK | 关联 `symbols.id` |
| `result_type` | TEXT | `quality` / `timing` / `executable` |
| `rank_no` | INTEGER | 排名 |
| `quality_score` | REAL | 股质评分 |
| `timing_score` | REAL | 时点评分 |
| `priority_score` | REAL | 执行优先级 |
| `stage` | TEXT | 当前阶段 |
| `action` | TEXT | 动作建议 |
| `recommended_position_pct` | REAL | 建议仓位比例 |
| `is_sector_overweight` | INTEGER | 是否赛道超配 |
| `is_asset_overweight` | INTEGER | 是否资产超配 |
| `reason_tags` | TEXT | 入选原因标签 |
| `created_at` | DATETIME | 创建时间 |

索引建议：

- `INDEX(scan_run_id, result_type, rank_no)`
- `INDEX(symbol_id, created_at DESC)`

### 3.11 `portfolios`

用途：保存组合账户。

关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 主键 |
| `name` | TEXT UNIQUE | 组合名称 |
| `account_type` | TEXT | `simulated` / `real` |
| `total_capital` | REAL | 总资金 |
| `investable_ratio` | REAL | 可投资比例 |
| `cash_reserve_ratio` | REAL | 现金保留比例 |
| `currency` | TEXT | 币种 |
| `is_default` | INTEGER | 是否默认组合 |
| `created_at` | DATETIME | 创建时间 |
| `updated_at` | DATETIME | 更新时间 |

### 3.12 `portfolio_rules`

用途：保存组合规则，可单独版本化。

关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 主键 |
| `portfolio_id` | INTEGER FK | 关联 `portfolios.id` |
| `rule_name` | TEXT | 规则名称 |
| `max_single_position_pct` | REAL | 单标最大仓位 |
| `max_sector_position_pct` | REAL | 单赛道最大仓位 |
| `max_stock_position_pct` | REAL | 股票总仓上限 |
| `max_etf_position_pct` | REAL | ETF 总仓上限 |
| `max_loss_per_trade_pct` | REAL | 单笔最大亏损 |
| `max_open_positions` | INTEGER | 同时持仓上限 |
| `stage_limits_json` | TEXT | 各阶段仓位上限 |
| `is_active` | INTEGER | 是否当前启用 |
| `created_at` | DATETIME | 创建时间 |

说明：

- `stage_limits_json` 示例：

```json
{
  "stock": { "start": 0.08, "accel": 0.15, "overheat": 0.05, "cooldown": 0.0 },
  "etf": { "start": 0.10, "accel": 0.20, "overheat": 0.08, "cooldown": 0.0 }
}
```

### 3.13 `positions`

用途：保存当前持仓和成本信息。

关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 主键 |
| `portfolio_id` | INTEGER FK | 关联 `portfolios.id` |
| `symbol_id` | INTEGER FK | 关联 `symbols.id` |
| `quantity` | REAL | 持仓数量 |
| `avg_cost` | REAL | 持仓成本 |
| `latest_price` | REAL | 最新价格 |
| `market_value` | REAL | 当前市值 |
| `position_pct` | REAL | 当前仓位占比 |
| `asset_type` | TEXT | 股票 / ETF |
| `theme` | TEXT | 赛道冗余快照 |
| `opened_at` | DATETIME | 首次建仓时间 |
| `updated_at` | DATETIME | 更新时间 |

约束建议：

- `UNIQUE(portfolio_id, symbol_id)`

### 3.14 `trade_setups`

用途：保存单标的交易计划，是“建议输出”的核心表。

关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 主键 |
| `portfolio_id` | INTEGER FK | 关联 `portfolios.id` |
| `symbol_id` | INTEGER FK | 关联 `symbols.id` |
| `score_id` | INTEGER FK | 关联 `scores.id` |
| `scan_run_id` | INTEGER FK | 来源扫描 |
| `stage` | TEXT | 当前阶段 |
| `action` | TEXT | 建议动作 |
| `entry_min` | REAL | 买入区间下限 |
| `entry_max` | REAL | 买入区间上限 |
| `stop_loss` | REAL | 止损位 |
| `target_price` | REAL | 第一目标位 |
| `recommended_position_pct` | REAL | 建议仓位比例 |
| `recommended_position_amount` | REAL | 建议金额 |
| `risk_reward_ratio` | REAL | 风险收益比 |
| `allow_add_position` | INTEGER | 是否允许加仓 |
| `is_sector_overweight` | INTEGER | 是否赛道超配 |
| `is_asset_overweight` | INTEGER | 是否资产超配 |
| `setup_reason` | TEXT | 生成依据摘要 |
| `created_at` | DATETIME | 生成时间 |

索引建议：

- `INDEX(portfolio_id, created_at DESC)`
- `INDEX(symbol_id, created_at DESC)`

### 3.15 `trade_signals`

用途：保存每次系统输出的交易动作提示。

关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 主键 |
| `portfolio_id` | INTEGER FK | 关联 `portfolios.id` |
| `symbol_id` | INTEGER FK | 关联 `symbols.id` |
| `trade_setup_id` | INTEGER FK | 关联 `trade_setups.id` |
| `signal_type` | TEXT | `open` / `add` / `hold` / `reduce` / `exit` |
| `signal_level` | TEXT | `info` / `warn` / `strong` |
| `message` | TEXT | 提示内容 |
| `trigger_price` | REAL | 触发价 |
| `status` | TEXT | `active` / `ignored` / `done` |
| `created_at` | DATETIME | 创建时间 |

### 3.16 `allocation_snapshots`

用途：保存某个时点的组合暴露状态，便于追溯。

关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 主键 |
| `portfolio_id` | INTEGER FK | 关联 `portfolios.id` |
| `related_run_type` | TEXT | `scan` / `setup` / `manual` |
| `related_run_id` | INTEGER | 关联对象 ID |
| `total_position_pct` | REAL | 总仓位 |
| `stock_position_pct` | REAL | 股票仓位 |
| `etf_position_pct` | REAL | ETF 仓位 |
| `cash_pct` | REAL | 现金占比 |
| `sector_exposure_json` | TEXT | 赛道暴露 |
| `position_count` | INTEGER | 持仓数量 |
| `created_at` | DATETIME | 创建时间 |

### 3.17 `journal_entries`

用途：保存观察、交易与复盘记录。

关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 主键 |
| `portfolio_id` | INTEGER FK | 关联 `portfolios.id` |
| `symbol_id` | INTEGER FK | 关联 `symbols.id` |
| `trade_setup_id` | INTEGER FK | 可为空 |
| `entry_type` | TEXT | `watch` / `trade` / `review` |
| `title` | TEXT | 标题 |
| `content` | TEXT | 正文 |
| `subjective_view` | TEXT | 主观看法 |
| `follow_system` | INTEGER | 是否按系统执行 |
| `outcome` | TEXT | 结果 |
| `review_note` | TEXT | 复盘总结 |
| `created_at` | DATETIME | 创建时间 |
| `updated_at` | DATETIME | 更新时间 |

## 4. 表关系摘要

核心关系如下：

- `symbols` 1:N `daily_bars`
- `symbols` 1:N `factor_values`
- `symbols` 1:N `scores`
- `symbols` 1:N `scan_results`
- `symbols` 1:N `trade_setups`
- `symbols` 1:N `journal_entries`
- `watchlists` 1:N `watchlist_items`
- `portfolios` 1:N `portfolio_rules`
- `portfolios` 1:N `positions`
- `portfolios` 1:N `trade_setups`
- `portfolios` 1:N `trade_signals`
- `portfolios` 1:N `allocation_snapshots`
- `scan_runs` 1:N `scan_results`
- `scores` 1:N `trade_setups`

## 5. 推荐枚举值

为减少前后端歧义，建议统一以下枚举：

### 5.1 `asset_type`

- `stock`
- `etf`
- `index`

### 5.2 `stage`

- `start`
- `accel`
- `overheat`
- `cooldown`

### 5.3 `action`

- `open`
- `buy_dip`
- `hold`
- `reduce`
- `exit`

### 5.4 `result_type`

- `quality`
- `timing`
- `executable`

## 6. SQLite 与 Parquet 分工建议

推荐这样落地：

放 SQLite：

- 主数据
- 配置数据
- 评分结果
- 扫描结果
- 交易计划
- 复盘日志

放 Parquet：

- 全量行情历史
- 因子中间计算结果
- 大批量扫描明细中间文件

这样能兼顾开发便利和性能。

## 7. MVP 实现建议

第一版不建议做得太复杂，可以按下面方式落地：

1. 先建核心 10 张表：
   `symbols`、`watchlists`、`watchlist_items`、`daily_bars`、`factors`、`factor_values`、`scores`、`scan_runs`、`scan_results`、`trade_setups`
2. 再补组合相关表：
   `portfolios`、`portfolio_rules`、`positions`、`allocation_snapshots`
3. 最后补日志和信号：
   `trade_signals`、`journal_entries`

## 8. 下一步建议

基于这份表设计，最适合继续往下走的是：

1. 输出 SQLite `DDL` 草案
2. 设计后端 API 清单
3. 设计前端页面与状态流转

如果继续按开发顺序推进，建议先做 `DDL + API`，这样就能直接开始搭后端骨架。
