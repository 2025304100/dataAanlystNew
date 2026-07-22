# 机会中心 / 标的研究 / 策略组合交易改造执行计划 Spec

> 底稿来源：`docs/opportunity-center-symbol-research-strategy-portfolio-final-plan.md`（2026-07-19 快照）
>
> 本 spec 不是重新设计，而是基于上述交接底稿拆解的工程执行计划。任何改动必须先核对底稿"当前项目功能基线"，禁止重复建设或重写已稳定底座。

## Why

当前项目功能不少，但产品对象和入口名称未对齐业务真相，存在底稿"0.9 七个真实缺口"：
1. 导航与命名未对齐业务对象（投资中心职责过载、观察池实际是组合工作台）。
2. 观察池信息过少且与投资中心本地收藏重复。
3. 缺少持久化 `portfolio_members`，成员/持仓/候选被混用，导致自动交易与组合回测无法稳定复现历史。
4. 候选→观察→成员→持仓之间无统一流转服务和审计。
5. 自动交易与组合回测依赖"最新扫描结果"，历史可复现性不足。
6. 订单/成交/回测缺少完整信号/规则/来源归因。
7. 大型前端组件和跨页面状态重复，增加测试遗漏风险。

同时底稿第 42 章明确：在机会中心、标的研究和组合交易正式切换前，必须先完成 WP-S（稳定性）、前置条件、WP-P（5 分钟扫描 SLA）、WP-AI（基础助手）、WP-MSG（消息管理）等横向能力，否则新页面会掩盖旧流程的卡顿与错误。

## What Changes

按底稿"工程交接与下一阶段任务包"组织为 13 个工作包（WP），遵循依赖与功能开关顺序实施：

- **WP0 基线、迁移框架与功能开关**：建立 4 个功能开关、Alembic 迁移框架、旧路由兼容映射、对账基线。
- **WP-S 接口风控、本地缓存与程序稳定性**：统一外部数据网关、L1~L4 缓存层级、单飞/限流/熔断、任务防卡死状态机、统一用户错误协议。
- **WP-P 挖掘性能与 5 分钟扫描 SLA**：评分快照模型、数据准备与用户扫描分离、两阶段过滤、相同参数结果复用、分层保留与清理、阶段预算与可观测性。
- **WP1 信息架构壳层与只读关联状态**：新增"机会中心"一级入口、组合页改名"组合交易"、投资中心暂留+迁移提示、统一只读状态徽标。
- **WP2 正式观察池与本地收藏迁移**：扩展 `WatchlistItem` 上下文字段、新增 `observations` 服务、本地收藏幂等迁移、保留 `core` 名单原样。
- **WP3 统一状态流转与审计**：新增 `opportunity_transitions` 领域服务与 `opportunity_transition_events` 审计表、幂等键、单事务回滚。
- **WP4 组合成员模型**：新增 `portfolio_members` 表、回填现有持仓为 `legacy_position` 成员、成员 CRUD API 与"组合成员"页签。
- **WP5 标的研究收口与组件拆分**：拆分 `InvestmentCenter.tsx` 为 9 个子组件、收藏/告警/风控/下单迁出、统一来源上下文参数。
- **WP6 自动交易成员化与安全切换**：买入来源切换为 `auto` 成员、`SimOrder` 归因字段、`client_order_key` 幂等、双跑切换、安全门禁。
- **WP7 组合回测成员化与历史可复现**：按 `effective_from/effective_to` 读取历史成员、回测快照、新旧引擎对比、VectorBT 仅作研究。
- **WP8 绩效归因、复盘和跨模块联动**：按成员/执行模式/规则版本/候选来源分拆收益、订单↔成员↔信号联动、绩效异常一键复盘。
- **WP9 旧入口与重复状态清理**：仅在 WP1~WP8 全部验收后执行；保留兼容路由但停止写入重复状态。
- **WP-AI 量化助手**：受控上下文包、只读工具集（capabilities/data_health/task_status/symbol_research/candidate_explanation/portfolio_summary/backtest_explanation）、草稿工具三步确认流程、多 Profile 主备降级、会话与审计。
- **WP-MSG 统一消息管理**：`notification_channels/policies/outbox/deliveries/templates` 数据模型、Outbox 异步发送、5 类渠道适配器（站内/WxPusher/钉钉/QQ-OneBot/邮件/通用 Webhook）、防打扰与隐私。

**BREAKING** 变更（分阶段、功能开关保护）：
- 一级导航移除"投资中心"，新增"机会中心"，"目前观察池"重命名"组合交易"。
- 自动交易买入来源从"最新扫描 executable"切换为"active + auto 成员"。
- 组合回测标的来源从"当前持仓 + 最新扫描"切换为"历史有效成员"。
- 投资中心本地收藏（`ic_favorites`）迁入后端观察池后停止写入。

## Impact

- **底稿约束**：所有改动必须遵守 0.1~0.10、13.1~13.6、21.2、42 章的"不重写"清单与兼容迁移原则。
- **Affected specs**：
  - `opportunity-center-refactor/spec.md`（已有高层 spec，本 spec 为其执行细化）
- **Affected code (按 WP 分组)**：
  - 基础设施：`app/core/config.py`、`app/db/init_db.py`、`alembic/versions/`、`frontend/src/App.tsx`、`frontend/src/context/AppContext.tsx`
  - 稳定性：新增 `app/services/external_data_gateway.py`、`app/services/task_state_machine.py`、`app/schemas/errors.py`；扩展 `akshare_registry.py`、`async_tasks` 服务
  - 性能：新增 `app/models/discovery_score_snapshot.py`、`app/services/discovery_snapshots.py`、`app/services/discovery_fast_scan.py`、`app/services/discovery_cleanup.py`；扩展 `app/services/discovery_tasks.py`、`app/api/routes/discovery.py`
  - 机会中心：新增 `frontend/src/components/OpportunityCenter.tsx`、`opportunity/CandidatePool.tsx`、`opportunity/ObservationPool.tsx`、`opportunity/OpportunityStatusBadges.tsx`；修改 `Discovery.tsx`、`TodayDecision.tsx`、i18n
  - 观察池：扩展 `app/models/watchlist.py`、`app/schemas/watchlist.py`、`app/api/routes/watchlists.py`；新增 `app/services/observations.py`、`frontend/src/components/opportunity/ObservationPool.tsx`
  - 流转：新增 `app/services/opportunity_transitions.py`、`app/models/opportunity_transition_event.py`
  - 组合成员：新增 `app/models/portfolio_member.py`、`app/schemas/portfolio_member.py`、`app/services/portfolio_members.py`、`frontend/src/components/PortfolioMembersPanel.tsx`；扩展 `app/api/routes/portfolios.py`
  - 标的研究：拆分 `InvestmentCenter.tsx` 为 `SymbolResearchShell`、`SymbolSearchHeader`、`SymbolRelationshipBar`、`FactorExplanationPanel`、`SymbolAlertSummary`、`RiskReferencePanel`、`TradePlanPanel`、`SymbolChartPanel`、`SingleSymbolBacktestPanel`
  - 自动交易：扩展 `app/models/sim_account.py`、`app/services/sim_accounts.py`、`app/services/auto_trade_task.py`、`frontend/src/components/AutoTradePanel.tsx`
  - 组合回测：扩展 `app/services/portfolio_backtest.py`、`app/models/backtest.py`、`frontend/src/components/PortfolioBacktestPanel.tsx`
  - 绩效：扩展 `app/services/metrics.py`、`app/services/portfolio_performance.py`、`frontend/src/components/PortfolioPerformancePanel.tsx`
  - AI：新增 `app/services/ai/`、`app/models/ai_session.py`、`frontend/src/components/ai/`
  - 消息：新增 `app/services/notifications/`、`app/models/notification.py`、`frontend/src/components/notifications/`
- **不重写清单（13.4）**：`Symbol`、`DailyBar`、`Score`、因子仓库、Quality/Timing 计算与扫描过滤算法、自定义指标公式引擎、事件驱动回测核心、VectorBT 辅助能力、模拟订单撮合、现金流水、佣金/印花税/滑点/整手/T+1/涨跌停规则、异步任务/调度器/取消、告警规则/事件、数据健康/接口管理、本地数据仓库、组合/持仓/订单/成交/回测现有主键。
- **数据库迁移原则（15.1）**：只允许新增表/列/索引；新表新字段先允许为空，回填后再加约束；迁移脚本可重复执行；同一版本不混合"新结构上线"与"旧字段删除"；迁移前后自动对账。
- **数据对账基线（0.10、34）**：业务标的 7,498、观察项 6、发现候选 31、扫描结果 1,999、组合 2、持仓 3、模拟订单/成交 7/7、回测 22、净值快照 2、定时任务 11。迁移后任何现金/持仓/订单/成交对账不一致属阻断发布问题。

## ADDED Requirements

### Requirement: 功能开关与基线（WP0）

系统 SHALL 提供 4 个功能开关：`OPPORTUNITY_CENTER_ENABLED`、`PORTFOLIO_MEMBERS_ENABLED`、`AUTO_TRADE_MEMBER_SOURCE_ENABLED`、`PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED`。新页面默认开发环境开启，自动交易和组合回测新来源默认关闭。

#### Scenario: 全部开关关闭时行为一致
- **WHEN** 4 个功能开关全部为 `false`
- **THEN** 当前页面、自动交易和组合回测行为与改造前完全一致
- **AND** SQLite 新库、SQLite 旧库升级、MySQL 旧库升级均可正常启动
- **AND** 重复执行迁移不会重复回填、改写 ID 或报错

#### Scenario: 旧路由兼容
- **WHEN** 用户访问旧 URL 或旧 `activeTab` 值（如 `portfolio`）
- **THEN** 系统自动重定向到新入口，不返回 404

### Requirement: 外部数据网关与稳定性（WP-S）

系统 SHALL 提供统一外部数据网关 `external_data_gateway.py`，所有 AKShare/网页抓取类请求逐步通过该网关执行。网关统一完成 L1~L4 缓存查询、单飞去重、限流、熔断、SourceChain 降级、字段契约校验和幂等 UPSERT。

#### Scenario: 本地存在合格数据时不发起第三方请求
- **WHEN** 业务数据库或 DuckDB 中存在满足 `freshness_requirement` 的数据
- **THEN** 网关直接返回缓存结果
- **AND** 记录 `cache_hit=true` 和数据截止日期
- **AND** 不发起任何第三方 HTTP 请求

#### Scenario: 相同参数并发调用单飞
- **WHEN** 20 个相同 `interface_key + normalized_params` 的并发调用同时到达
- **THEN** 只产生 1 个真实网络请求
- **AND** 其余调用等待同一结果或读取旧缓存

#### Scenario: 接口连续失败后熔断
- **WHEN** 接口连续返回 429/403 或超时达到失败阈值
- **THEN** 进入 `open` 熔断状态，暂停真实请求
- **AND** 自动尝试备用源或旧缓存
- **AND** 冷却后只允许 1 个 half-open 探测请求

#### Scenario: 任务防卡死
- **WHEN** worker 异常退出或任务超过阶段预算
- **THEN** 巡检将其标记为 `interrupted` 或 `stalled`
- **AND** 不永远显示 `running`
- **AND** 强制取消后 DuckDB/数据库连接与文件锁均可再次获取

#### Scenario: 统一用户错误协议
- **WHEN** 业务错误发生
- **THEN** 后端返回 `error_code`、`user_message`、`impact`、`retryable`、`completed`、`next_actions`、`technical_details`、`correlation_id`
- **AND** 普通用户默认只看"发生什么、影响什么、下一步做什么"
- **AND** 技术堆栈、HTTP 响应、SQL 错误折叠在"技术详情"
- **AND** 不显示 `Request failed`、`HTTP 500`、`NoneType`、数据库锁或第三方原始 HTML

### Requirement: 5 分钟扫描 SLA（WP-P）

系统 SHALL 实现"数据准备与用户扫描分离"：后台数据准备（行情同步、因子计算、评分快照物化）不受 5 分钟约束；用户快速扫描在 ready 快照命中时 P95 ≤ 300 秒，相同快照/参数二次扫描 ≤ 10 秒。

#### Scenario: ready 快照命中
- **WHEN** A 股约 5,500 只或 ETF 约 1,600 只，存在状态为 `ready` 的最近评分快照
- **THEN** 快速扫描 P50 ≤ 60 秒，P95 ≤ 300 秒
- **AND** 快速扫描路径不发起任何第三方 HTTP 请求
- **AND** 扫描主要数据库查询数量为固定量级，不随标的数形成 N+1

#### Scenario: 无可用快照
- **WHEN** 不存在 ready 评分快照
- **THEN** 10 秒内明确返回"数据准备未完成"
- **AND** 同时给出上一快照或启动准备任务
- **AND** 不允许进度条无期限停住

#### Scenario: 相同参数结果复用
- **WHEN** 相同 `snapshot_id + scope + min_score + filter_hash + indicator_plan_version + portfolio_id + portfolio_rule_version` 的扫描再次发起
- **THEN** 直接返回已有结果
- **AND** 不重复写 `ScanResult`

#### Scenario: 写入量受 Top-K 限制
- **WHEN** 快速扫描完成
- **THEN** 只物化 Quality Top 300、Timing Top 300、最终 executable 候选
- **AND** 不再接近 `3 × 全市场标的数` 写入量

### Requirement: 机会中心信息架构（WP1）

系统 SHALL 新建"机会中心"一级入口，包含候选池、观察池、已排除、扫描记录四个页签；将"目前观察池"重命名为"组合交易"并保留 `activeTab=portfolio` 兼容值；投资中心暂留旧入口并显示"即将迁移为标的研究"提示。

#### Scenario: 新旧入口并存
- **WHEN** 用户访问机会中心或旧 Discovery 入口
- **THEN** 两者指向同一候选数据
- **AND** 不会把观察项显示成持仓，也不会把组合成员显示成已成交

#### Scenario: 状态徽标降级
- **WHEN** 统一状态接口失败
- **THEN** 徽标降级为"状态未知"
- **AND** 不误报"未加入"

### Requirement: 正式观察池（WP2）

系统 SHALL 扩展 `WatchlistItem` 增加 `origin_type`、`origin_id`、`reason_json`、`score_snapshot_json`、`status`、`priority`、`tags_json`、`target_portfolio_id`、`updated_at`、`archived_at` 等字段；新增 `observations` 服务提供富读模型和幂等加入接口。

#### Scenario: 候选加入观察池
- **WHEN** 用户从候选池点击"加入观察池"
- **THEN** 同一事务写入观察项和来源/评分快照
- **AND** 同名单同标的重复请求返回已有记录（非 409）

#### Scenario: 本地收藏迁移
- **WHEN** 前端首次加载检测到 `ic_favorites`
- **THEN** 显示"将导入 N 个、已存在 N 个、无效 N 个"
- **AND** 用户确认后批量幂等写入后端
- **AND** 成功后记录 `ic_favorites_migrated_v1`，暂不删除原值

#### Scenario: 现有 core 名单保留
- **WHEN** 迁移执行
- **THEN** `core` 观察池及 6 个观察项原样保留
- **AND** 历史无来源项标记为 `legacy/manual_unknown`，不伪造来源

### Requirement: 统一流转与审计（WP3）

系统 SHALL 新增 `opportunity_transitions.py` 领域服务，所有候选→观察→组合→订单的流转必须经过该服务，单事务写入业务对象和关联状态，幂等键防止双击/重试产生重复关系。

#### Scenario: 双击幂等
- **WHEN** 同一候选连续点击两次"加入观察"
- **THEN** 只产生 1 个观察项和 1 个成功事件
- **AND** 不返回错误

#### Scenario: 流转原子性
- **WHEN** 任一步骤异常
- **THEN** 所有写入回滚
- **AND** 不出现"候选已晋升但观察项没写成功"的半状态

#### Scenario: 候选状态兼容
- **WHEN** 第一阶段查询候选
- **THEN** 继续保留 `discovery_candidates.is_promoted`
- **AND** 由统一服务同步更新

### Requirement: 组合成员模型（WP4）

系统 SHALL 新增 `portfolio_members` 表，字段包含 `portfolio_id`、`symbol_id`、`status`、`execution_mode`、`source_type/source_id`、`entry_rule_version_id`、`exit_rule_version_id`、`effective_from`、`effective_to`、`manual_lock`、`priority`、`note`。同一组合同一标的只能存在一条当前有效成员关系。

#### Scenario: 持仓回填
- **WHEN** 执行回填脚本
- **THEN** 对每条现有 `positions` 创建成员，`source_type=legacy_position`
- **AND** `effective_from` 优先取 `Position.opened_at`
- **AND** Position ID、数量、成本、最新价和持仓比例完全不变

#### Scenario: 成员不等于持仓
- **WHEN** 新增成员
- **THEN** 不改现金、不下单、不创建 `Position`
- **AND** 无持仓成员可以存在，账户权益不变化

#### Scenario: 持仓成员归档保护
- **WHEN** 用户尝试归档存在持仓的成员
- **THEN** 提示并要求选择"仅停止买入"或先卖出
- **AND** 不误删持仓

### Requirement: 标的研究收口（WP5）

系统 SHALL 将 `InvestmentCenter.tsx` 拆分为 9 个子组件（`SymbolResearchShell`、`SymbolSearchHeader`、`SymbolRelationshipBar`、`FactorExplanationPanel`、`SymbolAlertSummary`、`RiskReferencePanel`、`TradePlanPanel`、`SymbolChartPanel`、`SingleSymbolBacktestPanel`），收藏/告警/风控/下单迁出。

#### Scenario: 来源上下文统一
- **WHEN** 从候选、观察、组合、告警或回测进入标的研究
- **THEN** 携带统一参数对象（`symbol_id`、`source_type`、`source_id`、`portfolio_id`、`return_to`）
- **AND** 返回时保留原筛选和滚动位置

#### Scenario: 收藏单一真相
- **WHEN** 用户点击星标
- **THEN** 直接读写后端观察池
- **AND** 本地 `ic_favorites` 进入只读回退期

### Requirement: 自动交易成员化（WP6）

系统 SHALL 将自动交易买入来源从"最新扫描 executable"切换为"`active PortfolioMember` AND `execution_mode=auto` AND 当前无持仓 AND 最新有效信号允许买入 AND 数据健康通过 AND 组合风控通过"；卖出侧必须覆盖所有当前持仓。`SimOrder` 扩展 `member_id`、`source_type/source_id`、`signal_id`、`rule_version_id`、`execution_mode`、`client_order_key`、`decision_snapshot_json`。

#### Scenario: 三种执行模式互不混淆
- **WHEN** `manual`/`confirm`/`auto` 三种模式分别运行
- **THEN** `manual` 只提示信号不下单
- **AND** `confirm` 只生成待确认订单计划
- **AND** `auto` 满足规则后程序自动模拟下单

#### Scenario: 幂等订单
- **WHEN** 同一任务重复执行或调度重跑
- **THEN** 不重复下单
- **AND** `client_order_key`（组合+成员+信号日期+方向+规则版本）唯一索引生效

#### Scenario: 双跑切换
- **WHEN** `AUTO_TRADE_MEMBER_SOURCE_ENABLED=false`
- **THEN** 旧来源实际执行，新来源仅 Dry Run
- **AND** 连续 5 个交易日或 3 次有效运行保存旧/新差异
- **AND** 差异经人工确认后逐组合切换

#### Scenario: 数据过期 fail-closed
- **WHEN** K 线、评分或规则版本过期
- **THEN** 禁止买入
- **AND** 卖出风控不得静默跳过，应生成高优先级告警

### Requirement: 组合回测成员化（WP7）

系统 SHALL 将组合回测标的来源从"今天的持仓 + 最新扫描结果"切换为按每个交易日读取当时有效成员（`effective_from <= trade_date AND (effective_to IS NULL OR effective_to >= trade_date)`）。每次运行保存成员 ID、有效日期、执行模式、规则版本、组合风控版本、成本配置、评分模式、因子模型运行 ID、数据截止时间、引擎名称和版本、运行时排除标的及原因。

#### Scenario: 未来成员不污染过去
- **WHEN** 回测过去某日期
- **THEN** 未来才加入的成员不出现在该日期回测中
- **AND** 中途归档成员只参与有效期内回测

#### Scenario: 历史快照可读
- **WHEN** 成员、规则或模型后来改变
- **THEN** 历史回测继续按原快照可读
- **AND** 切回旧开关后原 22 条历史回测仍可查看

#### Scenario: 新旧引擎对比
- **WHEN** `PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED=false`
- **THEN** 继续运行旧推导逻辑
- **AND** UI 明确显示使用"旧临时标的集"还是"历史成员集"

### Requirement: 绩效归因与复盘（WP8）

系统 SHALL 在现有收益、回撤、Sharpe、胜率、盈亏比基础上增加按组合成员贡献、按执行模式贡献、按候选来源/观察标签贡献、按规则版本/信号类型/退出原因贡献、回测与模拟账户同期偏差、成本/滑点/未成交/风控阻断影响的归因维度。

#### Scenario: 订单↔成员↔信号联动
- **WHEN** 用户在订单或成交列表点击某条记录
- **THEN** 可打开对应成员与信号上下文
- **AND** 绩效异常可一键创建复盘记录

#### Scenario: 样本不足提示
- **WHEN** 绩效样本数不足
- **THEN** 显示样本数和限制
- **AND** 不展示具有误导性的稳定结论

### Requirement: 旧入口清理（WP9）

系统 SHALL 仅在 WP1~WP8、WP-AI、WP-MSG 全部验收后执行旧入口清理，从一级导航移除旧投资中心入口但保留兼容路由跳转；停止写入 `ic_favorites`、停止前端即时提醒称为正式告警、移除组合工作台中的机会/观察重复区块、停止自动交易和组合回测读取最新扫描作为默认来源。

#### Scenario: 历史数据保留
- **WHEN** 清理阶段执行
- **THEN** 不删除历史候选、观察、组合、持仓、订单、成交、回测、净值快照和告警事件
- **AND** 旧 API/字段进入废弃期并记录访问日志

### Requirement: 量化助手（WP-AI）

系统 SHALL 提供"系统使用与量化研究副驾驶"AI 助手，第一阶段开放只读工具集（`get_capabilities`、`get_data_health`、`get_task_status`、`get_symbol_research`、`get_candidate_explanation`、`get_portfolio_summary`、`get_backtest_explanation`），第二阶段开放草稿工具（自定义指标/筛选方案/告警规则/观察备注/复盘/模拟订单草稿）。所有有副作用的操作使用三步流程：AI 建议 → 系统规则校验和变更预览 → 用户明确确认后由普通业务 API 执行。

#### Scenario: AI 回答包含依据
- **WHEN** AI 回答用户问题
- **THEN** 响应包含 `answer`、`evidence`、`warnings`、`suggested_actions`、可选 `draft`
- **AND** 附"数据截至、模型/规则版本和依据对象"

#### Scenario: AI 不绕过确认
- **WHEN** AI 建议写操作
- **THEN** 不确认时不产生任何数据库变化
- **AND** 模拟订单即使由 AI 起草，也必须重新经过现金、手数、T+1、涨跌停、数据健康和组合风控校验
- **AND** 自动交易永远由策略规则和调度器负责，不由对话直接触发

#### Scenario: AI 失败降级
- **WHEN** AI 无法连接、超时、限流或返回格式异常
- **THEN** 用户看到可理解错误
- **AND** 扫描、回测、告警和交易等核心功能正常

#### Scenario: 敏感信息保护
- **WHEN** 任何 AI 请求和日志记录
- **THEN** 不出现数据库密码、AI Key、Webhook、SMTP 密码或完整 Secret

### Requirement: 统一消息管理（WP-MSG）

系统 SHALL 新增 `notification_channels`、`notification_policies`、`notification_policy_channels`、`notification_outbox`、`notification_deliveries`、`notification_templates` 数据模型；业务事务只写 Outbox，后台 dispatcher 异步发送；至少交付站内、WxPusher、钉钉、QQ/OneBot、邮件五类渠道的配置和测试入口。

#### Scenario: 多渠道多来源
- **WHEN** 用户配置推送策略
- **THEN** 可同时选择多个消息来源和多个渠道
- **AND** 未配置或测试失败的第三方渠道不能被策略选中
- **AND** 明确提示如何处理

#### Scenario: Outbox 幂等
- **WHEN** 同一业务事件重放
- **THEN** `event_key + channel_id` 唯一约束生效
- **AND** 不向同一渠道重复发送

#### Scenario: 渠道故障隔离
- **WHEN** 任一外部平台超时/限流/鉴权失败
- **THEN** 站内消息和其他渠道仍正常
- **AND** 鉴权失败直接暂停该渠道并产生站内系统告警

#### Scenario: 服务重启恢复
- **WHEN** dispatcher 重启
- **THEN** 继续处理未完成 Outbox
- **AND** 用户可对失败记录手动重发，继续使用同一业务事件审计链

## MODIFIED Requirements

### Requirement: 一级导航
当前一级导航 7 项调整为 6 项以内：今日决策、机会中心、组合交易、市场环境、消息与告警、设置。原"投资中心"从一级导航移除，核心研究能力改造成公共"标的研究页"由其他模块携带上下文进入。

### Requirement: 候选池生命周期
候选状态统一为 `new → reviewed → watched → portfolio → excluded/expired`。候选有效期基于生成时的 `warning_days/valid_days`，不使用当前配置反向覆盖历史候选。

### Requirement: 观察池
观察池从通用名单弹窗变为正式页面，补齐来源、加入原因、加入时评分、当前信号、价格变化、数据新鲜度、最近回测摘要、所属标签、优先级、备注、目标组合。观察分类使用标签，不再鼓励创建大量独立名单。

### Requirement: 自动交易执行优先级
冲突优先级：1. 手动锁定 → 2. 组合级风险强制减仓/清仓 → 3. 自动卖出规则 → 4. 自动买入规则 → 5. 普通信号建议。

### Requirement: 组合回测前提
完整组合回测前提：全部有效成员均为 `auto` 且规则有效。若存在 `manual`/`confirm` 成员，默认禁止完整回测，并提供"仅回测自动成员"选项及排除清单。

### Requirement: 回测与模拟账户隔离
历史回测不得直接污染当前模拟账户。回测结果应用到组合时必须创建独立演示账户，或明确清空后重放并要求二次确认。默认行为是保存回测结果和参数，不向当前账户写入订单。

## REMOVED Requirements

### Requirement: 投资中心一级入口
**Reason**: 投资中心职责过载（搜索/K 线/因子/收藏/提醒/风险/计划/回测同时承担），与机会中心、观察池、组合交易职责重叠。
**Migration**: 保留现有组件能力，逐步拆分为标的研究子组件；原一级导航先隐藏并保留兼容跳转；旧链接自动重定向到标的研究页。本地收藏迁入后端观察池后停止写入 `ic_favorites`。

### Requirement: 通用观察池弹窗作为主入口
**Reason**: 当前 `MetricModal` 作为通用观察池入口，缺少来源/信号/标签/目标组合等关键上下文，看起来像没有明确用途的收藏夹。
**Migration**: 移除 `MetricModal` 作为主要入口，建立独立观察池列表/筛选/详情/批量操作页面；"加入观察池"统一走 `opportunity_transitions` 后端服务；现有 `core` 名单及 6 个观察项原样保留，历史无来源项标记 `legacy/manual_unknown`。

### Requirement: 自动交易读取最新扫描 executable
**Reason**: 买入来源为该组合最新一次成功扫描中的 `executable` 结果，无法稳定复现历史成员变化；自动交易只有组合级开关，缺少成员级 manual/confirm/auto 三种模式。
**Migration**: 双轨切换——`AUTO_TRADE_MEMBER_SOURCE_ENABLED=false` 时旧来源实际执行，新来源仅 Dry Run；连续 5 个交易日或 3 次有效运行保存旧/新差异；差异经人工确认后逐组合切换；旧来源至少保留一个发布周期。

### Requirement: 组合回测读取"当前持仓 + 最新扫描"
**Reason**: 标的集合由"当前持仓 + 最新扫描可执行结果"临时推导，无法稳定复现历史成员变化。
**Migration**: 双轨切换——`PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED=false` 时继续运行旧推导逻辑；UI 明确显示使用"旧临时标的集"还是"历史成员集"；新旧引擎使用相同日期、资金、成本后做对比；切回旧开关后原 22 条历史回测仍可查看。

### Requirement: 投资中心本地收藏与即时提醒
**Reason**: `ic_favorites` 保存在 `localStorage`，不是后端业务真相；页面内价格提醒只是即时前端计算，不等于告警中心的持久化规则和事件。
**Migration**: 本地收藏一次性迁入默认观察池，去重后写入后端，迁移成功后停止使用本地收藏状态；正式价格/评分/公式提醒通过 `alert_rules` 创建，页面内即时计算只作为"当前提示"并标注未持久化。
