# Tasks

> 任务排序遵循底稿第 42 章"推荐整体实施顺序"：
> `WP0 → WP-S → WP-P → WP1/WP2 → WP-MSG → WP3/WP4/WP5 → WP-AI → WP6/WP7/WP8 → WP9`
>
> 每个 WP 内部按"基础设施 → 服务 → 接口 → 前端 → 测试 → 对账"顺序拆解。
> 所有 WP 必须遵守 13.1 总体约束与 15.1 数据库迁移原则。

## Phase 0：基线与开关

### Task WP0: 基线、迁移框架与功能开关

- [x] WP0.1 在 `app/core/config.py` 新增 4 个功能开关字段
  - `OPPORTUNITY_CENTER_ENABLED`（默认 `False`，开发环境可开启）
  - `PORTFOLIO_MEMBERS_ENABLED`（默认 `False`）
  - `AUTO_TRADE_MEMBER_SOURCE_ENABLED`（默认 `False`）
  - `PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED`（默认 `False`）
  - 验证：开关关闭时所有现有行为零变化
- [x] WP0.2 在 `app/db/init_db.py` 建立迁移框架
  - 为后续新增表/列建立 Alembic 迁移目录结构 `alembic/versions/`
  - 保留 `init_db.py` 的轻量兼容迁移能力（SQLite `_ensure_sqlite_*_columns` + MySQL `information_schema.COLUMNS` 检查）
  - 验证：SQLite 新库、SQLite 旧库升级、MySQL 旧库升级均可启动
- [x] WP0.3 记录 schema 与核心数量基线
  - 在 `docs/migration-baseline-2026-07-19.json` 保存：`watchlists=1`、`watchlist_items=6`、`portfolios=2`、`positions=3`、`cash_ledger=9`、`sim_orders=7`、`sim_trades=7`、`backtest_runs=22`、`portfolio_equity_snapshots=2`、`scheduled_tasks=11`、`alert_rules=3`、`alert_events=95`、`discovery_candidates=31`、`scan_runs=137`、`scan_results=1999`、`symbols=7498`
  - 验证：对账脚本确认迁移后数量不减少
- [x] WP0.4 旧路由与 `activeTab` 兼容映射
  - 在 `frontend/src/App.tsx` 与 `frontend/src/context/AppContext.tsx` 建立 URL/Tab 兼容映射表（`portfolio` → 新"组合交易"、`discovery` → 新"机会中心"等）
  - 旧 URL 自动重定向，不返回 404
  - 验证：旧深链接可访问，跳转目标正确

## Phase 1：稳定性与性能底座

### Task WP-S: 接口风控、本地缓存与程序稳定性

- [x] WP-S.1 新增统一外部数据网关 `app/services/external_data_gateway.py`
  - 接口签名：`interface_key`、`request_params`、`freshness_requirement`、`allow_stale`、`preferred_sources`、`task_context`
  - 实现 L1 进程缓存（秒/分钟级 TTL）→ L2 业务数据库 → L3 DuckDB/快照 → L4 第三方接口的优先级查询
  - 命中缓存时返回 `cache_hit=true` 与数据截止日期
  - 验证：本地存在合格数据时不发起第三方请求
- [x] WP-S.1b 补充用户显式要求的入口 API（与现有 `fetch()` 共存，作为兼容包装层）
  - 新增 `CacheLevel` 枚举（L1_PROCESS / L2_BUSINESS_DB / L3_DUCKDB / L4_REMOTE / NONE）
  - 新增 `GatewayRequest` dataclass（含 interface_key / request_params / freshness_requirement / allow_stale / preferred_sources / task_context）
  - 新增 `async def fetch_via_gateway(req: GatewayRequest) -> GatewayResponse` 主入口（透传到现有 `fetch()`）
  - 在 `__all__` 导出新增 API
  - 验证：`from app.services.external_data_gateway import fetch_via_gateway, GatewayRequest, GatewayResponse, CacheLevel` 可正常导入
- [x] WP-S.2 实现单飞、限流与熔断
  - 相同 `interface_key + normalized_params` 同时只允许一个真实网络请求
  - 每个主机、接口、任务类型分别设置并发上限
  - 429/403/连接重置/连续超时采用指数退避+随机抖动
  - 新增 `external_endpoint_runtime` 模型记录 `state`（closed/open/half_open）、`consecutive_failures`、`cooldown_until`、`last_error_code`、`request_count/cache_hit_count/fallback_count`
  - 验证：20 个并发调用只产生 1 个真实请求；连续失败后进入熔断；冷却后只允许 1 个 half-open 探测
- [x] WP-S.3 复用现有 SourceChain 降级链
  - 网关调用 `akshare_registry.py` 与现有 SourceChain（东财→新浪→腾讯→本地通达信等）作为接口策略与数据源适配层
  - 数据源降级后保存真实 `source`，不假装来自主接口
  - 验证：主源失败时按现有链尝试备用源
- [x] WP-S.3b 补充用户显式要求的 SourceChain 注册接口
  - 新增 `register_source_chain(interface_key, source_chain)`：注册一个 `SourceChain` 实例到指定 `interface_key`
  - 新增 `get_source_chain(interface_key) -> SourceChain | None`：查询已注册的 SourceChain
  - 注册后 L4 默认 fetcher 优先使用已注册的 SourceChain；未注册时回退到现有 `get_chain(symbol)` 路径
  - 在 `__all__` 导出新增 API
  - 验证：`from app.services.external_data_gateway import register_source_chain, get_source_chain` 可正常导入
- [x] WP-S.4 实现本地入库稳定性
  - 字段契约和日期范围校验先于业务表写入
  - UPSERT 唯一键由业务主键、日期、来源组成
  - 大批量写入按块提交，失败块可重试
  - 快照采用临时批次→校验→原子切换 `ready`
  - 网络请求前释放长期数据库事务，网络后建立短事务
  - DuckDB 连接使用上下文管理器和单写锁，取消/失败/退出 `finally` 释放
  - 验证：随机注入超时/429/空字段/断连/格式变化时数据不重复不丢失
- [x] WP-S.4b 补充用户显式要求的本地入库稳定性的独立模块
  - 新建 `app/services/local_persistence.py` 模块
  - 实现 `validate_record_contract(rows, *, required_fields, date_fields=(), numeric_fields=(), date_range=None, numeric_range=None) -> None`：包装现有 `external_data_gateway.validate_rows`
  - 实现 `batch_upsert(table, rows, conflict_keys, *, db=None, batch_size=None, max_retries=None) -> int`：包装现有 `external_data_gateway._upsert_batch`
  - 实现 `with_short_transaction(db: Session | None = None)` 上下文管理器：网络请求后建立短事务，单事务提交/回滚，自动关闭自建 session
  - 验证：`from app.services.local_persistence import validate_record_contract, batch_upsert, with_short_transaction` 可正常导入
- [x] WP-S.5 实现任务防卡死状态机
  - 新增 `app/services/task_state_machine.py`，状态：`queued/running/paused/cancelled/done/failed/interrupted/stalled`
  - 每个阶段有预算时间、心跳时间、最后进度时间
  - worker 异常退出由巡检标记 `interrupted`
  - 超过阶段预算进入 `stalled`，显示当前步骤、已完成量、建议操作
  - 取消时停止后续批次、关闭线程池、释放 DB/DuckDB 连接与文件锁
  - 任务重启从已提交批次恢复
  - 前端轮询有最大间隔、断线恢复、终态停止
  - 验证：worker 强制终止后任务在巡检窗口内进入 `interrupted`；强制取消因子/同步/挖掘任务后连接与文件锁可再次获取
- [x] WP-S.6 实现统一用户错误协议
  - 新增 `app/schemas/errors.py`，字段：`error_code`、`user_message`、`impact`、`retryable`、`completed`、`next_actions`、`technical_details`、`correlation_id`
  - 普通用户默认只看 user_message/impact/next_actions；技术堆栈/HTTP/SQL 折叠在 technical_details
  - 同一 error_code 在所有页面使用一致中文文案
  - 禁止裸露 `Request failed`、`HTTP 500`、`NoneType`、数据库锁、第三方原始 HTML
  - 后台日志不得包含 API Key、Webhook、SMTP 密码或完整用户数据
  - 验证：随机注入异常时用户能看到可理解错误和下一步
- [x] WP-S.7 前置条件引导与页面门禁
  - 新增 `GET /api/v1/system/capabilities` 聚合接口，返回每项功能的 `status`（ready/degraded/blocked）、`reason_code`、`user_message`、`prerequisites`、`recommended_actions`、`data_cutoff_at`
  - 覆盖：基础数据采集、行情新鲜度、评分配置激活、Ridge 因子仓库与活动模型、机会扫描快照、组合操作前组合/成员/规则/模拟账户、自动交易前账户开启/数据健康/规则、AI 配置测试启用、外部消息渠道配置测试启用
  - 页面门禁：按钮显示禁用原因（非无解释灰色），提供"去完成前置条件"入口，条件 ready 后自动刷新
  - degraded 状态允许安全只读/缓存操作，明确数据日期与限制
  - 验证：新用户不阅读文档也能按向导完成第一次扫描；任意后续操作被阻断时可在当前页面完成或跳转处理前置条件
- [x] WP-S.8 WP-S 回归测试
  - 新增 `tests/test_whitebox_external_data_gateway.py`（缓存命中/单飞/熔断/降级）
  - 新增 `tests/test_whitebox_task_state_machine.py`（interrupted/stalled/取消释放）
  - 新增 `tests/test_whitebox_unified_errors.py`（错误协议字段完整/敏感信息脱敏）
  - 新增 `tests/test_whitebox_capability_gates.py`（前置条件状态）
  - 验证：见 WP-S.6 验收清单

### Task WP-P: 挖掘性能与 5 分钟扫描 SLA

- [x] WP-P.1 增加阶段耗时与复用数监控
  - 在 `DiscoveryTaskRecord` 或任务结果补充：`snapshot_id`、`snapshot_hit`、`stage_durations_json`、`dirty_symbol_count`、`reused_score_count`、`rescored_count`、`coarse_match_count`、`advanced_match_count`、`result_rows_written`、`cache_key`、`cache_hit`、`degraded_reason`、峰值内存与关键 SQL 耗时日志
  - 前端进度条按真实阶段显示
  - 验证：固定真实性能基线
- [x] WP-P.2 新增评分快照模型
  - 新增 `app/models/discovery_score_snapshot.py`：`discovery_score_snapshots`（id/scope/trade_date/status=building|ready|failed|superseded/scoring_config_id/version/weight_mode/factor_model_run_id/data_cutoff_at/generated_at/symbol_count/coverage_pct/dirty_symbol_count/source_task_id/build_duration_seconds/error_summary_json）
  - 新增 `discovery_score_snapshot_items`（snapshot_id/universe_symbol_id/symbol_id/Quality/Timing/Priority/维度分数/stage/action/data_credibility/原因与健康摘要）
  - 复合索引：`(scope, status, trade_date, generated_at)`、`(snapshot_id, priority_score)`、`(snapshot_id, action, stage, priority_score)`、`(snapshot_id, universe_symbol_id)` 唯一约束
  - 配置/模型/数据截止信息存 manifest，不在每个 item 重复
  - 快照生成完成后一次性 `building` → `ready`，扫描永不读半成品
  - 验证：SQLite/MySQL 双库迁移可重复执行
- [x] WP-P.3 实现增量失效规则
  - 标的进入 dirty 集合的条件：`universe_symbols.last_synced_at` 晚于当前快照对应 Score 创建时间、最新 `last_bar_date` 变化、财报/资金/人气/龙虎榜/尾盘数据在快照后更新、定向数据修复成功
  - 触发新版本全量快照的条件：激活新评分配置版本、切换手工/影子/Ridge 权重模式、激活新因子模型、修改影响全市场的宏观权重或基础公式
  - 新快照 ready 前继续提供旧快照
  - 同一 scope、同一配置版本同时只允许一个快照构建任务
  - 验证：dirty 集合只包含真实变化标的
- [x] WP-P.4 数据准备与用户扫描分离
  - 后台数据准备链：行情增量同步 → 外部因子/宏观更新 → 因子与评分增量计算 → 生成 ready 评分快照（不受 5 分钟约束）
  - 用户快速扫描链：读取 ready 快照 → SQL 粗筛和排序 → Top-K 高级指标过滤 → 组合约束过滤 → 保存小规模候选结果
  - 默认"开始挖掘"执行快速扫描，不隐式触发第三方网络同步
  - 复用设置中 `UniverseDataPanel.tsx`、`HistoryInitSection.tsx`、`ScheduledTaskManager.tsx`，不在 Discovery 内复制抓取代码
  - 机会中心显示最近数据准备任务、快照日期、健康状态，提供"前往基础数据""运行现有增量同步""数据就绪后自动扫描"三个联动入口
  - 验证：快速扫描路径发起任何第三方 HTTP 请求即测试失败
- [x] WP-P.5 实现两阶段过滤
  - SQL 粗筛：在快照表上完成分数阈值、资产类型、阶段、动作、可信度、排序，保留 Top 300
  - 高级筛选：只对 Top 300 批量加载 K 线，向量化计算最多 5 个自定义指标
  - 组合风控只对高级筛选后最终集合执行
  - 消息分数读取最近缓存，不在用户扫描中同步抓取新闻
  - 全市场逐只复杂自定义公式定义为"预计算指标"由后台快照物化
  - 验证：自定义指标只处理 Top 300，超限明确拒绝或转后台预计算
- [x] WP-P.6 实现相同参数结果复用
  - 扫描缓存键：`snapshot_id + scope + min_score + filter_hash + indicator_plan_version + portfolio_id + portfolio_rule_version`
  - 缓存键完全相同时直接返回已有结果，不重复写 `ScanResult`
  - 第一版兼容 `ScanRun/ScanResult`，只物化 Quality Top 300、Timing Top 300、最终 executable 候选
  - 总数和分布统计写入 `ScanRun` 摘要，不为 5,500 只标的各写三类结果
  - 验证：相同快照/参数二次扫描 ≤ 10 秒；单次运行写入结果数受 Top-K 限制
- [x] WP-P.7 扩展分层保留与清理
  - 把 `cleanup_expired_discovery_results` 扩展为分层清理服务
  - 当前候选展示：默认 5 天，3 天预警，过期从活动候选隐藏
  - 未晋升 `ScanResult`：7 天且每 scope 至少保留最近 3 次，到期物理删除
  - 未晋升 `DiscoveryCandidate`：7 天到期物理删除
  - 已晋升/已观察/已入组合快照：长期保留，迁入正式对象后按业务审计保留
  - 评分快照 items：最近 20 个交易日/每 scope，只由后台清理
  - `ScanRun` 摘要：180 天，删除大明细后保留参数、数量、耗时、错误摘要
  - 日 K 与基础因子：按模型/回测所需窗口长期保留，禁止随候选过期删除
  - Score 历史：至少 250 个交易日
  - 候选晋升为观察项或组合成员时把入选评分和来源复制为正式快照
  - 保留启动清理、周期清理、手动清理入口
  - 验证：候选清理后底层快照仍可复用
- [x] WP-P.8 5 分钟阶段预算与可观测性
  - 阶段预算：快照与数据健康预检 10s + SQL 粗筛排序 Top-K 30s + 高级指标批量计算 90s + 组合约束过滤 60s + 候选快照与摘要写入 60s + 收尾审计前端返回 30s = 280s + 预留 20s
  - 无高级指标或组合过滤时目标 60s 内完成
  - 超过阶段预算显示具体慢在哪一步，允许取消
  - 验证：A 股 5,500 只、ETF 1,600 只，ready 快照命中 P95 ≤ 300s
- [x] WP-P.9 WP-P 测试
  - 新增 `tests/test_whitebox_discovery_snapshot.py`（快照 building/ready/failed/superseded 状态机）
  - 新增 `tests/test_whitebox_discovery_incremental.py`（dirty 集合判定）
  - 新增 `tests/test_whitebox_discovery_result_retention.py`（分层清理）
  - 新增 `tests/test_whitebox_discovery_fast_scan.py`（缓存命中/Top-K/无第三方请求）
  - 新增 `tests/performance/test_discovery_5500_sla.py` 标记 `slow`，发布环境运行
  - 验证：见 WP-P 性能验收清单

## Phase 2：机会中心与观察池

### Task WP1: 信息架构壳层与只读关联状态

- [x] WP1.1 新建"机会中心"一级入口
  - 新增 `frontend/src/components/OpportunityCenter.tsx`
  - 第一版组合现有 Discovery 数据，不改扫描算法
  - 提供"候选池、观察池、已排除、扫描记录"四个页签；未完成页签显示建设状态，不伪造数据
  - 验证：候选数据与旧 Discovery 入口一致
- [x] WP1.2 新建机会中心子组件
  - 新增 `frontend/src/components/opportunity/CandidatePool.tsx`（候选列表，复用 Discovery 结果表抽成可复用组件）
  - 新增 `frontend/src/components/opportunity/ObservationPool.tsx`（观察列表，第一阶段只读展示现有 watchlist_items）
  - 新增 `frontend/src/components/opportunity/OpportunityStatusBadges.tsx`（候选/已观察/组合成员/持仓/告警统一徽标）
  - 验证：徽标可解释数据来源；接口失败降级"状态未知"，不误报"未加入"
- [x] WP1.3 重命名"目前观察池"为"组合交易"
  - 修改 `frontend/src/App.tsx` 入口名称与 i18n
  - 保留 `activeTab=portfolio` 兼容值
  - 验证：旧深链接可访问
- [x] WP1.4 投资中心暂留旧入口+迁移提示
  - 保留 `InvestmentCenter.tsx` 旧入口
  - 增加"即将迁移为标的研究"说明与来源面包屑
  - 验证：旧入口可访问，提示可见
- [x] WP1.5 增加统一只读状态接口
  - 新增 `GET /api/v1/symbols/{symbol_id}/relationships`（返回候选/观察/组合成员/持仓/告警状态）
  - 前端 selector：`useSymbolRelationships(symbolId)`
  - 今日决策、候选列表、组合页使用统一徽标并能打开现有详情弹窗
  - 验证：不会把观察项显示成持仓，也不会把组合成员显示成已成交
- [x] WP1.6 i18n 同步
  - `frontend/src/i18n/zh-CN.ts` 与 `en-US.ts` 同步新增 key
  - 验证：两种语言显示一致
- [x] WP1.7 WP1 测试
  - 新增 `frontend/src/components/__tests__/OpportunityCenter.test.tsx`
  - 新增 `frontend/src/components/__tests__/OpportunityStatusBadges.test.tsx`
  - 扩展 `tests/test_blackbox_api.py`（relationships 接口）
  - 验证：见 WP1 验收清单

### Task WP2: 正式观察池与本地收藏迁移

- [x] WP2.1 扩展 `WatchlistItem` 数据模型
  - 修改 `app/models/watchlist.py`：新增 `origin_type`（manual/candidate/scan_result/alert/legacy_manual_unknown）、`origin_id`、`reason_json`、`score_snapshot_json`、`status`（watching/ready/invalid/archived）、`priority`、`tags_json`、`target_portfolio_id`、`updated_at`、`archived_at`
  - 在 `app/db/init_db.py` 增加 SQLite `_ensure_sqlite_watchlist_item_columns` + MySQL `information_schema.COLUMNS` 检查（参照 project_memory 硬约束）
  - 同步 Alembic 迁移
  - 验证：SQLite/MySQL 双库升级幂等
- [x] WP2.2 新增 `observations` 服务
  - 新增 `app/services/observations.py`
  - 富读模型返回标的、最新行情、最新评分、数据健康、来源、组合关系
  - 幂等加入接口：同名单同标的重复请求返回已有记录（非 409）
  - 支持更新标签、优先级、原因、目标组合、状态
  - 支持归档/恢复，默认不物理删除业务历史
  - 候选加入观察时同一事务写来源与评分快照
  - 验证：候选加入观察得到同一条后端记录
- [x] WP2.3 扩展 watchlists 路由
  - 修改 `app/api/routes/watchlists.py`：新增富读列表、幂等加入、批量更新、归档/恢复接口
  - 修改 `app/schemas/watchlist.py`：新增 `ObservationRead`、`ObservationCreate`、`ObservationUpdate`、`ObservationBatchImport`
  - 验证：接口幂等性测试通过
- [x] WP2.4 前端观察池正式页面
  - 完善 `frontend/src/components/opportunity/ObservationPool.tsx`：列表、筛选、详情、批量操作、空/错/加载状态
  - 修改 `InvestmentCenter.tsx`：本地收藏按钮改为加入观察池（仍保留只读回退期）
  - 验证：清空浏览器缓存后正式观察池数据仍存在
- [x] WP2.5 本地收藏迁移工具
  - 前端首次加载执行可见可取消的导入：读取 `ic_favorites` → 与后端 `core` 比对 → 显示"将导入 N/已存在 N/无效 N" → 用户确认后批量幂等写入 → 成功后记录 `ic_favorites_migrated_v1`
  - 暂不立即删除原值，至少保留一个版本回退期
  - 验证：迁移可重复执行且不产生重复项
- [x] WP2.6 现有 core 名单保留
  - 现有 `core` 名单及 6 个观察项原样保留
  - 历史无来源项统一标记 `legacy/manual_unknown`，禁止伪造来源
  - 验证：迁移后查询确认 6 项仍存在
- [x] WP2.7 WP2 测试
  - 扩展 `tests/test_whitebox_watchlists_portfolios.py`
  - 新增 `tests/test_whitebox_observations.py`
  - 新增 `frontend/src/components/__tests__/ObservationPool.test.tsx`
  - 新增 `frontend/src/components/__tests__/LocalFavoritesMigration.test.tsx`（WP2.5 迁移工具测试）
  - 验证：见 WP2 验收清单

## Phase 3：消息管理

### Task WP-MSG: 统一消息管理与多渠道推送

- [x] WP-MSG.1 新增通知数据模型
  - 新增 `app/models/notification.py`：`notification_channels`（id/name/channel_type/enabled/status/config_encrypted_json/verified_at/last_test_at/last_error_code）、`notification_policies`（id/name/enabled/source_types_json/min_severity/scope_type/scope_ids_json/delivery_mode/digest_schedule/quiet_hours_json/cooldown_minutes/dedup_window_minutes/template_id）、`notification_policy_channels`（policy_id/channel_id 唯一）、`notification_outbox`（event_key 唯一/source_type/source_id/channel_id/policy_id/payload_json/status/attempt_count/next_retry_at）、`notification_deliveries`（每次发送尝试/耗时/状态码/脱敏响应摘要/错误码）、`notification_templates`（标题/正文/Markdown|纯文本/变量定义/版本）
  - 敏感字段用 Secret Store 引用或加密存储
  - 同步 Alembic 迁移与 `init_db.py` SQLite/MySQL 兼容补丁
  - 验证：双库迁移可重复执行
- [x] WP-MSG.2 实现渠道适配器
  - 新增 `app/services/notifications/base.py`（统一接口：validate_config/send_test/send_message/normalize_error/mask_config）
  - 新增 `app/services/notifications/in_app.py`（站内消息，默认渠道）
  - 新增 `app/services/notifications/wxpusher.py`（Endpoint/AppToken/UID|Topic ID）
  - 新增 `app/services/notifications/dingtalk.py`（Webhook/签名 Secret/关键词）
  - 新增 `app/services/notifications/onebot.py`（Webhook/Access Token/目标群|用户 ID/消息类型）
  - 新增 `app/services/notifications/email.py`（SMTP 主机/端口/TLS|SSL/用户名/密码|授权码/发件人/收件人）
  - 新增 `app/services/notifications/webhook.py`（URL/HTTP 方法/鉴权 Header/请求模板/超时）
  - 适配器统一使用有限超时、重试、熔断，不允许第三方 SDK 建立无法取消的永久线程
  - 验证：各渠道配置/测试/发送接口可用
- [x] WP-MSG.3 实现 Outbox 与异步 dispatcher
  - 新增 `app/services/notifications/dispatcher.py`
  - 业务事务只写 Outbox，不直接等待第三方
  - 发送规则：`event_key + channel_id` 唯一防重；超时与临时错误指数退避；达上限进 dead-letter；鉴权失败直接暂停渠道并产生站内系统告警；一渠道失败不影响其他渠道；dispatcher 重启后继续处理未完成 Outbox；用户可手动重发失败记录
  - 验证：服务重启后未发送 Outbox 可继续处理
- [x] WP-MSG.4 消息来源与推送策略
  - 消息来源分组：系统运行、数据与接口、因子与模型、机会与观察、组合与交易、绩效与复盘
  - 推送策略：策略名/启停、一个或多个消息来源/事件类型、最低严重级别、一个或多个渠道、适用标的/观察池/组合范围、即时或定时摘要、冷却/去重/免打扰、消息模板
  - 渠道状态机：未配置 → 已配置待测试 → 测试成功 → 已启用；测试失败为独立状态
  - 只有"测试成功+已启用"的第三方渠道才能在策略中勾选；未满足时选项禁用，提供"去配置""立即测试"入口
  - 验证：未配置或测试失败的渠道不能被策略选中
- [x] WP-MSG.5 防打扰与隐私
  - 相同来源/标的/规则/状态在去重窗口内只发送一次
  - 高频 info/warn 合并摘要；error 默认即时发送
  - 支持交易日/工作日/免打扰时间；最高风险是否绕过免打扰由用户明确设置
  - 消息只含必要标的、状态、跳转 ID，不推送数据库连接/Token/完整策略配置/技术堆栈
  - 第三方内容长度超限时自动摘要，保留站内完整详情
  - 所有模板变量转义，防止 Markdown/Webhook 注入
  - 验证：API/日志/导出/前端状态中均不出现完整 Token、Webhook 签名 Secret、SMTP 密码
- [x] WP-MSG.6 设置新增"消息管理"
  - 修改 `frontend/src/components/Settings.tsx`：新增"消息管理"分区，包含渠道配置、推送策略、消息模板、发送记录四个页签
  - 新增 `frontend/src/components/notifications/`（ChannelConfig.tsx、PolicyEditor.tsx、TemplateEditor.tsx、DeliveryLog.tsx）
  - 验证：用户可同时选择多个消息来源和多个渠道
- [x] WP-MSG.7 消息事件接入
  - 将 AlertEvent、任务完成、成交、数据过期、候选新发现、观察信号满足、自动交易阻断、回撤预警等业务事件逐步接入 Outbox
  - 现有 `AlertEvent` 继续作为正式告警事实，不把每个第三方发送结果塞进 `data_json`
  - 验证：业务事件触发后能匹配策略并写入 Outbox
  - 已接入：AlertEvent（`app/services/alerts.py::_fire_event`）、候选新发现（`app/services/candidate_promote.py::sync_scan_results_to_candidates`）、成交（`app/services/sim_accounts.py::place_sim_order`）、自动交易阻断（`app/services/auto_trade_task.py::run_auto_trade`）
  - 待后续接入：任务完成（task scheduler 使用 UUID 字符串 ID，与 emit_task_complete 的 int task_id 签名不匹配，需独立改造）、数据过期（`_eval_data_stale` 已通过 AlertEvent 路径接入，独立的 data_expired 事件待后续接入）、观察信号满足（业务点暂未实现，待 signal 模块上线后接入）、回撤预警（`portfolio_performance.py` 仅计算 max_drawdown 指标，无超阈值触发点，待回撤预警模块上线后接入）
- [x] WP-MSG.8 WP-MSG 测试
  - 新增 `tests/test_whitebox_notification_channels.py`（渠道配置/测试/掩码）
  - 新增 `tests/test_whitebox_notification_outbox.py`（幂等/重试/暂停/dead-letter）
  - 新增 `tests/test_whitebox_notification_dispatcher.py`（重启恢复/故障隔离）
  - 新增 `frontend/src/components/notifications/__tests__/`
  - 验证：见 WP-MSG 验收清单

## Phase 4：流转、成员与标的研究

### Task WP3: 统一状态流转与审计

- [x] WP3.1 新增 `opportunity_transition_events` 审计表
  - 新增 `app/models/opportunity_transition_event.py`：`id`、`symbol_id`、`event_type`、`source_type/source_id`、`target_type/target_id`、`from_status/to_status`、`reason_json`、`idempotency_key` 唯一索引、`actor_type`（user/system/migration）、`created_at`
  - 同步 Alembic 迁移与 `init_db.py` 兼容补丁
  - 验证：双库迁移可重复执行
- [x] WP3.2 新增 `opportunity_transitions` 领域服务
  - 新增 `app/services/opportunity_transitions.py`
  - 操作：候选加入观察池、候选直接加入组合、观察项加入组合、候选/观察项排除/恢复/过期、组合成员归档后回到观察状态
  - 单事务写入业务对象和关联状态
  - 幂等键防双击/重试/网络超时产生重复关系
  - 来源快照和操作者类型
  - 明确旧状态、新状态、原因、时间
  - 失败回滚，不允许出现"候选已晋升但观察项没写成功"的半状态
  - 验证：双击幂等；任一步骤异常时所有写入回滚
- [x] WP3.3 候选状态兼容
  - 第一阶段继续保留 `discovery_candidates.is_promoted`，由统一服务同步更新
  - 完成双轨核对后前端改读统一 `status`，旧字段至少保留一个发布周期
  - 验证：历史 `is_promoted` 候选仍可正常显示
- [x] WP3.4 WP3 测试
  - 新增 `tests/test_whitebox_opportunity_transitions.py`（幂等/原子/审计）
  - 验证：每个观察项和组合成员都能查看来源链

### Task WP4: 组合成员模型

- [x] WP4.1 新增 `portfolio_members` 表
  - 新增 `app/models/portfolio_member.py`：`id`、`portfolio_id`、`symbol_id`、`status`（active/paused/archived）、`execution_mode`（manual/confirm/auto）、`source_type/source_id`、`entry_rule_version_id`、`exit_rule_version_id`、`effective_from`、`effective_to`、`manual_lock`、`priority`、`note`、`created_at`、`updated_at`
  - 约束：同一组合同一标的只能存在一条当前有效成员关系（部分唯一索引：`portfolio_id + symbol_id` WHERE `effective_to IS NULL`）
  - 同步 Alembic 迁移与 `init_db.py` 兼容补丁
  - 验证：双库迁移可重复执行
- [x] WP4.2 新增 `portfolio_members` 服务
  - 新增 `app/services/portfolio_members.py`
  - CRUD：创建、查询、更新、归档（默认归档不物理删除，设置 `effective_to`）
  - 已持仓成员即使暂停买入也必须允许卖出规则继续风控退出
  - 删除成员默认归档，存在持仓时提示选择"仅停止买入"或先卖出
  - 验证：成员归档不会误删持仓
- [x] WP4.3 持仓回填
  - 回填脚本：对每条现有 `positions` 创建成员，`source_type=legacy_position`
  - `effective_from` 优先取 `Position.opened_at`，缺失时取迁移时间并标记日期不确定
  - 不改变 Position ID、数量、成本、最新价、持仓比例
  - 无持仓组合不凭最新扫描结果自动创建成员
  - 回填脚本可重复运行，使用组合+标的幂等检查
  - 验证：现有 3 条持仓回填后对应 3 个有效成员，现金、持仓、订单数量完全不变
- [x] WP4.4 新增成员 API
  - 扩展 `app/api/routes/portfolios.py` 或新增独立路由
  - `GET /api/v1/portfolios/{id}/members`
  - `POST /api/v1/portfolios/{id}/members`
  - `PATCH /api/v1/portfolios/{id}/members/{member_id}`
  - `POST /api/v1/portfolios/{id}/members/{member_id}/archive`
  - 新增 `app/schemas/portfolio_member.py`
  - 验证：API 返回完整字段
- [x] WP4.5 前端"组合成员"页签
  - 新增 `frontend/src/components/PortfolioMembersPanel.tsx`
  - 分别显示成员状态、是否持仓、执行模式、来源、最近信号
  - 验证：无持仓成员可以存在且账户权益不变化
- [x] WP4.6 WP4 测试
  - 新增 `tests/test_whitebox_portfolio_members.py`
  - 扩展 `tests/test_whitebox_portfolio_crud.py`
  - 验证：见 WP4 验收清单

### Task WP5: 标的研究收口与组件拆分

- [ ] WP5.1 拆分 `InvestmentCenter.tsx` 为 9 个子组件
  - `SymbolResearchShell`（壳层，接收来源上下文参数）
  - `SymbolSearchHeader`（搜索+快捷标的+搜索历史）
  - `SymbolRelationshipBar`（候选/观察/组合成员/持仓/告警关联状态）
  - `FactorExplanationPanel`（因子解释+模型 ID+因子贡献+数据截止）
  - `SymbolAlertSummary`（告警摘要，正式提醒归告警中心）
  - `RiskReferencePanel`（研究情景参数，正式风控读 PortfolioRule）
  - `TradePlanPanel`（交易计划刷新+参数覆盖+情景分析+分批计划+未来买入计划）
  - `SymbolChartPanel`（K 线+MA10/MA20+MACD+RSI+BOLL+绘图 Hook 统一）
  - `SingleSymbolBacktestPanel`（事件驱动回测+规则模板+成本+结果详情+结果应用到组合）
  - 验证：组件拆分后现有单股回测、未来计划、移动端布局、详情绘图不丢失
- [ ] WP5.2 状态迁移
  - 搜索历史可继续保留本地（体验状态）
  - 收藏改读写后端观察池；本地 `ic_favorites` 进入只读回退期
  - 正式价格/评分/公式提醒通过 `alert_rules` 创建；页面内即时计算只作为"当前提示"并标注未持久化
  - `ic_risk_settings` 仅作研究情景参数，不再冒充组合风控
  - 模拟下单按钮跳转到指定组合交易上下文，不在研究页复制账户逻辑
  - 单股回测继续留在研究页，但结果保存来源上下文
  - 验证：收藏/正式提醒/组合风控/下单不再存在两套业务真相
- [ ] WP5.3 统一来源上下文
  - 统一参数对象：`symbol_id`、`source_type`（candidate|observation|portfolio_member|position|alert|backtest|manual）、`source_id`、`portfolio_id`（可选）、`return_to`
  - 候选、观察、组合、告警、回测使用同一研究壳层
  - 返回时保留原筛选和滚动位置
  - 验证：同一标的从不同来源进入时研究数据一致
- [ ] WP5.4 原 InvestmentCenter 兼容入口
  - 保留兼容入口仍能打开研究壳层
  - 验证：旧链接可访问
- [ ] WP5.5 WP5 测试
  - 新增 `frontend/src/components/__tests__/SymbolResearchShell.test.tsx`
  - 新增 `frontend/src/components/__tests__/SymbolRelationshipBar.test.tsx`
  - 验证：见 WP5 验收清单

## Phase 5：AI 助手

### Task WP-AI: 量化助手

- [ ] WP-AI.1 新增 AI 会话与审计数据模型
  - 新增 `app/models/ai_session.py`：`ai_sessions`（会话标题/来源页面/提供商/模型）、`ai_messages`（角色/内容/上下文摘要/Token/耗时）、`ai_action_audits`（建议动作/预览/用户确认/最终结果）
  - 同步 Alembic 迁移与 `init_db.py` 兼容补丁
  - 验证：双库迁移可重复执行
- [ ] WP-AI.2 多 Profile 主备降级
  - 把单一 `ai_config.json` 兼容迁移为多个 AI Profile
  - 每个 Profile 保存提供商、地址、模型、鉴权、超时、用途、启停状态
  - 主模型超时/限流切备用模型或本地 Ollama，切换在回复中显示
  - AI 失败不阻塞扫描/回测/告警/交易
  - 相同解释请求按数据版本短期缓存
  - 限制单次上下文、最大输出、每日请求量、并发
  - AI 与消息渠道共用统一 Secret Store（环境变量/系统凭据/主密钥加密）
  - 验证：日志/API 响应/导出永不返回明文 Secret
- [ ] WP-AI.3 受控上下文包
  - 后端按页面构造受控上下文包：用户问题+页面来源、标的/候选/观察/组合/任务 ID、数据截止/来源/可信度/缺失项、评分配置/模型版本/因子贡献、当前能力门禁/允许下一步、相关行情/回测/绩效摘要
  - 限制大小，去除 API Key/Webhook/邮箱密码
  - 新闻和第三方文本标记为"不可信数据内容"
  - AI 回复附"数据截至、模型/规则版本、依据对象"
  - 验证：上下文包不含敏感信息
- [ ] WP-AI.4 第一阶段只读工具集
  - 新增 `app/services/ai/tools/`：`get_capabilities`、`get_data_health`、`get_task_status`、`get_symbol_research`、`get_candidate_explanation`、`get_portfolio_summary`、`get_backtest_explanation`
  - AI 不得自行查询任意数据库或调用第三方接口
  - 验证：AI 能正确解释至少五类当前对象
- [ ] WP-AI.5 第二阶段草稿工具
  - 生成自定义指标草稿、筛选方案草稿、告警规则草稿、观察备注/标签草稿、复盘草稿、模拟订单草稿
  - 三步流程：AI 建议 → 系统规则校验和变更预览 → 用户明确确认后由普通业务 API 执行
  - AI 没有绕过确认调用写接口的权限
  - 模拟订单即使由 AI 起草也必须重新经过现金/手数/T+1/涨跌停/数据健康/组合风控校验
  - 自动交易永远由策略规则和调度器负责，不由对话直接触发
  - 验证：不确认时不产生任何数据库变化
- [ ] WP-AI.6 结构化输出与审计
  - AI 响应统一包含：`answer`、`evidence`、`warnings`、`suggested_actions`、可选 `draft`
  - 会话保留期可配置，用户可删除
  - 审计记录只保存必要上下文摘要，不重复存完整 K 线和敏感配置
  - 验证：缺数据时明确说不知道
- [ ] WP-AI.7 前端入口
  - 全局助手：每个主要页面可打开，自动携带当前上下文
  - 页面动作：候选、标的研究、任务、组合、回测提供"让 AI 解释"
  - 设置 → AI 助手：Profile 管理、连接测试、模型发现、主备优先级、用量、健康状态
  - 结果以解释卡、证据列表、操作草稿展示
  - AI 未配置时显示用途和配置入口，不进入请求失败
  - 验证：AI 无法连接/超时/限流/格式异常时用户看到可理解错误且核心功能正常
- [ ] WP-AI.8 WP-AI 测试
  - 新增 `tests/test_whitebox_ai_context.py`（上下文包大小/敏感信息脱敏）
  - 新增 `tests/test_whitebox_ai_tools.py`（只读工具返回结构）
  - 新增 `tests/test_whitebox_ai_drafts.py`（草稿三步确认流程）
  - 新增 `tests/test_whitebox_ai_failover.py`（主备降级）
  - 验证：见 WP-AI 验收清单

## Phase 6：交易、回测与归因

### Task WP6: 自动交易成员化与安全切换

- [x] WP6.1 扩展 `SimOrder` 归因字段
  - 修改 `app/models/sim_account.py`：新增 `member_id`、`source_type/source_id`、`signal_id` 或信号快照、`rule_version_id`、`execution_mode`、`client_order_key` 唯一索引、`decision_snapshot_json`、`rejection_code/rejection_detail`
  - 同步 Alembic 迁移与 `init_db.py` 兼容补丁
  - 验证：双库迁移可重复执行
- [ ] WP6.2 实现新执行逻辑
  - 修改 `app/services/auto_trade_task.py`
  - 买入候选：`active PortfolioMember AND execution_mode=auto AND 当前无持仓 AND 最新有效信号允许买入 AND 数据健康通过 AND 组合风控通过`
  - 卖出侧覆盖所有当前持仓，即使成员暂停或归档，不因成员状态漏掉止损/退出信号
  - `confirm` 模式只生成待确认订单计划；`manual` 模式只提示信号不下单
  - 冲突优先级：手动锁定 → 组合级风险强制减仓/清仓 → 自动卖出规则 → 自动买入规则 → 普通信号建议
  - 验证：三种执行模式结果互不混淆
- [ ] WP6.3 幂等订单
  - `client_order_key`（组合+成员+信号日期+方向+规则版本）唯一索引
  - 调度重跑不重复下单
  - 验证：同一任务重复执行不重复下单
- [ ] WP6.4 双跑切换
  - `AUTO_TRADE_MEMBER_SOURCE_ENABLED=false`：旧来源实际执行，新来源仅 Dry Run
  - 连续至少 5 个交易日或 3 次有效运行保存旧/新买卖集合差异
  - 对每个差异给出原因：成员缺失、状态暂停、信号不同、数据过期、风控阻断
  - 差异经人工确认后先对一个非默认测试组合开启新来源
  - 再逐组合切换；开关关闭可立即回退
  - 旧来源至少保留一个发布周期
  - 验证：关闭新来源开关后恢复当前 `Score.action + latest executable scan` 逻辑
- [ ] WP6.5 安全门禁
  - K 线、评分或规则版本过期时 fail-closed 禁止买入
  - 卖出风控不得因一般数据缺失静默跳过，应生成高优先级告警
  - 自动交易任务取消时停止后续组合和后续订单
  - 每笔失败独立记录，不回滚已合法成交的其他标的，但形成任务错误摘要
  - 验证：所有新订单可追溯到成员、信号、规则、数据截止时间
- [ ] WP6.6 前端 AutoTradePanel 改造
  - 修改 `frontend/src/components/AutoTradePanel.tsx`：成员级执行状态、dry-run 差异对比、双跑切换 UI
  - 验证：双跑差异可视化
- [ ] WP6.7 WP6 测试
  - 新增 `tests/test_whitebox_auto_trade_member_source.py`
  - 扩展 `tests/test_whitebox_auto_trade.py`
  - 验证：见 WP6 验收清单

### Task WP7: 组合回测成员化与历史可复现

- [ ] WP7.1 实现按有效日期读取成员
  - 修改 `app/services/portfolio_backtest.py`
  - 标的来源：`effective_from <= trade_date AND (effective_to IS NULL OR effective_to >= trade_date) AND status snapshot allows evaluation`
  - 验证：未来才加入的成员不会出现在过去日期
- [ ] WP7.2 回测快照
  - 每次运行至少保存：成员 ID、标的 ID、成员有效日期、执行模式快照、买卖规则版本、组合风控版本、成本配置、评分模式、因子模型运行 ID、数据截止时间、引擎名称和版本、运行时排除标的及原因
  - 历史回测即使成员/规则/模型改变仍按原快照可读
  - 扩展 `app/models/backtest.py` 增加快照字段
  - 同步 Alembic 迁移与 `init_db.py` 兼容补丁
  - 验证：同一快照重复运行得到一致标的集、规则、成本
- [ ] WP7.3 兼容与切换
  - `PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED=false` 时继续运行旧推导逻辑
  - UI 明确显示本次使用"旧临时标的集"还是"历史成员集"
  - 新旧引擎使用相同日期、资金、成本后做对比；差异必须可解释
  - VectorBT 可用于快速研究对比，但正式回测结果仍以事件驱动引擎为权威
  - 完整组合回测前提：全部有效成员均为 `auto` 且规则有效；存在 manual/confirm 成员时默认禁止完整回测，提供"仅回测自动成员"选项及排除清单
  - 验证：切回旧开关后原 22 条历史回测仍可查看
- [ ] WP7.4 前端 PortfolioBacktestPanel 改造
  - 修改 `frontend/src/components/PortfolioBacktestPanel.tsx`：成员资格校验、组合快照展示、新旧来源说明
  - 验证：UI 明确显示使用来源
- [ ] WP7.5 WP7 测试
  - 新增 `tests/test_whitebox_portfolio_backtest_membership.py`
  - 扩展 `tests/test_whitebox_portfolio_backtest.py`
  - 验证：见 WP7 验收清单

### Task WP8: 绩效归因、复盘和跨模块联动

- [ ] WP8.1 扩展绩效归因维度
  - 修改 `app/services/metrics.py`、`app/services/portfolio_performance.py`
  - 按组合成员贡献、按手动/确认/自动执行模式贡献、按候选来源或观察标签贡献、按规则版本/信号类型/退出原因贡献、回测与模拟账户同期偏差、成本/滑点/未成交/风控阻断影响
  - 验证：用户能解释一个组合收益来自哪些成员和执行模式
- [ ] WP8.2 联动
  - 订单和成交打开对应成员与信号
  - 绩效异常可一键创建复盘记录
  - 告警打开对应观察项、成员或持仓上下文
  - 今日决策显示待确认订单、数据门禁阻断、成员失效待办
  - 组合回测结果可回到成员列表，并标记使用的成员快照
  - 验证：用户能解释一笔交易"为何进入、谁触发、用哪套规则、成本多少、结果如何"
- [ ] WP8.3 前端绩效面板改造
  - 修改 `frontend/src/components/PortfolioPerformancePanel.tsx`：归因维度展示、样本数提示、基准对比
  - 绩效样本不足时显示样本数和限制，不展示具有误导性的稳定结论
  - 验证：样本不足提示正确
- [ ] WP8.4 WP8 测试
  - 扩展 `tests/test_whitebox_portfolio_performance.py`
  - 验证：见 WP8 验收清单

## Phase 7：旧入口清理

### Task WP9: 旧入口与重复状态清理

- [ ] WP9.1 移除旧投资中心一级入口
  - 从一级导航移除旧投资中心入口
  - 保留兼容路由跳转到标的研究
  - 验证：旧链接可访问
- [ ] WP9.2 停止写入 `ic_favorites`
  - 保留最后一次恢复/导入工具后再删除读取逻辑
  - 验证：本地收藏不再写入
- [ ] WP9.3 停止前端即时提醒称为正式告警
  - 页面内即时计算明确标注"未持久化"
  - 验证：UI 文案准确
- [ ] WP9.4 移除组合工作台中的机会/观察重复区块
  - 改为链接机会中心
  - 验证：组合工作台不再重复呈现机会/观察
- [ ] WP9.5 停止自动交易和组合回测读取最新扫描作为默认来源
  - 仅在 WP6/WP7 完成双轨切换并验收后执行
  - 验证：默认来源切换为成员
- [ ] WP9.6 旧 API/字段进入废弃期
  - 记录访问日志，确认无调用后才允许在未来版本删除
  - 验证：访问日志可查
- [ ] WP9.7 历史数据保留核对
  - 不删除历史候选、观察、组合、持仓、订单、成交、回测、净值快照、告警事件
  - 验证：与基线对账一致

## Phase 8：黑盒主链路与发布对账

### Task Final: 黑盒主链路验证与发布对账

- [ ] Final.1 黑盒主链路
  - 运行扫描 → 查看候选来源与数据日期 → 加入观察池 → 修改观察标签/原因 → 加入指定组合 → 确认只是成员而非持仓 → 运行单股回测 → 生成待确认订单 → 手动确认模拟成交 → 查看现金/持仓/净值/归因 → 创建复盘
  - 验证：链路完整可走通
- [ ] Final.2 自动模式专项测试
  - 重复调度、任务取消、数据过期、规则失效、涨跌停、T+1、现金不足、部分标的失败
  - 验证：每种异常都有合理处理
- [ ] Final.3 数据迁移对账（按 34 章）
  - `watchlists`/`watchlist_items`（原 6 项必须存在，仅允许新增导入项）/`portfolios`/`positions`/`portfolio_members`（至少覆盖全部现有持仓）/`cash_ledger`/`sim_orders`/`sim_trades`/`backtest_runs`/`scheduled_tasks`/`alert_rules`/`alert_events`
  - 任何现金/持仓/订单/成交对账不一致属阻断发布问题
  - 验证：发布前后对账一致
- [ ] Final.4 横向能力发布门槛核对（按 42 章）
  - WP-S：核心页面本地命中优先、接口失败可降级、任务不永久卡住
  - 前置条件：用户不能在条件缺失时误入下游
  - WP-P：ready 快照下 A 股/ETF 快速扫描 P95 ≤ 5 分钟
  - WP-AI：至少完成系统引导/数据诊断/候选解释/公式/任务诊断；AI 不直接写业务状态
  - WP-MSG：站内与已配置外部渠道具备策略/Outbox/重试/发送审计
  - 验证：5 项门槛全部通过

# Task Dependencies

- `WP0` 无依赖，必须最先完成
- `WP-S` 依赖 `WP0`；是所有新页面和外部集成的稳定性门槛
- `WP-P` 依赖 `WP-S`；是机会中心正式上线的性能门槛
- `WP1` 依赖 `WP0`；正式上线依赖 `WP-S`、`WP-P`
- `WP2` 依赖 `WP0`、`WP1`
- `WP-MSG` 依赖 `WP-S`、现有告警中心
- `WP3` 依赖 `WP2`
- `WP4` 依赖 `WP0`、`WP3`
- `WP5` 依赖 `WP1`、`WP2`；可与 `WP3`/`WP4` 后端开发期间并行推进组件拆分，但不得提前删除投资中心入口或本地状态迁移逻辑
- `WP-AI` 依赖 `WP-S`、`WP1`；组合能力依赖 `WP5`/`WP8`
- `WP6` 依赖 `WP4`、`WP5`
- `WP7` 依赖 `WP4`
- `WP8` 依赖 `WP6`、`WP7`
- `WP9` 依赖 `WP1`~`WP8`、`WP-AI`、`WP-MSG` 全部验收
- `Final` 依赖所有 WP 完成

依赖关系图：

```text
WP0 -> WP-S -> WP-P -------------------------┐
  |       ├-> WP-MSG                         │
  |       └-> WP-AI <------ WP5 / WP8        │
  └-> WP1 -> WP2 -> WP3 -> WP4               │
                   \          | \             │
                    -> WP5 ---|  -> WP7       │
                              -> WP6 -> WP8 -> WP9 -> Final
                                    \------/
```

## Phase 1.5：WP1 验证修复任务

### Task WP1-FIX: 统一徽标在今日决策/候选列表/组合页接入（WP1 验证 PARTIAL 项）

> 来源：WP1 信息架构壳层 checklist 验证（2026-07-20）发现 `OpportunityStatusBadges` 仅在 `ObservationPool.tsx` 内接入，今日决策、候选列表、组合页均未接入；徽标也未实现"打开现有详情弹窗"的交互。原 checklist 第 8 项已由 `[x]` 改为 `[ ]` 并标注 PARTIAL。

- [x] WP1-FIX.1 在今日决策页接入 `OpportunityStatusBadges`
  - 定位：`frontend/src/components/DecisionView.tsx`（或今日决策实际渲染组件，按 `App.tsx` 中 `activeTab === "decision"` 路由定位）
  - 在每日决策条目旁渲染 `<OpportunityStatusBadges symbolId={row.symbol_id} compact />`，紧凑模式以避免占用过多空间
  - 验证：今日决策列表中每条标的都能看到关联状态徽标，接口失败时降级"状态未知"
- [x] WP1-FIX.2 在候选列表接入 `OpportunityStatusBadges`
  - 定位：`frontend/src/components/Discovery.tsx`（`CandidatePool.tsx` 直接复用 Discovery）
  - 在候选结果表行尾新增"关联状态"列，渲染 `<OpportunityStatusBadges symbolId={row.symbol_id} />`
  - 验证：候选列表每行能看到是否已加入观察池/组合/持仓/告警
- [x] WP1-FIX.3 在组合页接入 `OpportunityStatusBadges`
  - 定位：`frontend/src/components/PortfolioWorkbench.tsx`、`frontend/src/components/Trading.tsx`
  - 在持仓表/组合成员表行尾新增"关联状态"列，渲染 `<OpportunityStatusBadges symbolId={row.symbol_id} />`
  - 验证：组合页能看到持仓的候选/观察/告警关联状态，但不会把持仓显示成观察项
- [ ] WP1-FIX.4 徽标点击打开现有详情弹窗
  - 为 `OpportunityStatusBadges` 增加可选 `onOpenDetail?: (symbolId: number) => void` prop
  - 点击徽标时调用 `onOpenDetail`，由宿主页面打开现有标的详情弹窗（`InvestmentCenter` 的 `SymbolDetailModal` 或 `OpportunityCenter` 的统一详情面板）
  - 验证：徽标可点击，点击后能打开详情弹窗；不破坏现有详情弹窗交互
- [x] WP1-FIX.5 补充定向测试
  - 在 `frontend/src/components/__tests__/` 下新增 `TodayDecision.badges.test.tsx`、`Discovery.badges.test.tsx`、`PortfolioWorkbench.badges.test.tsx`
    - 说明：WP1-FIX.1 实际接入位置为 `TodayDecision.tsx`（非 `DecisionView.tsx`），故测试文件命名为 `TodayDecision.badges.test.tsx`
  - 覆盖：徽标正确渲染、降级显示"状态未知"、点击徽标触发 `onOpenDetail`
  - 验证：定向测试通过（5+4+5=14 个用例）；现有 `OpportunityCenter.test.tsx`（9 个）、`OpportunityStatusBadges.test.tsx`（14 个）仍通过
- [x] WP1-FIX.6 复核 checklist 第 8 项
  - 全部子项完成后，将 `checklist.md` WP1 段第 8 项从 `[ ]` 改回 `[x]`，移除 PARTIAL 注释
  - 验证：checklist 勾选状态与代码实际状态一致

## Phase 4.5：WP4 验证修复任务

### Task WP4-FIX: PortfolioMembersPanel "最近信号"列渲染（WP4 验证 PARTIAL 项）

> 来源：WP4 组合成员模型 checklist 验证（2026-07-20）发现 `PortfolioMembersPanel.tsx` 5 项显示要求中只完成了 4 项（状态/是否持仓/执行模式/来源），"最近信号"列未渲染。前端类型已预留 `latest_signal?: string | null`（`frontend/src/components/PortfolioMembersPanel.tsx:69`），但后端 `app/schemas/portfolio_member.py` 的 `PortfolioMemberRead` 未包含该字段，且 `PortfolioMember` 模型本身不持有信号数据。"最近信号"需要联表查询（依赖 WP6 扩展 `SimOrder.signal_id` 后才能从订单/信号表反查成员最近一次信号）。原 checklist 第 15 项已由 `[x]` 改为 `[ ]` 并标注 PARTIAL。

- [ ] WP4-FIX.1 后端 `PortfolioMemberRead` 扩展 `latest_signal` 字段
  - 依赖：WP6.1 扩展 `SimOrder` 归因字段（`member_id`、`signal_id`、`rule_version_id` 等）完成后才能实现
  - 在 `app/schemas/portfolio_member.py` 的 `PortfolioMemberRead` 新增 `latest_signal: str | None = None`、可选 `latest_signal_at: str | None = None`、可选 `latest_signal_action: str | None = None`（买入/卖出/持有）
  - 在 `app/api/routes/portfolios.py` 的 `list_portfolio_members` 中按 member_id 联表查询最近一条 `SimOrder` 或信号记录，填充 `latest_signal` 字段
  - 验证：API 返回的成员对象包含 `latest_signal` 字段；无信号的成员返回 `null`
- [ ] WP4-FIX.2 前端 `PortfolioMembersPanel.tsx` 渲染"最近信号"列
  - 在 `columns` 数组中新增"最近信号"列（建议放在"来源"列之后），渲染 `record.latest_signal`（如"买入 2026-07-19"或"-"）
  - 可选：用 `Tag` 颜色区分买入（green）/卖出（red）/持有（default）
  - 验证：表格显示 5 项信息（状态/是否持仓/执行模式/来源/最近信号）；无信号时显示"-"
- [ ] WP4-FIX.3 复核 checklist 第 15 项
  - 全部子项完成后，将 `checklist.md` WP4 段第 15 项从 `[ ]` 改回 `[x]`，移除 PARTIAL 注释
  - 验证：checklist 勾选状态与代码实际状态一致

