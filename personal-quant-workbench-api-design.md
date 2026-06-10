# 个人量化研究工作台 MVP API 设计草案

## 1. 目标

本文件基于 MVP 需求文档和数据库设计草案，给出一版适合 `FastAPI` 落地的后端接口清单。

设计原则：

- 先覆盖 MVP 主线
- 接口语义清晰
- 方便前端三栏工作台调用
- 兼顾后续扩展

MVP 主线：

```text
标的池管理
-> 数据更新
-> 评分
-> 扫描
-> 仓位约束
-> 交易计划
-> 复盘
```

## 2. API 分组

建议按以下模块拆分：

- `symbols`
- `watchlists`
- `market-data`
- `factors`
- `scores`
- `scans`
- `portfolios`
- `positions`
- `trade-setups`
- `journals`
- `dashboard`

## 3. 通用约定

### 3.1 路由前缀

```text
/api/v1
```

### 3.2 通用响应格式

```json
{
  "success": true,
  "message": "",
  "data": {}
}
```

分页列表建议：

```json
{
  "success": true,
  "data": {
    "items": [],
    "total": 0,
    "page": 1,
    "page_size": 20
  }
}
```

### 3.3 枚举约定

- `asset_type`: `stock` / `etf` / `index`
- `stage`: `start` / `accel` / `overheat` / `cooldown`
- `action`: `open` / `buy_dip` / `hold` / `reduce` / `exit`

## 4. 标的与标的池

### 4.1 获取标的列表

`GET /api/v1/symbols`

查询参数：

- `keyword`
- `asset_type`
- `market`
- `board`
- `theme`
- `page`
- `page_size`

### 4.2 获取单个标的详情

`GET /api/v1/symbols/{symbol_id}`

返回：

- 基础信息
- 所属池
- 最新评分摘要

### 4.3 新增标的

`POST /api/v1/symbols`

请求体示例：

```json
{
  "symbol": "510300",
  "name": "沪深300ETF",
  "asset_type": "etf",
  "market": "sh",
  "board": "main",
  "industry": "broad_index",
  "theme": "index"
}
```

### 4.4 获取标的池列表

`GET /api/v1/watchlists`

### 4.5 新建标的池

`POST /api/v1/watchlists`

### 4.6 向标的池添加标的

`POST /api/v1/watchlists/{watchlist_id}/items`

请求体：

```json
{
  "symbol_id": 12,
  "note": "趋势观察"
}
```

### 4.7 从标的池删除标的

`DELETE /api/v1/watchlists/{watchlist_id}/items/{symbol_id}`

## 5. 数据更新

### 5.1 手动刷新行情

`POST /api/v1/market-data/update`

请求体：

```json
{
  "scope": "watchlist",
  "watchlist_id": 1,
  "asset_types": ["stock", "etf"]
}
```

返回：

- 更新任务 ID
- 状态
- 更新时间

### 5.2 查询更新任务状态

`GET /api/v1/market-data/jobs/{job_id}`

### 5.3 获取单标的 K 线

`GET /api/v1/market-data/bars/{symbol_id}`

查询参数：

- `start_date`
- `end_date`
- `limit`

## 6. 因子

### 6.1 获取因子列表

`GET /api/v1/factors`

### 6.2 新建因子

`POST /api/v1/factors`

### 6.3 更新因子

`PATCH /api/v1/factors/{factor_id}`

### 6.4 获取单个因子的最近值

`GET /api/v1/factors/{factor_id}/values`

查询参数：

- `symbol_id`
- `limit`

## 7. 评分

### 7.1 触发单标评分

`POST /api/v1/scores/calculate`

请求体：

```json
{
  "symbol_ids": [12],
  "trade_date": "2026-06-05"
}
```

### 7.2 获取单标最新评分

`GET /api/v1/scores/latest/{symbol_id}`

返回：

- `quality_score`
- `quality_grade`
- `timing_score`
- `stage`
- `action`
- 维度得分

### 7.3 获取评分历史

`GET /api/v1/scores/history/{symbol_id}`

查询参数：

- `start_date`
- `end_date`
- `limit`

## 8. 机会扫描

### 8.1 创建扫描预设

`POST /api/v1/scans/presets`

### 8.2 获取扫描预设列表

`GET /api/v1/scans/presets`

### 8.3 启动扫描

`POST /api/v1/scans/runs`

请求体示例：

```json
{
  "preset_id": 2,
  "portfolio_id": 1,
  "portfolio_rule_id": 3,
  "scope_snapshot": {
    "asset_types": ["stock", "etf"],
    "markets": ["sh", "sz"],
    "boards": ["main", "gem"]
  },
  "filters_snapshot": {
    "min_amount": 100000000,
    "exclude_st": true,
    "exclude_new_listing_days_lt": 120
  }
}
```

### 8.4 查询扫描执行状态

`GET /api/v1/scans/runs/{scan_run_id}`

### 8.5 获取扫描结果

`GET /api/v1/scans/runs/{scan_run_id}/results`

查询参数：

- `result_type`
- `page`
- `page_size`

### 8.6 获取最近一次可执行候选池

`GET /api/v1/scans/latest/executable`

查询参数：

- `portfolio_id`
- `limit`

## 9. 组合与仓位规则

### 9.1 获取组合列表

`GET /api/v1/portfolios`

### 9.2 创建组合

`POST /api/v1/portfolios`

请求体：

```json
{
  "name": "main",
  "account_type": "simulated",
  "total_capital": 500000,
  "investable_ratio": 0.9,
  "cash_reserve_ratio": 0.1,
  "currency": "CNY",
  "is_default": true
}
```

### 9.3 获取组合详情

`GET /api/v1/portfolios/{portfolio_id}`

返回：

- 基础信息
- 当前规则
- 仓位摘要

### 9.4 创建或更新组合规则

`POST /api/v1/portfolios/{portfolio_id}/rules`

请求体：

```json
{
  "rule_name": "default",
  "max_single_position_pct": 0.15,
  "max_sector_position_pct": 0.30,
  "max_stock_position_pct": 0.70,
  "max_etf_position_pct": 0.50,
  "max_loss_per_trade_pct": 0.01,
  "max_open_positions": 8,
  "stage_limits_json": {
    "stock": {
      "start": 0.08,
      "accel": 0.15,
      "overheat": 0.05,
      "cooldown": 0.0
    },
    "etf": {
      "start": 0.10,
      "accel": 0.20,
      "overheat": 0.08,
      "cooldown": 0.0
    }
  },
  "is_active": true
}
```

### 9.5 获取当前仓位摘要

`GET /api/v1/portfolios/{portfolio_id}/allocation`

返回：

- 总仓位
- 股票仓位
- ETF 仓位
- 现金占比
- 持仓数
- 赛道暴露

## 10. 持仓

### 10.1 获取持仓列表

`GET /api/v1/portfolios/{portfolio_id}/positions`

### 10.2 新增或更新持仓

`POST /api/v1/portfolios/{portfolio_id}/positions`

### 10.3 删除持仓

`DELETE /api/v1/portfolios/{portfolio_id}/positions/{symbol_id}`

## 11. 交易计划

### 11.1 生成单标交易计划

`POST /api/v1/trade-setups/generate`

请求体：

```json
{
  "portfolio_id": 1,
  "symbol_id": 12,
  "score_id": 156,
  "scan_run_id": 33
}
```

返回：

- 买入区间
- 止损位
- 目标位
- 建议仓位比例
- 建议仓位金额
- 风险收益比
- 是否允许加仓

### 11.2 获取单标最新交易计划

`GET /api/v1/trade-setups/latest/{symbol_id}`

查询参数：

- `portfolio_id`

### 11.3 获取组合下的交易计划列表

`GET /api/v1/portfolios/{portfolio_id}/trade-setups`

查询参数：

- `stage`
- `action`
- `limit`

### 11.4 获取交易信号列表

`GET /api/v1/portfolios/{portfolio_id}/trade-signals`

### 11.5 标记交易信号状态

`PATCH /api/v1/trade-signals/{signal_id}`

请求体：

```json
{
  "status": "done"
}
```

## 12. 复盘日志

### 12.1 获取复盘列表

`GET /api/v1/journals`

查询参数：

- `portfolio_id`
- `symbol_id`
- `entry_type`
- `page`
- `page_size`

### 12.2 新建复盘记录

`POST /api/v1/journals`

请求体示例：

```json
{
  "portfolio_id": 1,
  "symbol_id": 12,
  "trade_setup_id": 88,
  "entry_type": "trade",
  "title": "首次试仓",
  "content": "趋势启动，量能配合。",
  "subjective_view": "可以先小仓位试错",
  "follow_system": true,
  "outcome": "",
  "review_note": ""
}
```

### 12.3 更新复盘记录

`PATCH /api/v1/journals/{journal_id}`

## 13. 仪表盘

### 13.1 获取总览页数据

`GET /api/v1/dashboard/overview`

查询参数：

- `portfolio_id`

返回聚合内容：

- 自选池摘要
- 组合仓位摘要
- 高优先级候选
- 风险提示

### 13.2 获取组合驾驶舱数据

`GET /api/v1/dashboard/portfolio`

查询参数：

- `portfolio_id`

返回聚合内容：

- 当前仓位
- 赛道分布
- 当前可新增仓位
- 可执行候选池

## 14. 推荐实现顺序

建议按以下顺序开发接口：

1. `symbols`
2. `watchlists`
3. `market-data`
4. `scores`
5. `portfolios`
6. `positions`
7. `scans`
8. `trade-setups`
9. `journals`
10. `dashboard`

## 15. MVP 最小闭环

如果要尽快跑通第一条完整链路，最少需要以下接口：

- `POST /api/v1/market-data/update`
- `POST /api/v1/scores/calculate`
- `POST /api/v1/scans/runs`
- `GET /api/v1/scans/runs/{scan_run_id}/results`
- `POST /api/v1/portfolios/{portfolio_id}/rules`
- `POST /api/v1/trade-setups/generate`
- `GET /api/v1/dashboard/overview`

## 16. 下一步建议

现在最适合继续的是：

1. 生成 `FastAPI` 项目骨架
2. 先落 `SQLite + SQLAlchemy` 模型
3. 先实现最小闭环接口

如果继续往开发走，建议下一步直接开始建后端目录结构和 ORM 模型。
