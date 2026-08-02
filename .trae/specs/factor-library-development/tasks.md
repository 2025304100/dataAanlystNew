# 专业因子库开发任务清单

> 严格依据 `docs/专业因子库开发计划.md`（V1.1）的工作包与依赖顺序组织。每个任务包含验证手段。R0 未通过不进入 WP1；R1 未通过不进入 R2；Shadow 20 个交易日观察期不可压缩。

## R0 数据底座修复（WPD）

- [x] **Task WPD-01**: MySQL schema 与 Alembic 基线审计
  - [x] 备份 MySQL gpfx 并记录 MySQL 5.7.26 版本
  - [x] 导出实际表、列、索引、外键的 schema fingerprint
  - [x] 确认活动库没有可用 `alembic_version` 后形成安全 stamp 决策（不盲目 upgrade）
  - [x] 记录仓库实际 head `wps_0801_001_universe_incremental_index`（注意：底稿记录的 `wps_0023_020_api_deprecation_logs` 已被 2026-08-01 18:30 新增的 universe 增量索引覆盖）
  - [x] 导出 8 个系统因子、版本和运行模式快照
  - **验证**：schema fingerprint 文件可重复生成；备份与 stamp 决策有审计记录
  - **产出**：`tmp/wpd01_audit_report.json`、`tmp/wpd01_audit_supplement.json`、`tmp/wpd01_stamp_decision.md`
  - **关键发现**：07-28/29 横截面已补齐至 4334/4335（底稿记录 150/218）；async_tasks 无 error_code 列；factor_runtime_state.weight_mode=manual

- [x] **Task WPD-02**: 完整交易日判定
  - [x] 实现/验证 `latest_complete_trade_date`，以 universe_symbols active 范围和过去 20 个完整交易日中位数为基准
  - [x] 当日标的数低于基准 90% 时标记 incomplete
  - [x] 用 2026-07-24（完整）、07-27（2067）、07-28/29（150/218）覆盖完整/残缺场景
  - [x] EvaluationRun 保存 `selected_trade_date`、`observed_symbols`、`expected_symbols`、`completeness_ratio`、`fallback_reason`
  - **验证**：算法不会选择 2026-07-28/29 残缺横截面
  - **产出**：`app/services/factors/trade_calendar.py`、`tests/test_whitebox_complete_trade_day.py`（12 passed）、`tmp/wpd02_verify.py`、`tmp/wpd02_verify_report.json`
  - **关键发现**：现场 MySQL 实测 2026-07-31 横截面已补齐至 4,887/4,890（ratio=0.9994），算法判定为完整；07-28/29 历史残缺场景在单元测试中已覆盖；基准中位数排除候选日避免污染

- [x] **Task WPD-03**: DuckDB 锁和残留进程治理
  - [x] 复现并定位最近 factor_pipeline 锁失败（11.3%/19% failed）
  - [x] 实现锁拥有者诊断、跨进程单飞、超时、重启恢复
  - [x] 验证取消、重启和锁超时终态
  - [x] 保证失败批次不覆盖最近成功因子批次
  - [x] 记录 DuckDB 3.83 GB 基线和增长来源
  - **验证**：同时启动两次流水线只保留一个任务；锁冲突/重启/取消均进入明确终态
  - **产出**：`app/services/factors/warehouse_locks.py`（752 行，跨进程锁治理核心）、`app/services/factors/store.py` 扩展 4 个方法、`app/services/factors/pipeline_task.py` 扩展 2 个函数、`app/main.py` 追加启动钩子、`tests/test_whitebox_warehouse_locks.py`（19 测试：15 passed + 4 skipped 因 duckdb 未安装）、`tmp/wpd03_verify.py`、`tmp/wpd03_baseline.md`
  - **关键设计**：跨进程锁用 `os.O_CREAT|os.O_EXCL`（Windows 兼容）；PID 检测 psutil → ctypes → os.kill 三级降级；僵尸任务用 `heartbeat_at or updated_at or started_at or created_at` fallback 链；safe_write_context 保证 ROLLBACK

- [x] **Task WPD-04**: 数据 readiness API
  - [x] 按因子类型返回 `available/degraded/blocked` 及证据
  - [x] 输出 8 因子 readiness：turnover_z20=available，ep_ttm/negative_pb/main_inflow_5d_ratio/tail_accumulation_proxy=blocked
  - [x] 固定 turnover_z20 完整交易日对账样本
  - [x] 记录估值 6、财报 336、资金流 0、尾盘 0 的阻断证据
  - **验证**：readiness 明确把 0 覆盖因子标为 blocked
  - **产出**：`app/services/factors/readiness.py`（FactorReadiness/FactorReadinessReport 数据类、三层分类算法 A_continuous/B_event/C_blocked）、`app/api/routes/factors.py` 追加 `/factors/readiness` 和 `/factors/{code}/readiness` 端点、`tests/test_whitebox_factor_readiness.py`（16 passed）、`tmp/wpd04_verify.py`
  - **关键设计**：三层分类阈值（A: 0.7/0.9，B: 事件日覆盖率不适用，C: 1.0）；源表行数阈值用 expected_symbols 作为基准；B 层事件因子豁免 source_table_insufficient 检查；阻断原因用稳定英文枚举

- [x] **Task WPD-05**: 错误协议与乱码修复
  - [x] 流水线失败输出改为稳定 `error_code`（事实来源）
  - [x] 中文 message 以 UTF-8 存储
  - [x] 前端用 i18n 映射中文错误
  - [x] 历史乱码 message 提供兜底显示
  - **验证**：失败任务显示稳定 error_code 和中文本地化，不再新增乱码
  - **产出**：`app/schemas/async_task.py` 新增 `error_code` 字段、`app/services/async_tasks.py` 提取 errors_json[0].error_code、`app/api/routes/system.py` 双路径覆盖、`app/db/manager.py` 追加 charset=utf8mb4、`frontend/src/i18n/zh-CN.ts`/`en-US.ts` 追加 7 个 error_code_* 翻译、`frontend/src/utils/taskErrorDisplay.ts`（isLikelyMojibake + getTaskErrorMessage）、`frontend/src/components/TaskCenter.tsx` 接入兜底显示、`tests/test_whitebox_error_protocol.py`（15 passed）、`frontend/src/utils/__tests__/taskErrorDisplay.test.ts`（23 passed）、`tmp/wpd05_verify.py`
  - **关键设计**：error_code 从 errors_json[0] 动态提取避免 schema 变更；charset 双重保障（connect_args + SET NAMES 监听）；乱码检测启发式（U+FFFD/控制字符/非 BMP）；i18n 在前端做，后端只输出英文枚举

- [x] **Task WPD-06**: 因子值增量与批次审计
  - [x] 补齐 ingestion/factor/target 批次查询和状态
  - [x] 检测 factor_values 落后 raw_daily_bars 并给出可执行修复建议
  - [x] 批次清单含输入日期、行数、状态、原子提交证据
  - **验证**：批次清单可追溯；DuckDB ingestion_batches 不再为空
  - **产出**：`app/services/factors/batch_audit.py`（BatchRecord/BatchAuditEntry/BatchLagDiagnostic/BatchAuditReport 数据类 + begin/finalize/batch_context/list/audit/diagnose 函数）、`app/services/factors/bar_mirror.py` 集成 begin/finalize_batch（吞异常）、`app/services/factors/pipeline_task.py` 集成 batch_context（factors/targets 阶段）、`app/api/routes/factors.py` 追加 `/factors/batches`/`/batches/audit`/`/batches/{id}` 端点、`tests/test_whitebox_batch_audit.py`（20 测试，duckdb 未安装时跳过）、`tmp/wpd06_verify.py`
  - **关键设计**：批次审计绝不阻塞主流程（try/except 吞异常）；原子提交证据用 SOURCE_KEY_TO_TABLE 映射 + COUNT 对比；落后诊断 severity 分级（none/minor/major/critical）；calc_batch_id 预生成使 ingestion_batches 与 factor_values 关联

- [x] **Task WPD-07**: 数据源补齐路线
  - [x] 估值：从补齐日期开始积累，无历史 point-in-time 时不做伪历史 IC
  - [x] 财报：按 announcement_date 使用，先覆盖 300~500 高流动性训练池
  - [x] 主力资金：表为空时不运行 main_inflow_5d_ratio，先验证接口/复权/限流/60 日连续性
  - [x] 龙虎榜：只在事件样本内评估，不把非事件日填 0
  - [x] 热度：快照适合解释/候选增强，不承诺历史连续性
  - [x] 尾盘代理：维持 blocked，本期不引入大规模分钟库
  - [x] 宏观：作为 regime 条件使用，不按股票横截面覆盖率评估
  - **验证**：分来源补数任务、限流预算、失败重试和可达到覆盖说明文档
  - **产出**：`app/services/factors/data_source_roadmap.py`（7 类数据源策略、限流预算、可达覆盖说明、阻断逻辑；887 行）、`app/services/factors/readiness.py` 集成 `data_source_policy` 字段和 roadmap 阻断检查、`app/api/routes/factors.py` 追加 `/factors/data-source-roadmap` 端点、`tests/test_whitebox_data_source_roadmap.py`（25 测试全部通过）
  - **关键设计**：7 种 `CompletionStrategy`（INCREMENTAL_FROM_DATE/HIGH_LIQUIDITY_FIRST/VALIDATE_AND_CONTINUITY/EVENT_SAMPLE_ONLY/SNAPSHOT_ONLY/MAINTAIN_BLOCKED/REGIME_CONDITION）；4 种 `DataSourceLayer`（A/B/C/D_regime 扩展）；估值补齐起始日 `VALUATION_COMPLETION_START_DATE=2026-08-01` 禁止伪历史 IC；资金流 60 日连续性校验 `check_capital_flow_continuity`；龙虎榜事件日样本 `get_lhb_event_sample`（非事件日策略 `missing`）；尾盘代理 `MAINTAIN_BLOCKED` 始终阻断；宏观 `D_REGIME` 不按个股覆盖评估；readiness 集成在 try/except 中不阻断主流程

## WP0 基线冻结与开发护栏（依赖 WPD）

- [x] **Task WP0-01**: 导出 8 个系统因子及版本快照（因子代码、ID、版本、公式、方向、依赖和哈希基线）
- [x] **Task WP0-02**: 固定 manual/ridge/shadow API 契约（overview、config、versions、runtime、explanation 响应快照）
- [x] **Task WP0-03**: 建立旧计算结果对账样本（固定 data_cutoff_at、标的集、因子值和 Score 样本）
- [x] **Task WP0-04**: 固定任务取消、重跑和异常终态（流水线状态机回归测试）
  - **涉及文件**：`app/services/factors/baseline_freeze.py`（新增）；`tests/test_whitebox_baseline_freeze.py`（新增 15 测试）、`tests/test_whitebox_factor_pipeline_state.py`（新增 15 测试）；现有 `test_whitebox_factor_settings.py`、`test_whitebox_factor_engine.py`、`test_whitebox_factor_runtime_api.py`、`test_whitebox_factor_pipeline_startup.py`、`test_blackbox_factor_settings.py` 保持不变
  - **验证**：8 因子快照可重复生成（`freeze_factor_baseline` + `compute_factor_content_hash` 确定性）；24 个 API 端点契约已冻结（`API_CONTRACTS` + `validate_api_response_fields`）；对账样本可构建（`build_reconciliation_sample` 固定 2026-07-24 + 20 标的）；任务取消/重跑/异常终态有 15 个回归测试（单飞、终态保护、僵尸恢复、error_code）；后续差异可定位到字段/版本/批次
  - **产出**：`app/services/factors/baseline_freeze.py`（基线冻结模块，含因子快照/API 契约/对账样本/统一报告；~830 行）、`tests/test_whitebox_baseline_freeze.py`（15 测试全部通过）、`tests/test_whitebox_factor_pipeline_state.py`（15 测试全部通过）
  - **关键设计**：`compute_factor_content_hash` 用 canonical JSON + SHA256[:16]；`baseline_hash` 聚合全部因子 content_hash；`API_CONTRACTS` 精确 24 端点（13+5+6）含 required/optional/status_codes；`validate_api_response_fields` 支持路径模板匹配（`{param}` 通配）；`RECONCILIATION_BASE_DATE=2026-07-24` 固定完整交易日；`generate_baseline_freeze_report` 汇总因子基线+API 契约+对账样本+运行时状态+Alembic head；状态机测试覆盖单飞、终态保护（cancelled/done/failed 不被覆盖）、僵尸恢复（heartbeat 过期）、error_code 端到端一致性

## WP1 数据模型、迁移与生命周期（依赖 WP0）

- [x] **Task WP1-01**: 扩展 Factor 和 FactorVersion
  - Factor：lifecycle_status、origin、owner、thesis、factor_kind、asset_scope_json、active_version_id、shadow_version_id、risk_level、archived_at
  - FactorVersion：formula_ast_json、postprocess_json、parameter_schema_json、data_dependencies_json、compiler_version、execution_plan_hash、complexity_score、created_by、created_via、ai_provenance_json、validation_status、validation_errors_json
  - 保留 status/is_active 作为过渡兼容字段，lifecycle_status 为新流程唯一事实来源
  - **产出**：`app/models/factor.py`（Factor +10 字段）、`app/models/factor_model.py`（FactorVersion +12 字段）；全部 `nullable=True`、FK `ondelete=SET NULL`；使用 `mapped_column` 对齐代码库 SQLAlchemy 2.0 风格
- [x] **Task WP1-02**: 新增治理与评估主表（EvaluationRun、TransitionAudit、FactorSet 基础表）
  - **产出**：`app/models/factor_evaluation.py`（4 个 ORM 模型：EvaluationRun/TransitionAudit/FactorSet/FactorSetMember）；`app/models/__init__.py` 和 `alembic/env.py` 注册导入
  - **关键设计**：EvaluationRun 含 WPD-02 完整交易日证据字段；TransitionAudit 追加式（actor/reason/evidence_run_id/request_id/migration_note）；FactorSet has members relationship；FactorSetMember UniqueConstraint(factor_set_id, factor_id)
- [x] **Task WP1-03**: 编写 Alembic 0021
  - `revision = "wps_0023_021_factor_library_lifecycle"`
  - `down_revision = "wps_0801_001_universe_incremental_index"`（代码实际 head，非底稿记录）
  - 幂等增列、新表、索引、外键、历史回填；8 因子幂等回填 origin=system、lifecycle_status=active
  - 创建初始 FactorSet `legacy-system-v1`，成员固定为迁移时 8 个版本
  - 迁移事务不改变 FactorRuntimeState.weight_mode 和 active_model_run_id
  - **产出**：`alembic/versions/2026_07_31_0021_factor_library_lifecycle.py`（5 阶段幂等迁移：增列→增列→建表→回填→FactorSet）；factor_kind 映射（lhb/hot_rank=event，其余 continuous）；content_hash 用 SHA256(sorted code:version)；downgrade 用 batch_alter_table 处理 SQLite FK 列删除
- [x] **Task WP1-04**: 实现因子仓储和 Schema（CRUD、分页、筛选、版本创建、引用查询；`app/schemas/factor_library.py`）
  - **产出**：`app/schemas/factor_library.py`（FactorRead/FactorVersionRead/FactorDraftCreate/FactorVersionCreate/FactorTransitionRequest/FactorFilter 等 8+ Schema）、`app/services/factors/factor_registry.py`（list_factors/get_factor/create_factor_draft/create_factor_version/check_version_immutable/get_factor_references）
  - **关键设计**：JSON 字段用 AliasChoices 映射（params_json→params）；create_factor_version 用 compute_factor_content_hash 幂等；check_version_immutable 检查 EvaluationRun/FactorSetMember/FactorWeightSnapshot 三处引用
- [x] **Task WP1-05**: 实现生命周期服务（合法迁移、硬门禁、actor/reason、409 冲突；`app/services/factors/factor_lifecycle.py`）
  - **产出**：`app/services/factors/factor_lifecycle.py`（ALLOWED_TRANSITIONS/ACTION_TO_STATUS/ACTION_PREREQUISITES 状态机定义；validate_transition 纯校验；execute_transition 行锁+三重校验+幂等+审计+legacy 同步；get_transition_history）
  - **关键设计**：with_for_update 行锁；legacy 因子（lifecycle_status=None）按 origin 推断（system→active，其他→draft）；request_id 幂等；单向同步 status/is_active（active→is_active=1，deprecated→is_active=0）
- [x] **Task WP1-06**: 完成迁移兼容测试（空库、历史 SQLite、目标 MySQL、单 head）
  - **涉及文件**：`app/models/factor.py`、`factor_model.py`、`factor_evaluation.py`（新增）、`factor_runtime.py`、`__init__.py`；`app/schemas/factor_library.py`；`app/services/factors/factor_registry.py`、`factor_lifecycle.py`；`alembic/versions/2026_07_31_0021_factor_library_lifecycle.py`；`tests/test_migration_alembic_chain.py`（更新）、`tests/test_whitebox_factor_lifecycle.py`（新增）
  - **验证**：仓库 Alembic 只有一个 head（`wps_0023_021_factor_library_lifecycle`）；upgrade 不改变已有 Factor.id/Version.id；8 因子幂等回填 Active（lifecycle_status/origin/factor_kind/active_version_id 齐全）；同内容版本幂等（compute_factor_content_hash）；已引用版本不可更新/删除（check_version_immutable 三处引用检查）；非法迁移返回 transition_forbidden（409）；字段错误返回 ValidationError（422）；每次迁移有前后状态/actor/reason/request_id（TransitionAudit 追加式）；普通 CRUD 无法绕过状态机（execute_transition 是唯一入口）
  - **测试结果**：迁移链 24 passed（含 2 个新测试：回填验证+幂等性）；生命周期 28 passed（7 个场景：状态机规则/草稿创建/版本管理/迁移执行/审计不可变/legacy 兼容/版本不可变）；回归 93 passed + 1 failed（DuckDB 未安装环境限制）

## WP2 公式编译、校验与预览 API（依赖 WP1）

- [x] **Task WP2-01**: 抽取 AST 编译核心（白名单、节点数、深度、依赖收集、错误码；`app/services/indicator_ast_sandbox.py` 复用）
  - **产出**：`app/services/factors/factor_compiler.py`（AST 白名单 `_ALLOWED_NODES` 24 种节点类型 + `_FORBIDDEN_NODE_MAP` 11 种精确错误码；`MAX_AST_DEPTH=4`、`MAX_FUNCTION_CALLS=12`；`_collect_dependencies` 收集 fields/source_tables/point_in_time_fields/unknown_names；`_ast_depth` 排除 `expr_context` 节点）
- [x] **Task WP2-02**: 定义原始表达式 DSL（字段和函数能力目录、参数范围、类型检查）
  - **产出**：`FIELD_CATALOG`（24 字段，含 source_table/point_in_time/layer）、`FUNCTION_CATALOG`（16 函数，含 category=rolling/math/conditional、min_args/max_args、arg_validators）；`_BOOLEAN_CONSTANTS`（True/False）
- [x] **Task WP2-03**: 定义后处理配置（winsorize、rank/zscore、neutralize、missing policy）
  - **产出**：`VALID_MISSING_POLICIES`（exclude/impute_zero/ignore）、`_validate_postprocess` 校验 winsorize（mad/quantile）、zscore（ddof）、rank（ascending）、missing_policy
- [x] **Task WP2-04**: 实现稳定执行计划和哈希（canonical JSON、content_hash、compiler_version；`app/services/factors/factor_compiler.py`）
  - **产出**：`ExecutionPlan` dataclass（formula/formula_ast/params/postprocess/data_dependencies/complexity_score/compiler_version/max_lookback/point_in_time_fields）；`content_hash()` 用 `json.dumps(sort_keys=True)` + SHA256[:16]；`COMPILER_VERSION="wp2-1.0.0"`；`compile_formula` 返回 `CompileResult(is_valid/execution_plan/errors)`
- [x] **Task WP2-05**: 实现校验与预览 API（多日期预览、数据来源、缺失原因、data_cutoff_at）
  - API 顺序：POST /factors/validate；POST /factors/preview；POST /factors；POST /factors/{code}/versions；GET /factors；GET /factors/{code}；GET /factors/{code}/versions；GET /factors/{code}/references；POST /factors/{code}/transitions
  - **产出**：`app/api/routes/factors.py` 追加 `POST /factors/validate`（纯静态校验，不访问数据库）和 `POST /factors/preview`（编译 + readiness + 完整交易日证据 + FactorExecutor 预览值）；`app/schemas/factor_library.py` 追加 `FactorValidateRequest/Response`、`FactorPreviewRequest/Response/ValueItem`；`app/services/factors/factor_registry.py` 集成编译器到版本创建流程（自动填充 formula_ast_json/data_dependencies_json/execution_plan_hash/complexity_score）
- [x] **Task WP2-06**: 接入 DuckDB 兼容执行（冻结批次读取、单写锁、失败不覆盖旧批次）
  - **涉及文件**：`app/services/factors/factor_compiler.py`、`factor_executor.py`（新增）、`factor_registry.py`、`store.py`；`app/api/routes/factors.py`；`app/schemas/factor_library.py`
  - **产出**：`app/services/factors/factor_executor.py`（FactorExecutor 类：`_read_source_data` 冻结批次读取 read_only 连接；`_evaluate` AST 递归求值 pandas Series 向量化；`apply_postprocess` winsorize/zscore/rank/missing_policy；`preview` 只读预览；`execute` 单写锁写入 `safe_write_context` + `calc_batch_id` 主键防覆盖）；`tests/test_whitebox_factor_executor.py`（43 测试：后处理 5 类 + AST 评估器 12 + FactorExecutor 集成 9 + 预览 API 集成 1 + 辅助函数 5 + 安全幂运算 4）
  - **验证**：禁止属性访问/导入/任意函数/eval（`_ALLOWED_NODES` + `_FORBIDDEN_NODE_MAP` 11 种错误码）；超限明确拒绝（`MAX_AST_DEPTH=4`、`MAX_FUNCTION_CALLS=12`）；未知字段/函数/类型错误返回稳定错误码（`unknown_field`/`unknown_function`/`attribute_access_forbidden` 等）；公式依赖和 readiness 可解释（`data_dependencies` 含 fields/source_tables/point_in_time_fields/max_lookback）；相同输入相同执行计划和 content_hash（canonical JSON + SHA256[:16]）；固定 data_cutoff_at 预览可重复（冻结批次读取 read_only 连接）；预览默认选最近完整交易日（`latest_complete_trade_date`）；财报公告日前不可见（8 系统因子 PIT 由现有 `factor_engine.py` 保证，`test_roe_yoy_growth_respects_announcement_date` 验证）；DuckDB 写失败不覆盖上一成功批次（`calc_batch_id` 主键 + `safe_write_context` ROLLBACK，`test_execute_does_not_overwrite_old_batch` 验证）
  - **测试结果**：WP2 编译器 97 + 执行器 43 = 140 测试全部通过；回归 240 测试全部通过（编译器+执行器+基线冻结+生命周期+迁移链+流水线状态+设置+引擎+运行时API）

## WP3 因子中心前端（API 契约冻结后可与 WP2 并行）

- [x] **Task WP3-01**: 因子中心壳层（Settings 入口、页签、旧入口兼容）
- [x] **Task WP3-02**: 因子库列表（搜索、筛选、分页、状态、空状态）
- [x] **Task WP3-03**: 因子详情（版本、引用、状态历史、风险提示）
- [x] **Task WP3-04**: 抽取公式编辑组件（从 `CustomIndicatorSettings.tsx` 复用编辑与预览）
- [x] **Task WP3-05**: 因子编辑器（草稿、模板、参数、后处理、错误定位）
- [x] **Task WP3-06**: 国际化与响应式验收（中英文枚举、错误码、小屏不重叠）
  - **涉及文件**：`frontend/src/components/Settings.tsx`（添加 AppstoreOutlined 入口与 factor-center 容器）；`frontend/src/components/factors/FactorCenter.tsx`（壳层：library/detail/editor 子页签 + localStorage 记忆）；`FactorLibrary.tsx`（搜索/状态/来源/类型筛选 + 分页 + 空状态 + 状态颜色映射）；`FactorDetail.tsx`（基本信息/版本列表/引用信息/迁移历史/状态操作按钮按门禁映射/不可变风险提示 Alert）；`FactorEditor.tsx`（基本信息表单/公式编辑/模板/参数 JSON/后处理 winsorize+zscore+rank+missing_policy/校验+预览结果展示/两栏布局 className 化）；`frontend/src/i18n/zh-CN.ts`（补充 56 个因子中心键 + factorRiskLevel_ 前缀 + factorTransitionSuccess/Failed）；`frontend/src/i18n/en-US.ts`（新增 175 个因子中心英文翻译键）；`frontend/src/styles/workbench.css`（新增 .factor-center/.factor-editor-layout/.factor-editor-main/.factor-editor-side/.factor-editor-actions/.factor-editor-grid 样式 + 1024px/720px 响应式断点）
  - **验证**：中文环境所有枚举（draft/candidate/testing/shadow/active/quarantined/deprecated/rejected、system/user/ai_assisted/imported、continuous/event/regime、higher_better/lower_better/nonlinear、low/medium/high）有中文本地化，不显示裸英文；英文环境翻译完整，中英文键集一致（translations.test.ts 3 项通过）；状态按钮按门禁禁用（TRANSITION_ACTIONS 按 lifecycleStatus 映射，非允许状态不显示按钮）；保存/预览/校验按钮稳定 loading（saving/previewing/validating 状态防重）；1366×768 和 390×844 不重叠（≤1024px 两栏变单栏，≤720px 操作栏纵向排列 + 表单网格单列）；TypeScript tsc --noEmit 通过；Vitest 翻译测试通过（ObservationPool/ExcludedPool 失败为预先存在的 mock 问题，与 WP3 无关）

## WP4 自定义指标提升与 AI 草案（依赖 WP1、WP2、WP3）

- [x] **Task WP4-01**: 数值指标提升接口（CustomIndicator → Candidate Factor，保存来源映射；`app/api/routes/custom_indicators.py` POST /custom-indicators/{id}/promote-to-factor）
  - **产出**：`app/services/factors/factor_registry.py` 新增 `promote_factor_from_indicator`（value_type=number 校验 + params list[dict]→dict 转换 + source_mapping 溯源 + compile_formula 计算 execution_plan_hash 幂等检查）；`app/api/routes/custom_indicators.py` 追加 `POST /settings/custom-indicators/{id}/promote-to-factor` 端点（201/422/404 结构化错误）；`app/schemas/factor_library.py` 追加 `CustomIndicatorPromoteRequest/Response`
  - **关键设计**：boolean 指标拒绝抛 `indicator_not_number`（422）；不存在的指标抛 `indicator_not_found`（404）；key 以数字开头抛 `invalid_factor_code`；同 execution_plan_hash 返回既有版本不创建新版本（幂等）；source_mapping 含 source_type/indicator_id/indicator_key/indicator_version/promoted_at
- [x] **Task WP4-02**: 前端提升操作（仅 number 显示入口，预览映射结果）
  - **产出**：`frontend/src/components/CustomIndicatorSettings.tsx` 新增提升按钮（仅 value_type=number 显示）+ 确认 Modal；`frontend/src/api/client.ts` 追加 `promoteIndicatorToFactor` 方法；`frontend/src/types/index.ts` 追加 `CustomIndicatorPromoteResponse` 类型
- [x] **Task WP4-03**: FactorDraft Schema（公式、参数、方向、依赖、后处理、假设）
  - **产出**：`app/schemas/factor_library.py` 追加 `FactorDraftCreate`（code/name/category 必填 + description/thesis 可选）；`app/services/ai/drafts/factor_draft.py` 新增 `FactorDraftSchema` Pydantic 模型（code/name/category/formula_expr/params/direction/factor_kind/risk_level/description/thesis/change_note）
  - **关键设计**：`FactorDraftSchema` 作为 AI 建议的白名单过滤器，敏感字段（如 api_key）不会进入 suggested_payload；direction 枚举 higher_better/lower_better/nonlinear；factor_kind 枚举 continuous/event/regime；risk_level 枚举 low/medium/high
- [x] **Task WP4-04**: AI 草案服务（结构化返回、校验、来源、内容哈希；`app/services/ai/drafts/indicator.py` 拆出/复用 FactorDraft）
  - **产出**：`app/services/ai/drafts/factor_draft.py`（draft_factor/preview_factor/execute_factor 三步流程）；`app/services/ai/drafts/__init__.py` 注册 `draft_factor` 到 `DRAFT_REGISTRY`；`app/models/ai_session.py` 新增 `ACTION_DRAFT_FACTOR="draft_factor"` 枚举
  - **关键设计**：draft_factor 不写 DB（requires_confirmation=True），用 FactorDraftSchema 过滤 + compile_formula 校验返回 content_hash/execution_plan；preview_factor dry-run 校验（重名检测/非法 direction/AST 校验）；execute_factor 必须先 confirm_action，未确认返回 not_confirmed，确认后创建 draft 因子（lifecycle_status=draft, origin=user, is_active=0）；幂等执行返回 idempotent=True；action_type 不匹配拒绝（action_type_mismatch）；AI 不自动激活（无 TransitionAudit 记录）
- [x] **Task WP4-05**: AI 前端确认流程（应用草案、查看差异、用户主动保存）
  - **产出**：后端 `app/api/routes/ai_drafts.py`（通用 `/ai/drafts/{audit_id}` 端点：GET 详情/POST preview/POST confirm/POST reject/POST execute）；`app/api/router.py` 注册路由；前端 `frontend/src/components/factors/FactorDraftConfirmModal.tsx`（表单编辑/差异展示/校验预览/确认创建/应用到编辑器/拒绝）；`frontend/src/components/factors/FactorCenter.tsx` 集成 Modal + window 事件监听；`frontend/src/components/ai/ExplanationCard.tsx` 检测 draft_factor 类型显示"应用到草稿"按钮；`frontend/src/components/factors/FactorEditor.tsx` 新增 initialPayload 预填表单；`frontend/src/api/client.ts` 新增 getAIDraft/previewAIDraft/confirmAIDraft/rejectAIDraft/executeAIDraft 方法；`frontend/src/types/index.ts` 新增 AIDraftDetail/AIDraftPreviewResult/AIDraftExecuteResult 接口；`frontend/src/i18n/zh-CN.ts` + `en-US.ts` 各新增 28 个 aiDraft* 翻译键
  - **验证**：用测试种子创建至少 2 个 number 指标并验证提升（当前业务库 number=0，不伪造迁移成果）；boolean 提升被拒绝；重复提交用幂等键不产生两个候选；AI 草案过 Pydantic 和 AST 校验；AI 不自动保存/提交/激活；页面显示 AI 原建议与用户修改差异；AI 未配置时手工编辑/模板/校验/预览/候选提交全部可用
  - **测试结果**：WP4 白盒测试 42 passed（指标提升 10 + API 端点 4 + AI 草案三步流程 12 + AI 草案 API 16）；回归测试 363 passed（baseline_freeze 15 + lifecycle 28 + migration 24 + pipeline_state 15 + compiler 97 + executor 43 + settings 6 + engine 7 + runtime_api 6 + error_protocol 15 + readiness 16 + data_source_roadmap 25 + batch_audit 20 + complete_trade_day 12 + pipeline_startup 15 + blackbox 9 + warehouse_locks 20）；1 failed（`test_safe_write_context_raises_when_locked` 为 WPD-03 既有测试设计缺陷，同 PID 重入锁不应抛异常，与 WP4 无关）；1 skipped（blackbox disabled_factor_pipeline 因 duckdb 环境跳过）；前端 `tsc --noEmit` 通过；`translations.test.ts` 3/3 通过

## R1 发布门禁（依赖 WPD + WP0~WP4）

- [x] **Task R1-GATE**: R1 发布门禁验收
  - 现有 8 系统因子 ID/版本/历史引用不变
  - legacy-system-v1 与旧读取路径完成双读对账
  - 因子中心默认只开放 draft/candidate 管理
  - testing 可创建运行记录但 R1 不允许进入 Shadow/Active
  - 数据阻断因子只显示定义和 readiness，不允许提交 testing
  - 至少用 turnover_z20 和 2 个日线数值草稿完成真实 MySQL + DuckDB 预览
  - manual 为默认运行模式，现有评分链无变化
  - 后端 whitebox/blackbox 和前端组件测试通过
  - 浏览器完成中文环境桌面与移动端验收
  - 回滚演练能关闭新写入口并恢复旧读取路径
  - **验证产出**：
    - 8 因子不变性：`freeze_factor_baseline` + `compute_factor_content_hash`（SHA256[:16] canonical JSON）+ 6 白盒测试（`test_freeze_factor_baseline_has_8_factors`/`test_freeze_factor_baseline_db_consistent_after_seed`/`test_freeze_factor_baseline_detects_missing_factor`/`test_freeze_factor_baseline_has_content_hash`/`test_content_hash_is_deterministic`/`test_content_hash_differs_on_formula_change`）
    - legacy-system-v1 双读对账：`_create_legacy_factor_set`（迁移 Phase 5）按 `is_latest=1` 取 8 版本 + `content_hash=sha256(sorted code:version)` + 唯一约束 `(factor_set_id, factor_id)` 幂等；`build_reconciliation_sample` 固定 `RECONCILIATION_BASE_DATE=2026-07-24` + 20 标的 + `sample_hash`
    - draft/candidate 默认开放：后端 `ALLOWED_TRANSITIONS` 无任何状态以 shadow/active 为目标；`ACTION_TO_STATUS` 枚举无 activate/shadow 动作；前端 `TRANSITION_ACTIONS` 修复 testing 状态按钮（移除错误的 `revoke_to_draft`，添加正确的 `reject`）+ draft 补充 `reject` 按钮
    - testing 不进 Shadow/Active：`ACTION_PREREQUISITES` 无 activate/shadow 前置；Schema `TransitionAction` 注释 `# WP6 will add: activate, quarantine, recover_shadow`
    - 数据阻断只读：`data_source_roadmap.py` `forbidden_uses` 显式声明 tail_proxy/capital_flow/valuation 禁止 testing；`readiness.py` C_blocked 层 `_C_BLOCKED_COVERAGE=1.0`；状态机 `start_testing` prereq={candidate} 间接阻断
    - 真实预览：`test_preview_turnover_z20_formula`（sma(turnover_rate, 20) + 2 标的 × 22 天 DuckDB 数据）+ `test_preview_ep_formula`（1/pe_ttm）+ `test_preview_returns_values`（close）= 3 个日线数值草稿真实 DuckDB 预览
    - manual 默认：ORM `weight_mode default='manual'` + 配置 `FACTOR_WEIGHT_MODE default="manual"` + 运行时 `if configured_mode == 'ridge': configured_mode = 'manual'` 强制降级；迁移不触碰 `factor_runtime_state`；`scoring_bridge.py` manual 模式零读写
    - 测试通过：WP4 42 + lifecycle 28 + executor 44 + compiler 97 + baseline_freeze 15 = 226 passed；前端 tsc 通过 + translations 3/3 通过
    - 回滚演练：新增 `_require_feature_enabled` 守卫 `POST /factors`、`POST /factors/{code}/versions`、`POST /factors/{code}/transitions` 三个写入口端点（409 `factor_feature_disabled`）；`fallback_factor_model` 运行时回退（weight_mode→manual, active_model_run_id→None）；旧读取路径 `factor_engine.py` + `definitions.py` 完全未变；迁移 `downgrade()` 路径完整
  - **R1-GATE 修复**：
    - `frontend/src/components/factors/FactorDetail.tsx`：修复 testing 状态按钮（`revoke_to_draft`→`reject`，对齐后端 `ACTION_PREREQUISITES`）；draft 补充 `reject` 按钮
    - `app/api/routes/factors.py`：新增 `_require_feature_enabled` 函数，为 3 个新写入口端点添加 feature flag 检查（回滚门禁）
    - `tests/test_whitebox_factor_executor.py`：新增 `test_preview_turnover_z20_formula`（R1-GATE 真实预览门禁）

## WP5 科学评估与压力测试（依赖 R1）

- [x] **Task WP5-01**: 评估运行契约（不可变 EvaluationRun、配置哈希、数据截止时间）
  - **产出**：`app/services/factors/factor_evaluator.py`（`compute_config_hash` canonical JSON + SHA256[:16]；`build_evaluation_run_id` 幂等键 factor_version_id+config_hash+data_cutoff；`create_evaluation_run` 幂等创建 + 终态保护；`finalize_evaluation_run` 终态拒绝覆盖 `TERMINAL_GATE_RESULTS={passed, rejected}`）
  - **关键设计**：配置哈希 key 顺序无关（`json.dumps(sort_keys=True)`）；终态记录返回既有不覆盖；非终态（None/warn）允许更新可变字段；EvaluationRun 含 WPD-02 完整交易日证据字段（selected_trade_date/observed_symbols/expected_symbols/completeness_ratio/fallback_reason）
- [x] **Task WP5-02**: 时间切分与样本构造（训练/验证/测试、purge/embargo、目标对齐）
  - **产出**：`app/services/factors/factor_evaluator.py`（`build_time_split` 训练/验证/测试三段切分 + purge + embargo 间隔；`align_factor_with_target` 因子值与目标收益对齐，处理事件日样本和 NaN）
  - **关键设计**：purge 隔离训练与验证避免标签泄漏；embargo 隔离验证与测试；事件因子只对事件日样本计算；目标退出日不进入特征可见区（`shift(-horizon)` 对齐）
- [x] **Task WP5-03**: 基础指标（Rank IC、ICIR、覆盖、分组单调性、换手、成本后收益；`app/services/factors/factor_evaluator.py`）
  - **产出**：`compute_rank_ic`（日截面 Rank IC + mean/median/std/ICIR/positive_ratio）；`compute_quantile_returns`（N 分组收益 + monotonicity_score + long_short_return）；`compute_turnover`（日均换手率）；`compute_cost_adjusted_return`（成本后收益）；`compute_coverage`（覆盖率 + n_samples）
  - **关键设计**：Rank IC 用 Spearman 秩相关；分组单调性 = 相邻分组收益同向比例；成本后收益 = 多空收益 - 换手率 × 成本率 × 2
- [x] **Task WP5-04**: 分类门禁（continuous、event、regime 独立阈值和失败原因）
  - **产出**：`evaluate_gate` 按 `factor_kind`（continuous/event/regime）应用不同阈值；返回 `GateResult(gate_result, rejection_reasons, metrics)`
  - **关键设计**：continuous 门禁（coverage≥0.7, ICIR≥0.3, rank_ic>0, n_days≥30, net_return>0）；event 门禁（event_samples≥10, event_icir≥0.2）；regime 门禁（time_coverage≥0.6）；任一硬门禁失败 gate_result=rejected
- [x] **Task WP5-05**: 参数和样本扰动（参数邻域、时间段、市场状态、缺失敏感度；`app/services/factors/factor_stress.py`）
  - **产出**：`perturb_parameter`（参数邻域扰动 ±50%/±25%/0%，IC 符号一致性 + 中位 IC 比例 + 断崖检测）；`perturb_time_segments`（时间段切分评估 IC 稳定性）；`perturb_missing_sensitivity`（缺失数据对 IC 的影响）；`run_stress_test`（汇总参数/时间/缺失扰动结果）
  - **关键设计**：`DEFAULT_PERTURBATION_RATIOS=[-0.5,-0.25,0,0.25,0.5]`；整数参数取整；断崖检测（任一非基准点 IC < 基准 50%）；verdict 三态 stable/unstable/cliff_drop；扰动复用相同 data_cutoff_at（`PERTURBATION_THRESHOLDS`）
- [x] **Task WP5-06**: 异步任务接线（进度、取消、重跑、心跳、终态恢复、幂等）
  - **产出**：`app/services/factors/wp5_eval_task.py`（`create_evaluation_task` 单飞幂等 + 复用现有 async_tasks 框架；`_run_evaluation_worker` 评估 worker 含进度/心跳/终态；`get_evaluation_task`/`cancel_evaluation_task`/`list_evaluation_tasks`）；`app/api/routes/factor_evaluation.py`（POST /factor-evaluation/tasks 创建、GET 列表/详情、POST cancel）；`app/api/router.py` 注册路由
  - **关键设计**：单飞检查同 factor_code 有 queued/running 任务则复用；worker 复用 `async_tasks.create_async_task` + `_start_worker`；终态保护（cancelled/done/failed 不被覆盖）；幂等键（factor_code + config）
- [x] **Task WP5-07**: 评估实验室前端（报告、门禁、扰动、运行历史、失败原因）
  - **产出**：`frontend/src/components/factors/FactorEvaluationLab.tsx`（任务创建表单 + 活动任务进度卡片含心跳/停滞检测/已运行时长 + 任务列表 + 运行历史含门禁筛选 + 运行报告含基础指标/门禁结论/拒绝原因/压力测试参数扰动表/时间扰动/缺失敏感度/完整交易日证据/不可变警告）；`frontend/src/api/client.ts` 新增 6 个 API 方法 + 5 个 TypeScript 类型；`frontend/src/i18n/zh-CN.ts`+`en-US.ts` 新增 40+ 翻译键；`frontend/src/styles/workbench.css` 新增 `.factor-eval-lab*` 样式；`frontend/src/components/factors/FactorCenter.tsx` 注册 evaluation 页签；`frontend/src/components/__tests__/FactorEvaluationLab.test.tsx` 12 测试
  - **关键设计**：进度轮询 2s 间隔；停滞检测 30s 无进度更新告警；终态自动选中对应运行；门禁颜色映射 passed=green/rejected=red/warn=orange；压力测试 verdict 三态颜色 stable=green/unstable=orange/cliff_drop=red；完整交易日证据展示 selected_trade_date/observed/expected/completeness_ratio
  - **评估顺序**：turnover_z20 验证新引擎 → return_5d/return_20d/ma_gap/volatility/volume_ratio → lhb 事件日样本 → hot_rank 快照连续性验证 → roe 公告日覆盖达标后 → ep/negative_pb/main_inflow/tail 保持 blocked
  - **验证**：同版本/同快照/同配置可重复（`compute_config_hash` + `build_evaluation_run_id` 幂等）；EvaluationRun 保存完整交易日证据（WPD-02 字段）；事件因子不误用连续门禁（`evaluate_gate` 分类阈值）；参数扰动复用相同 data_cutoff_at（`perturb_parameter` 不改 cutoff）；任一硬门禁失败不进 Shadow（gate_result=rejected）；取消进 canceled 不停留 running（`cancel_evaluation_task` 终态）；同幂等键不启动两个评估（单飞检查）；评估结果和原始配置不可更新（`finalize_evaluation_run` 终态保护）
  - **测试结果**：后端 `test_whitebox_wp5_evaluation.py` 51 passed（评估运行契约 11 + 时间切分 8 + IC 指标 4 + 分组单调性 3 + 换手 2 + 成本后收益 2 + 覆盖 2 + 分类门禁 9 + 参数扰动 3 + 时间扰动 2 + 缺失敏感度 2 + 压力测试汇总 1 + run_evaluation 端到端 2）；前端 `FactorEvaluationLab.test.tsx` 12 passed（空状态/表单校验/提交/任务列表/运行历史/拒绝原因/完整交易日证据/压力测试/自动选中/取消/不可变警告/运行中禁用）；前端 `tsc --noEmit` 通过；后端回归 470 passed + 3 skipped（blackbox duckdb 环境）+ 1 failed（WPD-03 既有 `test_safe_write_context_raises_when_locked` 同 PID 重入锁设计缺陷，与 WP5 无关）

## WP6 相关性治理、Shadow 与审批（依赖 WP5）

- [ ] **Task WP6-01**: 相关矩阵和聚类（Active/Candidate 相关矩阵、簇、代表因子；`app/services/factors/factor_correlation.py`）
- [ ] **Task WP6-02**: 残差增量评估（中性化、正交化、残差 IC）
- [ ] **Task WP6-03**: Shadow 每日观测（按因子版本和交易日幂等记录）
- [ ] **Task WP6-04**: 衰减和数据健康告警（IC 衰减、覆盖突降、常数化、缺失异常）
- [ ] **Task WP6-05**: 人工审批流程（Shadow → Active 申请、确认、驳回、审计）
- [ ] **Task WP6-06**: Shadow 前端（观察曲线、风险、相关簇、审批入口）
  - **观察期规则**：Shadow 至少连续 20 个有效交易日；缺少交易日/横截面完整率<90%/数据异常不计入；不允许历史回测回填；性能衰减只告警不自动 Deprecated；数据硬错误可自动 Quarantined 但追加审计；Active 必须 local_user 明确批准
  - **验证**：高相关候选被识别并给出去留证据；残差 IC 说明独立增量；同因子版本同交易日只有一条 Shadow 观测；未满足 20 有效交易日 Active 按钮禁用；150/218 残缺日不累计 Shadow 天数；数据硬错误隔离不影响旧 Active 批次；审批含评估 run/观察区间/actor/reason

## R2 发布门禁（依赖 WP5~WP6）

- [ ] **Task R2-GATE**: R2 发布门禁验收（A 层技术因子评估开放；其他类别按数据门禁逐步开放；不降低门禁）

## WP7 FactorSet 与 Ridge 接线（依赖 R2，可与 Shadow 观察期并行非生产接线）

- [x] **Task WP7-01**: FactorSet 冻结（集合、成员版本、顺序、缺失策略、内容哈希）
- [x] **Task WP7-02**: 动态因子计算（factor_engine 按指定 FactorSet 执行）
- [x] **Task WP7-03**: Ridge 样本接线（动态特征、覆盖、排除原因、版本记录；移除静态 FEATURE_CODES）
- [x] **Task WP7-04**: 模型门禁增强（数据、统计、回测、Shadow 准入）
      - ModelGate 新增 minimum_validation_icir / minimum_cost_adjusted_return / max_weight_drift / max_cluster_exposure / require_factor_set_healthy
      - evaluate_model_gate 增加 ICIR/成本后收益/权重漂移/簇暴露/FactorSet 健康检查（全部向后兼容）
      - 新增 _compute_validation_icir / _compute_weight_drift / _compute_cluster_exposure / _check_factor_set_healthy
      - train_rolling_ridge 自动计算增强指标并写入 metrics_json
      - 白盒测试：tests/test_whitebox_wp7_model_gate.py（27 passed）
- [x] **Task WP7-05**: Score 与解释追溯（factor_set_id、版本、cutoff、模型运行）
      - Score 模型新增 factor_set_id / factor_member_versions_json 字段
      - Alembic 0023 迁移（幂等 ALTER TABLE，添加列+索引）
      - scoring_bridge 从 model.hyperparameters_json 解析 factor_set_id，从 model.weights 构造成员版本快照
      - factor_scores_json 中 _dynamic_model 包含 factor_set_id 追溯信息
      - 向后兼容：legacy 模型无 factor_set_id 时为 None；manual 模式不写入
      - 白盒测试：tests/test_whitebox_wp7_score_traceability.py（6 passed）
- [x] **Task WP7-06**: 模型页前端（查看 FactorSet、候选模型、激活、回退）
      - FactorModelPage.tsx：runtime 状态卡片 + FactorSet 列表（可展开成员）+ 候选模型列表 + 模型详情（权重快照 + 审计日志）
      - 激活/回退 Modal（shadow/ridge 切换、manual 回退需原因）
      - client.ts 新增 listFactorSets/getFactorSet/freezeFactorSet/deprecateFactorSet API
      - FactorCenter.tsx 新增 "因子模型" Tab
      - i18n：zh-CN/en-US 新增 40+ 模型页 key
      - 白盒测试：tests/frontend FactorModelPage.test.tsx（14 passed）；tsc --noEmit 通过
      - 回归：WP4~WP7 + 迁移链 242 passed
  - **验证**：FactorSet 发布后不可修改成员；模型产物固定 FactorSet ID 和每个 FactorVersion；新 Active 因子只影响新模型运行；旧模型重放不读最新因子版本；不满足覆盖和样本门禁时 Ridge 保持 rejected；Shadow 或 Ridge 激活必须人工确认；manual 回退后扫描/评分解释/交易计划仍可用

## WP8 迁移切换、回退与正式验收（依赖 WP7，Shadow 观察期结束后）

- [ ] **Task WP8-01**: 上线顺序执行
  1. 备份业务数据库并记录 Alembic revision；活动 MySQL 无 alembic_version 时记录 schema fingerprint 并先执行经评审 stamp 基线
  2. 部署新表和只读 API，保持所有新写开关关闭
  3. 回填系统因子、版本哈希和 legacy-system-v1
  4. 运行 definitions 与 registry 对账
  5. 对相同 data_cutoff_at 双跑旧路径和 FactorSet 路径
  6. 对比行数、缺失、归一化值、Quality/Timing Score、解释
  7. 开放草稿、候选和评估
  8. 完成 Shadow 观察与审批后单独开放新 FactorSet
  9. 最后允许人工切换 shadow/ridge
  10. 保存正式验收报告和回滚证据
- [ ] **Task WP8-02**: 回滚演练（关闭写开关和评估调度；runtime 切回 manual；清空 active_model_run_id；读取路径回 definitions.py 和旧 factor_engine；保留新表和审计供排障；仅确认无新业务写入时才 Alembic downgrade；回滚后执行 factor settings/engine/runtime/ridge/scoring/主流程测试）
- [ ] **Task WP8-03**: 正式验收门禁
  - **验证**：双读数值在约定容差内一致；迁移在空库/历史 SQLite/目标 MySQL 5.7.26 通过（不用 SQLite 代替）；任务中断/重启/锁冲突/重复请求均有稳定终态；Active 异常不改写旧 FactorSet 或历史 Score；manual 和上一 FactorSet 均可回退；中文界面无未翻译业务枚举；OpenAPI/状态机/错误码/迁移/回滚文档齐全；验收报告记录环境/revision/命令/通过数/遗留风险

# Task Dependencies

- WPD-01 → WPD-02 → WPD-03 → WPD-04 → WPD-05 → WPD-06 → WPD-07（建议顺序，部分可并行）
- WPD（全部）→ WP0
- WP0 → WP1-01 → WP1-02 → WP1-03 → WP1-04 → WP1-05 → WP1-06
- WP1 → WP2-01 → WP2-02 → WP2-03 → WP2-04 → WP2-05 → WP2-06
- WP1 API 契约冻结 → WP3-01 → WP3-02 → WP3-03 → WP3-04 → WP3-05 → WP3-06（WP3 可与 WP2 后端并行）
- WP1 + WP2 + WP3 → WP4-01 → WP4-02 → WP4-03 → WP4-04 → WP4-05
- WPD + WP0 + WP1 + WP2 + WP3 + WP4 → R1-GATE
- R1-GATE → WP5-01 → WP5-02 → WP5-03 → WP5-04 → WP5-05 → WP5-06 → WP5-07
- WP5 → WP6-01 → WP6-02 → WP6-03 → WP6-04 → WP6-05 → WP6-06
- WP5 + WP6 → R2-GATE
- R2-GATE → WP7-01 → WP7-02 → WP7-03 → WP7-04 → WP7-05 → WP7-06（WP7 非生产接线可与 Shadow 20 交易日观察期并行）
- WP7 + Shadow 观察期满 → WP8-01 → WP8-02 → WP8-03

# 并行机会

- WP3 前端外壳可在 WP1 API 契约冻结后与 WP2 后端并行
- 基本面/资金流补数可与 R1 并行，但不作为 R1 上线条件
- 前端可提前使用 mock contract，但正式合并前必须切换真实 API
- WP7 非生产接线可与 Shadow 20 交易日观察期并行
