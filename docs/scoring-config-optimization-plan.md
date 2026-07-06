# 轻量自定义评分配置优化方案

## 背景

当前机会挖掘的核心链路是：

```text
同步/读取行情 -> 计算 scores -> run_scan -> 生成 scan_results -> 前端机会列表展示
```

现有评分逻辑主要写在 `app/services/analysis.py` 的 `calculate_symbol_score()` 中，权重和维度基本是硬编码。用户目前的真实诉求不是搭建复杂的多模型研究平台，而是：

1. 在全市场里“大海捞针”，按自己关心的维度和权重打分。
2. 股票和 ETF 是两类资产，评分维度和权重应该分开配置。
3. 配置放在设置里，用户改完后，新的评分和新的机会挖掘按当前激活预设执行。
4. 已经生成的历史评分和历史挖掘结果不被新配置覆盖，便于复盘。
5. 初始化全部标的时，按当前激活预设重新评价所有股票/ETF。

因此，本方案采用“轻量自定义评分配置 + 配置版本快照”的方式，不先引入复杂的多模型体系。

## 目标

### 必须支持

1. 设置页维护两套评分配置：
   - 股票评分配置：`stock`
   - ETF 评分配置：`etf`
2. 用户可以配置：
   - 维度是否启用
   - 维度权重
   - 维度内因子权重
   - 维度筛选阈值
3. 每类资产支持多个评分预设。
4. 用户通过“启用/激活”选择当前生效预设。
5. 系统内置常用预设，系统预设不可删除。
6. 每次保存用户预设生成新版本。
7. 新评分使用当前激活预设。
8. 历史评分保留原始分数、预设版本、配置快照。
9. 机会挖掘按资产类型自动使用当前股票/ETF 激活预设。
10. 初始化全部标的时，按当前激活预设批量重算。

### 暂不做

1. 暂不做复杂的多模型 A/B 对比。
2. 暂不做每个用户多套模型并行管理。
3. 暂不强依赖 PE、PB、主力资金、北向资金等外部数据；这些作为预留维度，等数据源稳定后接入。
4. 暂不改造所有历史评分为新结构，仅对新评分写入配置快照。
5. 暂不支持同一资产类型同时启用多个预设；第一版保持 `stock` 一个激活预设、`etf` 一个激活预设。

## 核心设计原则

### 1. 当前激活预设只影响未来

用户修改设置或切换激活预设后：

```text
旧 scores 不改
旧 scan_results 不改
新 scores 使用当前激活预设
新 scan_results 使用新评分
初始化任务使用当前激活预设重算新一批 scores
```

### 2. 多个预设，一个激活

每个资产类型可以有多个评分预设，但同一时间只有一个生效：

```text
stock: 多个股票预设，最多一个 is_active = 1
etf:   多个 ETF 预设，最多一个 is_active = 1
```

系统内置预设可以被激活、查看和复制，但不允许删除，也不建议直接覆盖。用户要微调系统预设时，走“复制为我的预设”或“另存为用户预设”。

### 3. 股票和 ETF 分开配置

股票和 ETF 不共用一套权重。

股票配置适合：

```text
趋势、动量、波动、流动性、交易活跃、估值、资金流、事件/题材
```

ETF 配置适合：

```text
趋势、动量、波动、流动性、成交活跃、回撤、跟踪强度、溢价折价
```

PE、市盈率、基本面估值等维度默认只属于股票，不用于 ETF。

### 4. 轻量版本化

不用一开始做复杂的 `score_models / score_model_versions` 多模型体系，但必须有配置版本。

版本的作用是复盘和保护历史：

```text
score A 是用 stock/steady_value v3 算的
score B 是用 stock/trend_growth v2 算的
旧分数不会因为新预设激活或新版本发布而变化
```

## 数据模型方案

### 新增表：`scoring_configs`

保存股票/ETF 的系统预设、用户预设、当前激活状态和历史版本。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int | 主键 |
| `asset_type` | string | `stock` / `etf` |
| `preset_key` | string | 稳定预设 Key，如 `balanced_opportunity` |
| `name` | string | 预设名称，如“均衡机会” |
| `description` | text/null | 预设说明 |
| `version` | int | 同一 `asset_type + preset_key` 下递增 |
| `config_json` | text/json | 完整评分配置 |
| `preset_source` | string | `system` / `user` |
| `is_system` | int/bool | 是否系统预设；系统预设不可删除 |
| `is_active` | int/bool | 是否当前生效 |
| `is_latest` | int/bool | 是否该预设最新版本 |
| `base_preset_key` | string/null | 用户预设从哪个系统预设复制而来 |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |

约束建议：

```text
unique(asset_type, preset_key, version)
同一 asset_type 只能有一个 is_active = 1
同一 asset_type + preset_key 只能有一个 is_latest = 1
系统预设 is_system = 1，不允许 delete
```

删除规则：

```text
系统预设：不可删除，只能激活或复制
用户预设：可删除，但如果正在激活，需要先切换到其他预设
历史版本：默认不在列表主视图删除，用于评分追溯
```

### 扩展表：`scores`

新增字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `scoring_asset_type` | string | `stock` / `etf` |
| `scoring_config_id` | int/null | 使用的评分配置 ID |
| `scoring_preset_key` | string/null | 使用的预设 Key |
| `scoring_preset_name` | string/null | 使用的预设名称 |
| `scoring_config_version` | int/null | 使用的预设版本 |
| `scoring_config_snapshot_json` | text/json | 当时配置快照 |
| `dimension_scores_json` | text/json | 每个维度的得分 |
| `factor_scores_json` | text/json | 每个因子的原始值、标准化分、贡献 |

说明：

1. `scores` 仍保留现有字段：`quality_score`、`timing_score`、`priority_score`、`trend_score` 等。
2. 新 JSON 字段用于解释和追溯，不要求第一版所有页面都展示。
3. 旧数据这些字段可以为空。

## 系统内置预设

系统启动或数据库初始化时，应内置一批经典常用预设。系统预设属于基础模板：可启用、可查看、可复制，不允许删除。

### 股票系统预设

| 预设 Key | 名称 | 适用场景 | 特点 |
|---|---|---|---|
| `balanced_opportunity` | 均衡机会 | 默认股票机会挖掘 | 趋势、时点、流动性较均衡，适合作为初始激活预设 |
| `steady_value` | 稳健股质 | 中低频、偏稳健选股 | 更重视股质、波动控制、估值预留，降低追高倾向 |
| `trend_growth` | 趋势成长 | 趋势跟随、强者恒强 | 更重视趋势、动量、突破，适合找强势票 |
| `active_capital` | 活跃资金 | 短线活跃、资金异动 | 更重视成交活跃、换手、资金流预留，适合“大海捞针” |

建议默认激活：`balanced_opportunity`。

### ETF 系统预设

| 预设 Key | 名称 | 适用场景 | 特点 |
|---|---|---|---|
| `etf_balanced` | ETF 均衡机会 | 默认 ETF 机会挖掘 | 趋势、动量、流动性、波动较均衡 |
| `etf_trend_rotation` | ETF 趋势轮动 | 行业/主题 ETF 轮动 | 更重视趋势、动量和突破 |
| `etf_low_volatility` | ETF 低波稳健 | 宽基、债券、低波 ETF | 更重视波动、回撤和流动性 |
| `etf_liquidity_first` | ETF 高流动 | 大资金进出友好 | 更重视成交额、流动性和价差风险预留 |

建议默认激活：`etf_balanced`。

### 系统预设规则

1. 系统预设 `is_system = 1`，不允许删除。
2. 系统预设不建议直接覆盖；用户编辑系统预设时，前端默认走“复制为我的预设”。
3. 系统预设可以随着版本升级补充新版本，但已经用于历史评分的快照不变。
4. 用户预设 `is_system = 0`，可以重命名、修改、删除。
5. 每个资产类型最多一个激活预设。
## 配置 JSON 示例

### 股票配置

```json
{
  "preset_key": "balanced_opportunity",
  "name": "均衡机会",
  "asset_type": "stock",
  "preset_source": "system",
  "final_weights": {
    "quality": 0.4,
    "timing": 0.5,
    "news": 0.1
  },
  "dimensions": [
    {
      "key": "trend",
      "name": "趋势",
      "enabled": true,
      "score_bucket": "quality",
      "weight": 0.22,
      "filter": { "enabled": false, "operator": "gte", "value": 60 },
      "factors": [
        { "key": "trend_score", "source": "builtin", "weight": 1.0 }
      ]
    },
    {
      "key": "momentum",
      "name": "动量",
      "enabled": true,
      "score_bucket": "timing",
      "weight": 0.18,
      "filter": { "enabled": false, "operator": "gte", "value": 60 },
      "factors": [
        { "key": "momentum_score", "source": "builtin", "weight": 1.0 }
      ]
    },
    {
      "key": "activity",
      "name": "交易活跃",
      "enabled": true,
      "score_bucket": "timing",
      "weight": 0.2,
      "filter": { "enabled": true, "operator": "gte", "value": 65 },
      "factors": [
        { "key": "amount_activity", "source": "daily_bar", "weight": 0.55, "direction": "higher_better" },
        { "key": "turnover_activity", "source": "daily_bar", "weight": 0.45, "direction": "range_better" }
      ]
    },
    {
      "key": "valuation",
      "name": "估值",
      "enabled": false,
      "score_bucket": "quality",
      "weight": 0.15,
      "filter": { "enabled": false, "operator": "gte", "value": 55 },
      "factors": [
        { "key": "pe_score", "source": "fundamental", "weight": 1.0, "direction": "lower_or_range_better" }
      ]
    },
    {
      "key": "capital_flow",
      "name": "资金流",
      "enabled": false,
      "score_bucket": "timing",
      "weight": 0.2,
      "filter": { "enabled": false, "operator": "gte", "value": 60 },
      "factors": [
        { "key": "main_net_inflow_score", "source": "capital_flow", "weight": 1.0, "direction": "higher_better" }
      ]
    }
  ]
}
```

### ETF 配置

```json
{
  "preset_key": "etf_balanced",
  "name": "ETF均衡机会",
  "asset_type": "etf",
  "preset_source": "system",
  "final_weights": {
    "quality": 0.35,
    "timing": 0.55,
    "news": 0.1
  },
  "dimensions": [
    {
      "key": "trend",
      "name": "趋势",
      "enabled": true,
      "score_bucket": "quality",
      "weight": 0.28,
      "filter": { "enabled": false, "operator": "gte", "value": 60 },
      "factors": [
        { "key": "trend_score", "source": "builtin", "weight": 1.0 }
      ]
    },
    {
      "key": "momentum",
      "name": "动量",
      "enabled": true,
      "score_bucket": "timing",
      "weight": 0.22,
      "filter": { "enabled": false, "operator": "gte", "value": 60 },
      "factors": [
        { "key": "momentum_score", "source": "builtin", "weight": 1.0 }
      ]
    },
    {
      "key": "liquidity",
      "name": "流动性",
      "enabled": true,
      "score_bucket": "quality",
      "weight": 0.2,
      "filter": { "enabled": true, "operator": "gte", "value": 60 },
      "factors": [
        { "key": "liquidity_score", "source": "builtin", "weight": 0.7 },
        { "key": "amount_activity", "source": "daily_bar", "weight": 0.3, "direction": "higher_better" }
      ]
    },
    {
      "key": "drawdown_risk",
      "name": "回撤风险",
      "enabled": true,
      "score_bucket": "quality",
      "weight": 0.15,
      "filter": { "enabled": false, "operator": "gte", "value": 55 },
      "factors": [
        { "key": "volatility_score", "source": "builtin", "weight": 1.0 }
      ]
    },
    {
      "key": "premium_discount",
      "name": "溢价折价",
      "enabled": false,
      "score_bucket": "quality",
      "weight": 0.1,
      "filter": { "enabled": false, "operator": "gte", "value": 55 },
      "factors": [
        { "key": "premium_discount_score", "source": "etf_basic", "weight": 1.0, "direction": "range_better" }
      ]
    }
  ]
}
```

## 后端 API 方案

### 设置接口

新增路由：`app/api/routes/scoring_configs.py`

```text
GET    /settings/scoring-configs?asset_type=stock
GET    /settings/scoring-configs/active?asset_type=stock
POST   /settings/scoring-configs
PUT    /settings/scoring-configs/{id}
POST   /settings/scoring-configs/{id}/activate
POST   /settings/scoring-configs/{id}/duplicate
GET    /settings/scoring-configs/{id}/versions
DELETE /settings/scoring-configs/{id}
```

接口职责：

| 接口 | 作用 |
|---|---|
| `GET /settings/scoring-configs` | 按资产类型列出最新预设，包含系统预设和用户预设 |
| `GET /settings/scoring-configs/active` | 获取某资产类型当前激活预设 |
| `POST /settings/scoring-configs` | 新建用户预设 |
| `PUT /settings/scoring-configs/{id}` | 保存用户预设新版本；系统预设不允许直接覆盖 |
| `POST /settings/scoring-configs/{id}/activate` | 激活该预设，并自动停用同资产类型其他预设 |
| `POST /settings/scoring-configs/{id}/duplicate` | 复制系统预设或用户预设为新的用户预设 |
| `GET /settings/scoring-configs/{id}/versions` | 查看该预设历史版本 |
| `DELETE /settings/scoring-configs/{id}` | 删除用户预设；系统预设返回 400/403 |

`PUT` 保存用户预设时自动：

1. 校验该预设不是系统预设，或本次操作明确为“复制后保存”。
2. 读取当前 `asset_type + preset_key` 最新版本。
3. 新建一条 `version + 1` 的配置。
4. 旧版本 `is_latest` 置为 0。
5. 新版本 `is_latest` 置为 1。
6. 如果该预设原本是激活预设，新版本继续保持 `is_active = 1`。

`activate` 激活时自动：

1. 校验预设是该 `asset_type` 的最新版本。
2. 将同一 `asset_type` 下其他预设 `is_active` 置为 0。
3. 将当前预设 `is_active` 置为 1。
4. 后续评分、初始化、机会挖掘都读取这个激活预设。

### 评分接口

现有接口：

```text
POST /scores/calculate
```

请求体可以保持兼容：

```json
{
  "symbol_ids": [1, 2, 3],
  "trade_date": "2026-07-04"
}
```

后端根据 `Symbol.asset_type` 自动选择当前激活预设：

```text
stock -> active stock preset
etf   -> active etf preset
```

可选扩展字段：

```json
{
  "symbol_ids": [1, 2, 3],
  "trade_date": "2026-07-04",
  "force_preset_key": null,
  "force_config_version": null
}
```

第一版不建议让普通页面传 `force_preset_key` 或 `force_config_version`，避免用户混乱。

### 机会挖掘接口

现有接口：

```text
POST /discovery/tasks
```

请求体可以先不增加 `score_model_id`。后端自动按 scope 选择：

```text
cn-stock / us-stock -> 股票当前激活预设
cn-etf / us-etf     -> ETF 当前激活预设
```

可以新增一个轻量字段，用于运行时覆盖筛选阈值：

```json
{
  "scope": "cn-stock",
  "min_score": 60,
  "dimension_filters": [
    { "field": "activity_score", "operator": "gte", "value": 70 },
    { "field": "capital_flow_score", "operator": "gte", "value": 65 }
  ]
}
```

如果设置页里已经配置了维度阈值，第一版可以直接使用配置里的 `filter`，不必让挖掘页重复配置。

## 评分计算服务方案

新增服务：`app/services/scoring_config_engine.py`

建议核心函数：

```python
def get_active_scoring_config(db, asset_type: str) -> ScoringConfig:
    ...

def calculate_symbol_score_with_config(db, symbol, trade_date, config=None) -> Score:
    ...

def evaluate_dimension_scores(context, config_json) -> dict:
    ...

def passes_dimension_filters(dimension_scores: dict, config_json) -> tuple[bool, list[str]]:
    ...
```

### 兼容现有 `calculate_symbol_score()`

第一版不要大拆。

推荐做法：

1. 保留 `calculate_symbol_score()` 作为外部入口。
2. 入口内部读取当前激活预设。
3. 先复用现有内置分项：
   - `trend_score`
   - `momentum_score`
   - `volatility_score`
   - `liquidity_score`
   - `breadth_score`
   - `event_score`
   - `breakout_score`
   - `pullback_score`
   - `overheat_penalty`
4. 新增交易活跃维度：
   - `amount_activity`
   - `turnover_activity`
5. 根据配置重新聚合：
   - `quality_score`
   - `timing_score`
   - `priority_score`
6. 把配置版本和 JSON 快照写入 `scores`。

### 缺失数据处理

每个因子支持缺失策略：

```text
neutral: 给 50 分
ignore: 从维度权重中剔除
penalty: 给低分，如 30 分
```

第一版默认：

```text
已有技术分项缺失 -> neutral
PE / 资金流未接入 -> enabled=false，不参与评分
```

## 初始化流程

历史初始化或全市场初始化时，按当前激活预设执行。

```text
选择 scope
  -> 拉取/补齐标的列表
  -> 同步 K 线
  -> 根据 symbol.asset_type 读取当前激活评分预设
  -> 批量计算 scores
  -> 写入配置版本和快照
  -> auto_scan 时基于新 scores 扫描
  -> 清理不符合保留规则的僵尸标的
```

这意味着：

1. 用户修改或切换股票预设后，再跑股票初始化，会生成一批新版本评分。
2. 用户修改或切换 ETF 预设后，再跑 ETF 初始化，只影响 ETF 新评分。
3. 旧评分仍在库中，不被覆盖。

## 机会挖掘流程

```text
用户选择 cn-stock
  -> 读取股票当前激活预设
  -> 同步/读取候选标的行情
  -> 计算当前版本 scores
  -> 按 priority_score 排序
  -> 按配置中的维度阈值过滤
  -> 生成 scan_results
  -> 前端展示机会列表和评分解释
```

ETF 同理，只是读取 ETF 当前激活预设。

## 前端页面方案

入口：`设置 -> 评分配置`

### 页面结构

```text
顶部：资产类型 Tabs
  - 股票
  - ETF

预设栏：
  - 当前资产类型下的系统预设 + 用户预设
  - 显示预设名称、来源、版本、是否启用
  - 操作：启用、复制、编辑、删除
  - 系统预设显示“系统”标记，删除按钮禁用

左侧：维度列表
  - 启用开关
  - 维度名称
  - 权重
  - 阈值摘要

右侧：维度详情
  - 维度名称
  - 所属分组：股质 / 时点
  - 权重
  - 因子列表
  - 因子权重
  - 缺失数据处理
  - 是否作为挖掘筛选条件
  - 阈值 operator/value

底部：
  - 权重合计提示
  - 保存为新版本
  - 复制为我的预设
  - 恢复系统默认
```

### 交互约束

1. 启用维度权重合计建议为 100%，保存时可自动归一化。
2. 禁用维度不参与评分，也不参与阈值筛选。
3. 股票配置中可显示 PE、资金流预留项，但默认禁用并标记“待数据源接入”。
4. ETF 配置不显示 PE，避免误用。
5. 系统预设不可删除，编辑系统预设时提示先复制为用户预设。
6. 每个资产类型只能有一个启用预设；启用新预设会自动停用旧预设。
7. 用户预设如果已被历史评分引用，删除只影响预设列表，不影响历史评分快照。
8. 保存按钮文案明确提示：

```text
保存后只影响新评分和新挖掘结果，历史评分不会重算。
```

## 参数说明与 Tip 文档方案

评分配置必须让普通操作者看得懂。第一版不应该把设置页做成纯参数表，而是采用“预设优先 + 参数 Tip + 高级说明文档”的方式：

```text
普通用户：选系统预设、调少量阈值、看中文解释
熟练用户：复制预设、调整维度权重和筛选阈值
高级用户：调整因子权重、缺失数据策略、后续新增自定义因子
```

### 设置页提示层级

| 层级 | 展示位置 | 作用 |
|---|---|---|
| 简短 Tip | 字段名旁的信息图标 | 用一句话解释这个参数影响什么 |
| 影响提示 | 修改权重/阈值时实时展示 | 告诉用户调高或调低后会更偏向哪类标的 |
| 预设说明 | 预设卡片/详情页顶部 | 说明这个预设适合什么行情和操作风格 |
| 参数文档 | 设置页右上角“参数说明” | 集中解释所有维度、因子、阈值、缺失策略 |
| 结果解释 | 机会详情页 | 告诉用户本次入选主要靠什么加分，因为什么被过滤 |

### 核心参数释义

| 参数 | 用户能看到的名称 | 含义 | 调大后的效果 | 建议默认 |
|---|---|---|---|---|
| `asset_type` | 资产类型 | 当前配置用于股票还是 ETF | 不可调，只用于隔离配置 | 股票/ETF 分开 |
| `preset` | 评分预设 | 一套完整的评分规则模板 | 切换后新评分按新规则执行 | 系统均衡预设 |
| `is_active` | 当前启用 | 是否作为当前生效规则 | 启用后只影响新评分和新挖掘 | 每类资产一个 |
| `version` | 版本 | 每次保存产生的新版本号 | 用于复盘当时按什么规则评分 | 自动生成 |
| `dimension.enabled` | 是否启用维度 | 该维度是否参与评分和筛选 | 开启后会影响最终机会分 | 常用维度开启 |
| `dimension.weight` | 维度权重 | 该维度在所属分组里的重要程度 | 越大越影响最终分数 | 系统预设给定 |
| `factor.weight` | 因子权重 | 维度内部不同指标的重要程度 | 越大越影响该维度分 | 系统预设给定 |
| `filter.enabled` | 作为筛选条件 | 是否把该维度当成硬性门槛 | 开启后不达标会被过滤 | 只对关键维度开启 |
| `filter.value` | 筛选阈值 | 最低需要达到多少分才保留 | 越高越严格，机会更少 | 60-70 |
| `score_bucket` | 所属评分 | 维度计入股质分还是时点分 | 不建议普通用户调整 | 系统预设给定 |
| `missing_policy` | 缺失数据处理 | 指标没有数据时怎么处理 | 决定数据缺失是否惩罚 | 默认中性 |
| `direction` | 指标方向 | 指标越高越好、越低越好还是区间更好 | 用于标准化得分 | 系统内置 |

### 维度说明

#### 股票维度

| 维度 | 通俗解释 | 适合解决的问题 | Tip 文案 |
|---|---|---|---|
| 趋势 | 股价是否处在更强的上行结构中 | 找强势票、避开弱趋势 | 趋势权重越高，越偏向已经走强的股票。 |
| 动量 | 最近涨跌速度和强度 | 找短中期表现突出的标的 | 动量权重越高，越容易选到近期活跃的股票。 |
| 波动/风险 | 价格波动是否过大 | 控制追高和剧烈回撤 | 风险维度越严格，结果会更稳，但可能错过强势波动票。 |
| 流动性 | 成交是否足够顺畅 | 避免买卖困难 | 流动性阈值越高，越偏向成交额大的标的。 |
| 交易活跃 | 成交额、换手是否明显放大 | 做“大海捞针”的异动发现 | 活跃度越高，越容易捕捉市场正在交易的标的。 |
| 估值 | PE/PB 等是否合理 | 避免明显高估或寻找低估 | 估值维度适合中低频使用，短线可降低权重。 |
| 资金流 | 是否有主力或大资金净流入 | 识别资金推动迹象 | 资金流需要稳定数据源，未接入前默认关闭。 |
| 事件/题材 | 是否有消息、公告、题材催化 | 捕捉事件驱动机会 | 事件维度权重越高，越偏向近期有催化的标的。 |

#### ETF 维度

| 维度 | 通俗解释 | 适合解决的问题 | Tip 文案 |
|---|---|---|---|
| 趋势 | ETF 对应板块/指数是否走强 | 做行业、主题、宽基轮动 | 趋势权重越高，越偏向正在走强的方向。 |
| 动量 | 最近表现是否强于其他 ETF | 找轮动中的强势品种 | 动量权重越高，切换会更敏感。 |
| 流动性 | 成交额是否足够、买卖是否顺畅 | 避免冷门 ETF | 流动性阈值越高，越适合较大资金进出。 |
| 回撤风险 | 近期波动和回撤是否可控 | 控制持有体验 | 回撤要求越严格，结果更稳，但弹性可能下降。 |
| 溢价折价 | 场内价格是否偏离净值 | 避免买贵或异常折价风险 | 溢价折价异常时，即使趋势好也要谨慎。 |
| 跟踪强度 | 是否紧跟目标指数 | 选择更可靠的 ETF | 跟踪越稳定，越适合作为配置工具。 |
| 规模/份额 | 基金规模和份额是否健康 | 避免规模太小或流动性差 | 规模太小的 ETF 容易有流动性和清盘风险。 |

### 常见参数的用户提示文案

设置页可以直接使用以下 Tip：

```text
维度权重：
这个维度在最终评分里的重要程度。权重越高，该维度对最终结果影响越大。

筛选阈值：
这是硬性门槛。开启后，低于该分数的标的会被过滤，不会进入机会列表。

因子权重：
同一个维度里，不同指标的重要程度。普通用户一般不用调整。

缺失数据处理：
当某个指标没有数据时的处理方式。中性表示给一个普通分，不因为缺数据明显加分或扣分。

系统预设：
系统内置的常用评分模板，可以启用或复制，不能删除。

用户预设：
你自己保存的评分模板，可以修改、启用、删除。

保存为新版本：
保存后只影响以后新计算的评分，历史评分和历史机会列表不会改变。

初始化重算：
用当前启用预设重新评价全部标的，适合你调整规则后想重新筛一遍市场。
```

### 修改参数时的实时影响提示

前端可以在用户修改关键参数时显示动态提示：

| 用户操作 | 实时提示 |
|---|---|
| 调高趋势权重 | 结果会更偏向已经走强的标的，可能减少低位潜伏型机会。 |
| 调高交易活跃权重 | 结果会更偏向近期成交放大的标的，适合大海捞针发现异动。 |
| 调高估值权重 | 结果会更偏向估值合理或偏低的股票，可能减少高成长高估值股票。 |
| 开启资金流筛选 | 没有达到资金流阈值的标的会被过滤，请确认资金流数据已接入。 |
| 调高流动性阈值 | 冷门、小成交标的会减少，更适合关注可交易性。 |
| 调高 ETF 回撤要求 | 波动大的 ETF 会减少，机会数量可能下降。 |
| 切换启用预设 | 只影响后续评分和新机会挖掘，不会改动历史结果。 |

### 参数文档入口

建议在设置页右上角增加：

```text
参数说明
```

点击后打开抽屉或弹窗，包含：

1. 当前资产类型的维度说明。
2. 当前预设的适用场景。
3. 权重、阈值、启用、版本的解释。
4. “怎么调”的建议：
   - 想多发现异动：提高交易活跃、动量、资金流权重。
   - 想稳一点：提高流动性、波动/回撤、估值权重。
   - 想做趋势：提高趋势、动量、突破相关权重。
   - 想减少结果数量：提高筛选阈值。
   - 想扩大候选池：降低筛选阈值，少开硬过滤。

### 新手保护

为了避免用户不理解参数导致误配，第一版建议加这些保护：

1. 默认只展示“预设选择、启用状态、关键阈值、维度权重”。
2. 因子权重、缺失策略、指标方向放到“高级设置”。
3. 保存时提示影响范围：

```text
本次保存会生成新版本，只影响后续评分和机会挖掘；历史评分不会变化。
```

4. 权重合计异常时给出清晰提示：

```text
当前启用维度权重合计不是 100%，系统将自动归一化后计算。
```

5. 对未接入数据源的指标显示禁用态：

```text
待数据源接入，当前不会参与评分。
```

6. 对高风险配置显示提醒：

```text
你开启了多个硬筛选条件，机会数量可能明显减少。
```

## 评分解释

新评分结果建议增加解释结构：

```json
{
  "scoring_preset": {
    "asset_type": "stock",
    "version": 4,
    "preset_key": "balanced_opportunity",
    "name": "均衡机会"
  },
  "dimension_scores": {
    "trend": 72.3,
    "momentum": 64.8,
    "activity": 81.2,
    "valuation": null,
    "capital_flow": null
  },
  "factor_scores": {
    "amount_activity": {
      "raw_value": 180000000,
      "normalized_value": 82.5,
      "weight": 0.55,
      "contribution": 45.4
    }
  }
}
```

前端机会详情里展示：

```text
使用预设：均衡机会 v4
最终机会分：72.6
主要加分：交易活跃、趋势
主要拖累：波动、估值未启用
```

## 第一版实施步骤

### P0：配置落库和系统预设

1. 新增 `scoring_configs` 模型、schema、路由。
2. 初始化股票系统预设：均衡机会、稳健股质、趋势成长、活跃资金。
3. 初始化 ETF 系统预设：ETF 均衡机会、ETF 趋势轮动、ETF 低波稳健、ETF 高流动。
4. 默认激活 `balanced_opportunity` 和 `etf_balanced`。
5. 系统预设标记 `is_system = 1`，删除接口禁止删除。
6. 设置页新增“评分配置”入口。
7. 保存用户预设时生成新版本。
8. 支持复制系统预设为用户预设。

### P0：评分计算接入配置

1. 扩展 `scores` 字段，保存配置版本和快照。
2. `calculate_symbol_score()` 改为读取当前激活预设。
3. 保留现有技术维度作为内置因子。
4. 新增 `amount_activity` 和 `turnover_activity` 两个交易活跃因子。
5. 计算 `dimension_scores_json` 和 `factor_scores_json`。

### P0：机会挖掘接入配置筛选

1. `discovery_tasks` 计算评分时使用当前激活预设。
2. `run_scan()` 或扫描后处理阶段应用维度阈值。
3. `scan_results.filters_snapshot` 写入配置版本和筛选条件。
4. 前端机会列表显示当前评分预设和版本。

### P1：初始化任务联动

1. 历史初始化/全市场同步评分时读取当前激活预设。
2. auto_scan 后生成当前激活预设下的候选。
3. 数据健康页面提示当前股票/ETF 激活预设和版本。

### P1：评分解释 UI

1. 详情页展示维度贡献。
2. 机会挖掘结果支持按维度分排序。
3. 支持“为什么入选/为什么没入选”的基础解释。

### P2：外部数据因子

1. 股票估值：PE、PB、市值、行业分位。
2. 资金流：主力净流入、超大单净流入、北向资金。
3. ETF 特有：溢价折价、跟踪误差、基金规模、份额变化。

## 回归验证

### 后端验证

1. 修改股票预设后，旧 `scores` 的分数不变化。
2. 新计算的股票评分写入新预设版本和预设快照。
3. ETF 评分不使用股票预设。
4. 股票评分不使用 ETF 预设。
5. 初始化任务能按当前激活预设批量生成新评分。
6. 维度阈值能过滤 scan_results。
7. 未启用维度不参与评分和过滤。
8. 同一资产类型激活新预设后，旧激活预设自动停用。
9. 系统预设删除接口返回错误，用户预设可删除。
10. 系统预设复制后生成用户预设，原系统预设不变。

### 前端验证

1. 设置页股票/ETF Tab 预设互不影响。
2. 系统预设带“系统”标记，删除按钮禁用。
3. 用户预设可以新增、复制、编辑、删除。
4. 保存用户预设后版本号递增。
5. 启用一个预设后，同资产类型其他预设显示为未启用。
6. 权重合计异常时有提示。
7. 机会挖掘结果能看到评分预设和版本。
8. 详情页能展示维度贡献。

## 风险和处理

| 风险 | 处理 |
|---|---|
| 权重配置错误导致评分失真 | 保存时提示权重合计，支持恢复系统预设 |
| 历史评分和新评分混在一起难比较 | 每条 score 保存预设 Key、版本和快照 |
| 系统预设被误删 | `is_system = 1` 禁止删除，只允许复制 |
| 同一资产类型多个预设同时启用 | 数据库约束 + 激活接口事务保证单激活 |
| 用户误改系统预设 | 系统预设不可直接覆盖，编辑时走复制 |
| PE/资金流数据缺失 | 第一版默认禁用，接入数据源后再开启 |
| ETF 误用股票因子 | 股票/ETF 配置分开，ETF 不展示 PE |
| 初始化后评分数量变多 | 查询最新评分时继续按 trade_date + id/version 取最新 |

## 最终结论

第一版应该做“轻量自定义评分配置”，不是完整评分模型平台。

落地形态：

```text
设置页维护股票/ETF 两类评分预设
每类资产支持多个系统预设和用户预设
系统预设经典常用、可启用可复制、不可删除
每类资产同一时间只有一个激活预设
保存用户预设生成新版本
新评分按当前激活预设计算
历史评分保留原预设快照
初始化全部标的按当前激活预设重新评价
机会挖掘按当前激活预设和维度阈值筛选
```

这能覆盖用户当前“大海捞针”的核心目标，也能通过系统预设降低配置门槛，同时为后续 PE、资金流、ETF 特有指标接入留出空间。

















