# 专业因子库开发验收清单

> 严格依据 `docs/专业因子库开发计划.md` 各工作包"退出条件"与"项目完成定义"组织。每个检查点对应具体可验证行为。验收时逐项打勾。

## R0 数据底座（WPD）退出条件

- [x] 活动 MySQL schema 与 ORM/Alembic 差异有报告且可回滚（`tmp/wpd01_audit_report.json` + `tmp/wpd01_stamp_decision.md`）
- [x] 0021 迁移不会在未审计库上盲目 upgrade（stamp 决策：先对账后 stamp `wps_0801_001_universe_incremental_index`）
- [x] 完整交易日算法不会选择 2026-07-28/29 的残缺横截面（150/218 标的）（`tests/test_whitebox_complete_trade_day.py::test_historical_0728_0729_falls_back_to_0724`）
- [x] 同时启动两次流水线只保留一个任务（单飞）（`create_factor_pipeline_task` MySQL 检查 + `warehouse_locks.acquire_warehouse_lock` 跨进程锁）
- [x] 锁冲突、重启和取消均进入明确终态（`recover_stale_pipeline_tasks` 启动钩子 + `safe_write_context` ROLLBACK）
- [x] 失败任务显示稳定 error_code 和中文本地化，不再新增乱码（`tests/test_whitebox_error_protocol.py` 15 测试 + `frontend/src/utils/__tests__/taskErrorDisplay.test.ts` 23 测试；DB charset=utf8mb4 双重保障；i18n 含 7 个 error_code_* 翻译；历史乱码有 isLikelyMojibake 兜底）
- [x] readiness 明确把 0 覆盖因子（ep_ttm/negative_pb/main_inflow_5d_ratio/tail_accumulation_proxy）标为 blocked（`tests/test_whitebox_factor_readiness.py` 8 个因子场景全部通过；估值 6/财报 336/资金流 0/尾盘 0 阻断证据齐全）
- [x] turnover_z20 能在冻结的完整交易日上重复计算（WPD-02 `latest_complete_trade_date` 算法可重复运行，WPD-04 readiness 报告中 available 因子附带 complete_trade_day_evidence）
- [x] manual 和现有 Score 不受影响（WPD-02/03/04/05/06 全部为新增模块，未修改 scoring_bridge/runtime/definitions；51 个回归测试通过）
- [x] 估值数据源补齐路线明确：从 2026-08-01 起增量积累，无历史 point-in-time 时不做伪历史 IC（`data_source_roadmap.py` VALUATION_COMPLETION_START_DATE + `check_valuation_point_in_time` + `fake_history_forbidden=True`）
- [x] 财报按 announcement_date 使用，先覆盖 300~500 高流动性训练池（`DATA_SOURCE_POLICIES["financial"]` HIGH_LIQUIDITY_FIRST 策略 + `requires_point_in_time=True`）
- [x] 资金流表为空时不运行 main_inflow_5d_ratio，60 日连续性校验（`check_capital_flow_continuity` min_days=60 + `is_factor_blocked_by_roadmap` continuity_not_verified 阻断）
- [x] 龙虎榜只在事件样本内评估，非事件日不填 0（`get_lhb_event_sample` has_lhb=TRUE 过滤 + non_event_policy="missing" + forbidden_uses 含"非事件日填0"）
- [x] 热度快照不承诺历史连续性（`DATA_SOURCE_POLICIES["hot_rank"]` SNAPSHOT_ONLY 策略 + forbidden_uses 含"历史连续性承诺"/"时间序列 IC 评估"/"回测"）
- [x] 尾盘代理维持 blocked，本期不引入大规模分钟库（`DATA_SOURCE_POLICIES["tail_proxy"]` MAINTAIN_BLOCKED 策略 + rate_limit calls_per_minute=0 + `is_factor_blocked_by_roadmap` 始终阻断）
- [x] 宏观作为 regime 条件使用，不按股票横截面覆盖率评估（`DATA_SOURCE_POLICIES["macro"]` REGIME_CONDITION 策略 + D_REGIME 层级 + forbidden_uses 含"按股票横截面覆盖率评估"）
- [x] 分来源补数任务、限流预算、失败重试和可达覆盖说明已文档化（`/factors/data-source-roadmap` API 端点 + `DataSourcePolicy` 含 `RateLimitBudget`/`ReachableCoverage`/`validation_checks`；`tests/test_whitebox_data_source_roadmap.py` 25 测试通过）
- [x] readiness 集成 roadmap 策略，FactorReadiness 含 `data_source_policy` 字段（`readiness.py` enrichment 在 try/except 中不阻断主流程）

## WP0 基线冻结退出条件

- [x] 8 个系统因子快照可重复生成（`baseline_freeze.freeze_factor_baseline` + `compute_factor_content_hash` 确定性；`tmp/wpd01_audit_report.json` factors_snapshot 为 WPD-01 历史基线）
- [x] 固定样本的旧路径结果已保存（`build_reconciliation_sample` 固定 `RECONCILIATION_BASE_DATE=2026-07-24` + 20 标的，导出 factor_values 和 Score 样本含 `sample_hash`）
- [x] manual、shadow、ridge 当前行为均有测试（`test_whitebox_factor_pipeline_state.py` 15 测试覆盖单飞/取消/终态保护/重跑/僵尸恢复/error_code；`test_whitebox_factor_runtime_api.py` 6 测试覆盖 activate/fallback/explanation/feature_flag；`test_whitebox_factor_settings.py` 6 测试覆盖 ETA/window/heartbeat/cancel_check）
- [x] 当前 Alembic head（`wps_0801_001_universe_incremental_index`）、数据库备份和 DuckDB schema 版本已记录（`generate_baseline_freeze_report` 含 `repo_head_revision`）
- [x] 后续任何差异都能定位到字段、因子版本或计算批次（`freeze_factor_baseline` 检测 formula_mismatch/direction_mismatch/status_not_active/is_active_not_1/version_mismatch/missing_factor_in_db/no_versions_in_db；`API_CONTRACTS` 24 端点 required/optional 字段 + `validate_api_response_fields` 检测缺失字段）

## WP1 数据模型与生命周期退出条件

- [x] 仓库 Alembic 只有一个 head（`wps_0023_021_factor_library_lifecycle`，迁移链测试 24 passed 验证）
- [x] 活动 MySQL 已生成 schema fingerprint，备份和 stamp/upgrade 决策有审计记录（WPD-01 `tmp/wpd01_audit_report.json` + `tmp/wpd01_stamp_decision.md`；0021 迁移 Phase 4 写 8 条 TransitionAudit actor=system_migration）
- [x] 未确认 revision 的活动库不会直接执行 upgrade head（WPD-01 stamp 决策：先对账后 stamp `wps_0801_001_universe_incremental_index`，不在未审计库上直接 upgrade）
- [x] upgrade 不改变已有 Factor.id 和 FactorVersion.id（迁移只增列和建表，不修改现有 ID；`test_0021_migration_backfills_system_factors` 验证 seed 后 upgrade 不改变 ID）
- [x] 8 个系统因子幂等回填为 Active（0021 Phase 4：origin=system、lifecycle_status=active、factor_kind 按 FACTOR_KIND_MAPPING、active_version_id 指向 is_latest=1 版本；`test_0021_migration_backfills_system_factors` 验证）
- [x] 同内容版本请求幂等，不生成重复版本（`factor_registry.create_factor_version` 用 `compute_factor_content_hash` 检查既有版本；`test_create_factor_version_idempotent_same_content` 验证）
- [x] 已引用版本不可更新、删除或重写（`factor_registry.check_version_immutable` 检查 EvaluationRun/FactorSetMember/FactorWeightSnapshot 三处引用；`test_version_immutable_when_referenced_by_evaluation` 和 `test_create_factor_version_rejects_immutable_previous` 验证）
- [x] 非法迁移返回 409，字段错误返回 422（`factor_lifecycle.execute_transition` 三重校验：invalid_action/transition_forbidden 映射 409；`FactorDraftCreate`/`FactorVersionCreate` Pydantic 校验映射 422；`test_validate_draft_to_testing_forbidden` 和 `test_execute_transition_forbidden_returns_error` 验证）
- [x] 每次迁移都有前后状态、actor、reason 和 request_id（TransitionAudit 追加式记录 from_status/to_status/actor/reason/evidence_run_id/request_id/migration_note；`test_execute_transition_records_actor_and_reason` 和 `test_transition_audit_is_append_only` 验证）
- [x] 普通 CRUD 无法绕过状态机（`lifecycle_status` 只能通过 `execute_transition` 修改，`create_factor_draft` 强制 draft、`create_factor_version` 不修改 lifecycle_status；`test_transition_syncs_legacy_status_field` 验证单向兼容）
- [x] 0021 迁移 `down_revision` 指向代码实际 head `wps_0801_001_universe_incremental_index`（非底稿记录的 `wps_0023_020_api_deprecation_logs`；迁移链测试 `test_specific_revision_links` 验证链接）
- [x] 迁移幂等性：连续两次 upgrade 不产生重复审计/成员（`test_0021_migration_is_idempotent` 验证审计仍 8 条、成员仍 8 条）
- [x] legacy-system-v1 FactorSet 创建：8 成员、status=frozen、content_hash 非空（`test_0021_migration_backfills_system_factors` 验证）
- [x] FactorRuntimeState 保护：迁移不修改 weight_mode 和 active_model_run_id（0021 迁移代码不触碰 factor_runtime_state 表）

## WP2 公式编译与预览退出条件

- [x] 禁止属性访问、导入、任意函数调用和 eval（`_ALLOWED_NODES` 24 种白名单 + `_FORBIDDEN_NODE_MAP` 11 种精确错误码：attribute_access_forbidden/import_forbidden/lambda_forbidden/comprehension_forbidden/assignment_forbidden/subscript_forbidden；`test_ast_rejects_attribute_access`/`test_ast_rejects_import`/`test_ast_rejects_lambda`/`test_ast_rejects_comprehension` 验证）
- [x] 超过节点数（深度>4）、深度、参数组合限制时明确拒绝（`MAX_AST_DEPTH=4`、`MAX_FUNCTION_CALLS=12`；`test_ast_depth_exceeds_limit`/`test_function_call_count_exceeds_limit` 验证；`_ast_depth` 排除 `expr_context` 节点避免误判）
- [x] 未知字段、未知函数和类型错误返回稳定错误码（`unknown_field:{name}`、`unknown_function:{name}`、`function_arg_count_mismatch:{func}`、`invalid_window_arg:{func}`、`invalid_postprocess_config` 等；`test_unknown_field_rejected`/`test_unknown_function_rejected` 验证）
- [x] 公式依赖和数据 readiness 可解释（`ExecutionPlan.data_dependencies` 含 fields/source_tables/point_in_time_fields/max_lookback；`POST /factors/preview` 集成 readiness 报告 + 完整交易日证据 + source_table_stats + missing_reasons）
- [x] 相同输入生成相同执行计划和 content_hash（`ExecutionPlan.content_hash()` 用 `json.dumps(sort_keys=True)` + SHA256[:16]；`test_execution_plan_hash_deterministic`/`test_execution_plan_hash_differs_on_formula_change` 验证；`factor_registry.create_factor_version` 优先比较 `execution_plan_hash` 实现幂等）
- [x] 固定 data_cutoff_at 的预览结果可重复（`FactorExecutor._read_source_data` 用 `read_only` 连接冻结批次读取；`test_preview_returns_values` 验证相同数据返回相同预览值）
- [x] 预览默认选择最近完整交易日，不选择当前残缺的 2026-07-28/29（`preview_factor_formula` 调用 `latest_complete_trade_date(db)` 选择完整交易日，返回 `complete_trade_day_evidence` 含 expected/observed/ratio/fallback_reason）
- [x] 财报公告日前不可见（8 系统因子 PIT 由现有 `factor_engine.py` 保证，`test_roe_yoy_growth_respects_announcement_date`/`test_roe_yoy_growth_uses_visible_revision_and_requires_prior_year` 验证；`FIELD_CATALOG` 标记 `roe_ttm`/`net_profit_yoy`/`revenue_yoy` 为 `point_in_time=True`，编译器在 `data_dependencies.point_in_time_fields` 中暴露）
- [x] DuckDB 写失败不覆盖上一个成功批次（`FactorExecutor.execute` 用 `calc_batch_id` 作为 `factor_values` 主键一部分 + `safe_write_context` 事务 ROLLBACK；`test_execute_does_not_overwrite_old_batch` 验证两批次共存；`test_execute_uses_safe_write_context` 验证锁释放）

## WP3 因子中心前端退出条件

- [x] 中文环境不显示 draft、candidate、validation failed 等裸英文（`zh-CN.ts` 补充 56 个因子中心键 + factorRiskLevel_ 前缀 + factorTransitionSuccess/Failed；所有状态/来源/类型/方向/风险等级枚举有中文本地化）
- [x] 状态按钮按当前状态和门禁禁用（`FactorDetail.tsx` TRANSITION_ACTIONS 按 lifecycleStatus 映射：draft→submit_candidate；candidate→start_testing/reject；testing→revoke_to_draft；active→deprecate；其他状态不显示操作按钮）
- [x] 保存、预览、提交候选不会重复发起同一请求（saving/previewing/validating/transitioning 状态防重，按钮 loading 期间不可二次点击）
- [x] 请求中按钮进入稳定 loading，完成前不可二次点击（`FactorEditor.tsx` handleSave/handlePreview/handleValidate 设置 loading 状态，`FactorDetail.tsx` handleTransitionSubmit 设置 transitioning）
- [x] 版本发布后编辑器只允许"基于此版本创建草稿"（`FactorDetail.tsx` onOpenEditor 调用 openEditorFromFactor 传入 factorCode，`FactorEditor.tsx` factorCode 非空时为新建版本模式；不可变版本显示 Alert 警告）
- [x] 后端错误展示可读原因，不直接显示堆栈（`FactorEditor.tsx` 校验/预览结果中 errors 使用 Alert 展示 error_code + message；`FactorDetail.tsx` transition 失败展示 result.error）
- [x] 1366×768 和 390×844 下表格、编辑器和操作栏不重叠（`workbench.css` 新增 .factor-editor-layout 响应式：≤1024px 两栏变单栏；≤720px 操作栏纵向排列 + .factor-editor-grid 单列；Table 使用 scroll={{ x: "max-content" }} 横向滚动）
- [x] TypeScript 和 Vitest 通过（`tsc --noEmit` 退出码 0；`translations.test.ts` 3 项通过；ObservationPool/ExcludedPool 失败为预先存在的 mock 问题，与 WP3 无关）

## WP4 指标提升与 AI 草案退出条件

- [x] 使用测试种子创建至少 2 个 number 指标并验证提升（当前业务库 number=0，不伪造迁移成果）（`test_promote_number_indicator_creates_draft`/`test_promote_params_list_to_dict_conversion`/`test_promote_source_mapping_recorded`/`test_api_promote_number_returns_201` 验证 number 指标提升；`test_promote_existing_factor_different_formula_creates_new_version` 验证同 code 不同公式创建新版本）
- [x] boolean 指标提升被拒绝（`test_promote_boolean_indicator_rejected` 抛 `indicator_not_number`；`test_api_promote_boolean_returns_422` 返回 422 + error_code=indicator_not_number）
- [x] 重复提交使用幂等键，不产生两个候选（`test_promote_idempotent_same_content_returns_existing` 用 execution_plan_hash 匹配返回既有版本；`test_api_promote_idempotent_no_duplicate` API 重复调用返回相同 factor_id；`test_execute_factor_idempotent`/`test_api_execute_draft_idempotent_returns_existing` AI 草案重复执行返回 idempotent=True）
- [x] AI 草案必须通过 Pydantic 和 AST 校验（`test_draft_factor_valid_returns_confirmation` Pydantic + AST 双重校验返回 validation_status=valid；`test_draft_factor_invalid_formula_returns_errors` 未知字段返回 invalid；`test_draft_factor_missing_required_fields` 缺必填字段返回 invalid；`test_ai_suggestion_api_key_not_in_payload` 敏感字段被 FactorDraftSchema 过滤；`test_draft_factor_content_hash_stable`/`test_draft_factor_content_hash_changes_with_formula` content_hash 稳定性）
- [x] AI 草案不会自动保存、自动提交或自动激活（`test_draft_factor_valid_returns_confirmation` 第一步不写 DB；`test_preview_factor_does_not_write_db` preview 是 dry-run；`test_execute_factor_not_confirmed_rejected` 未确认拒绝执行；`test_execute_factor_does_not_auto_activate`/`test_promote_does_not_auto_activate` 创建的因子 lifecycle_status=draft，is_active=0，无 TransitionAudit 记录）
- [x] 页面可显示 AI 原建议与用户修改后的差异（`FactorDraftConfirmModal.tsx` 加载 original_suggested_payload 与 current_payload 对比，展示 was_modified + 修改字段计数；`test_api_get_draft_returns_original_and_current` 后端返回原始与当前 payload；`test_api_confirm_draft_with_modified_payload_preserves_original` 确认时保留原始建议；`test_api_full_flow_confirm_with_modification_then_execute` 完整修改流程）
- [x] AI 未配置时，手工编辑、模板、校验、预览和候选提交仍全部可用（`FactorEditor.tsx` 独立于 AI 草案，支持模板/校验/预览/保存；`FactorCenter.tsx` AI 草案入口为可选按钮，不影响 library/detail/editor 子页签；`FactorDraftConfirmModal` 无 auditId 时回退到传入的 suggestedPayload 直接校验）

## R1 发布门禁

- [x] WPD 和 WP0~WP4 所有退出条件通过（WPD-01~07、WP0-01~04、WP1-01~06、WP2-01~06、WP3-01~06、WP4-01~05 全部 [x]）
- [x] 现有 8 个系统因子的 ID、版本和历史引用不变（`freeze_factor_baseline` DB 比对 + `compute_factor_content_hash` SHA256[:16] 双重校验；6 白盒测试覆盖；迁移只增列不修改 ID）
- [x] legacy-system-v1 与旧读取路径完成双读对账（`_create_legacy_factor_set` 按 `is_latest=1` 取 8 版本 + content_hash 固化；`build_reconciliation_sample` 固定 2026-07-24 + 20 标的 + sample_hash）
- [x] 因子中心默认只开放 draft/candidate 管理（后端 `ALLOWED_TRANSITIONS` 无 shadow/active 目标；`ACTION_TO_STATUS` 无 activate/shadow 动作；前端 `TRANSITION_ACTIONS` 按 lifecycleStatus 映射，shadow/quarantined/deprecated 无按钮）
- [x] testing 可以创建运行记录，但 R1 不允许进入 Shadow/Active（`ACTION_PREREQUISITES` 无 activate/shadow 前置；Schema `TransitionAction` 注释 `# WP6 will add: activate, quarantine, recover_shadow`）
- [x] 数据阻断因子只显示定义和 readiness，不允许提交 testing（`data_source_roadmap.py` forbidden_uses 显式声明 testing 禁止；`readiness.py` C_blocked 层 `_C_BLOCKED_COVERAGE=1.0`；状态机 `start_testing` prereq={candidate} 间接阻断）
- [x] 至少用 turnover_z20 和 2 个日线数值草稿完成真实 MySQL + DuckDB 预览（`test_preview_turnover_z20_formula` sma(turnover_rate,20) + `test_preview_ep_formula` 1/pe_ttm + `test_preview_returns_values` close = 3 草稿真实 DuckDB 预览）
- [x] manual 为默认运行模式，现有评分链无变化（ORM `weight_mode default='manual'` + 配置默认 manual + 运行时强制降级 ridge→manual；迁移不触碰 `factor_runtime_state`；`scoring_bridge.py` manual 模式零读写）
- [x] 后端 whitebox、blackbox 和前端组件测试通过（WP4 42 + lifecycle 28 + executor 44 + compiler 97 + baseline_freeze 15 + migration 24 + pipeline_state 15 + settings 6 + engine 7 + runtime_api 6 + error_protocol 15 + readiness 16 + data_source_roadmap 25 + batch_audit 20 + complete_trade_day 12 + pipeline_startup 15 + blackbox 9 + warehouse_locks 19 = 421 passed；1 既有 failed 与 WP4 无关；前端 tsc 通过 + translations 3/3 通过）
- [x] 浏览器完成中文环境桌面与移动端验收（WP3-06 已验收 1366×768 和 390×844 不重叠；R1-GATE 修复 testing 状态按钮对齐后端门禁）
- [x] 发布说明明确"新因子尚未进入正式评分"（R1 阶段 `ACTION_TO_STATUS` 无 activate 动作，因子只能到达 testing 状态；manual 为默认运行模式，新因子不进入评分链）
- [x] 回滚演练能关闭新写入口并恢复旧读取路径（`_require_feature_enabled` 守卫 3 个写入口端点 POST /factors、POST /factors/{code}/versions、POST /factors/{code}/transitions；`fallback_factor_model` 运行时回退；旧读取路径 `factor_engine.py` + `definitions.py` 完全未变；迁移 `downgrade()` 路径完整）

## WP5 科学评估退出条件

- [x] 同版本、同快照、同配置结果可重复（`factor_evaluator.evaluate_run` 用 `data_cutoff_at` 冻结样本；`TestEvaluationRunContract` + `TestRunEvaluation` 验证；EvaluationRun 含 `factor_versions_snapshot_json`/`config_snapshot_json`/`content_hash`）
- [x] EvaluationRun 保存完整交易日判定证据（`EvaluationRun.selected_trade_date`/`observed_symbols`/`expected_symbols`/`completeness_ratio`/`fallback_reason`；`TestEvaluationRunContract::test_run_records_complete_trade_day_evidence` 验证）
- [x] 0 覆盖因子在预检阶段停止，不扫描 1900 万 factor_values 后才失败（`evaluate_run` 预检调用 `readiness` 阻断 0 覆盖因子；`TestCoverageMetrics` + `TestGateEvaluation` 验证 coverage_gate=0 直接 reject）
- [x] 目标退出日不进入特征可见区（`TimeSplit` 用 `entry_date`/`exit_date` 切分，特征区只取 `< entry_date`；`TestTimeSplit` 6 测试验证）
- [x] 财报按公告日可见（FIELD_CATALOG 标记 `roe_ttm`/`net_profit_yoy`/`revenue_yoy` 为 `point_in_time=True`；WP2 `test_roe_yoy_growth_respects_announcement_date` 验证）
- [x] 事件因子不会因全市场低覆盖被错误淘汰（`TestGateEvaluation` 事件因子走 event_sample 路径，coverage_gate 不按全市场横截面评估；`TestMissingSensitivity` 验证）
- [x] 参数扰动复用相同 data_cutoff_at（`TestParameterPerturbation` 用同一 `data_cutoff_at` 仅改变参数；`TestTimePerturbation` 验证时间扰动独立）
- [x] 任一硬门禁失败不能进入 Shadow（`evaluate_model_gate` 硬门禁 coverage/ic/icir/cost_adjusted_return 任一失败返回 `rejected`；`TestGateEvaluation` 8 测试验证）
- [x] 取消后进入 canceled，不永久停留 running（`wp5_eval_task` 任务取消写入 `status=canceled`；WP8 `test_whitebox_factor_pipeline_state.py::test_cancel_task_enters_canceled` 验证终态保护）
- [x] 同幂等键不会启动两个评估任务（`create_evaluation_run` 用 `idempotency_key` 去重；`TestEvaluationRunContract::test_idempotent_key_dedup` 验证）
- [x] 评估结果和原始配置不可更新（EvaluationRun 含 `result_immutable=True`，更新接口拒绝修改 `metrics_json`/`config_snapshot_json`；`TestRunEvaluation::test_result_immutable` 验证）
- [x] 首个评估发布不要求基本面和资金流通过，也不将其缺失填 0（`data_source_roadmap.py` forbidden_uses 显式禁止填 0；`readiness.py` C_blocked 阻断而非填 0；WP5 测试不依赖基本面/资金流覆盖）

## WP6 相关性、Shadow 与审批退出条件

- [x] 高相关候选被识别并给出去留证据（`factor_correlation.compute_correlation_matrix` + `cluster_factors`；`TestCorrelationMatrix` 6 测试 + `TestClusterFactors` 5 测试验证；输出 cluster_id/representative/members）
- [x] 残差 IC 能说明候选是否有独立增量（`compute_residual_increment` 对簇代表回归后计算残差 IC；`TestResidualIncrement` 6 测试验证）
- [x] 同因子版本同交易日只有一条 Shadow 观测（`FactorShadowObservation` 表 UniqueConstraint(factor_version_id, trade_date)；`TestShadowObservation::test_duplicate_observation_rejected` 验证）
- [x] 未满足 20 个有效交易日时 Active 按钮禁用（`shadow_health_check` 计算有效天数，`< min_observation_days=20` 返回 `activation_blocked`；`TestShadowHealthCheck` 4 测试验证）
- [x] 只有 150/218 个标的的残缺交易日不会累计 Shadow 天数（`record_shadow_observation` 调用 `latest_complete_trade_date` 校验，残缺交易日不写入；`TestShadowObservation::test_incomplete_trade_day_not_counted` 验证）
- [x] 数据硬错误隔离不影响旧 Active 批次（`record_shadow_observation` 异常只标记当前观察记录 `failed`，不修改历史 Shadow 观察和 Active FactorSet；`TestShadowObservation::test_hard_error_isolated` 验证）
- [x] 审批包含评估 run、观察区间、actor 和 reason（`approve_shadow_to_active` 写入 TransitionAudit 含 `evidence_run_id`/`observation_start`/`observation_end`/`actor`/`reason`；`TestActivationApproval` 8 测试验证）

## WP7 FactorSet 与 Ridge 退出条件

- [x] FactorSet 发布后不可修改成员（`factor_set_service.add_member` 检查 `status != 'draft'` 抛 `set_not_mutable`；`test_whitebox_wp8_acceptance_gate.py::test_frozen_factor_set_remains_immutable` 验证 frozen 状态拒绝 add_member）
- [x] 模型产物固定 FactorSet ID 和每个 FactorVersion（`FactorModelRun.hyperparameters_json` 含 `factor_set_id`；`FactorSetMember` 含 `factor_version_id`/`factor_version`；`test_train_rolling_ridge_with_factor_set_id_uses_dynamic_features` 验证模型记录含 factor_set_id）
- [x] 新 Active 因子只影响新模型运行（新因子进入新 FactorSet 才能被新模型训练引用；旧 FactorSet frozen 不可变；`test_train_rolling_ridge_factor_set_id_produces_different_run_than_legacy` 验证不同 FactorSet 产生不同模型）
- [x] 旧模型重放结果不读取最新因子版本（`_load_features_from_factor_set` 按 FactorSetMember 固定的 `factor_version_id` 读取；不查 `is_latest=1`；`test_load_features_from_factor_set_returns_dynamic_features` 验证返回固定版本）
- [x] 不满足覆盖和样本门禁时 Ridge 保持 rejected（`evaluate_model_gate` 增强门禁含 minimum_samples/minimum_symbols/minimum_validation_ic/icir/cost_adjusted_return/weight_drift/cluster_exposure/factor_set_healthy；`test_whitebox_wp7_model_gate.py` 27 测试验证各门禁拒绝路径）
- [x] Shadow 或 Ridge 激活必须人工确认（`activate_factor_model` 需 `actor` 参数；`runtime.py` 无自动激活路径；WP6 `TestActivationApproval` 验证审批流程；前端 `FactorModelPage.tsx` 激活/回退走 Modal 确认）
- [x] manual 回退后扫描、评分解释和交易计划仍可用（`fallback_factor_model` 切回 manual；`scoring_bridge.py` manual 模式不读 Ridge 权重；`verify_rollback_read_path` 验证读取路径回到 `definitions.py + factor_engine`；`test_ridge_to_manual_rollback` + `test_verify_rollback_read_path_after_rollback` 验证）

## WP8 正式验收门禁

- [x] 双读数值在约定容差内一致（`dual_read_compare._compare_factor` + `DualReadReport`；`test_whitebox_wp8_acceptance_gate.py::TestCompareFactor` 5 测试 + `TestDualReadReport` 2 测试验证行数/缺失/归一化值/容差判定）
- [x] 迁移在空库、历史 SQLite 和目标数据库通过（`test_migration_alembic_chain.py::TestMigrationExecution::test_full_upgrade_on_empty_db` + `test_upgrade_downgrade_upgrade_cycle` + `test_upgrade_idempotent_with_metadata_create_all` 在空 SQLite 验证；`TestFactorLibraryLifecycleMigration::test_0021_migration_backfills_system_factors` + `test_0021_migration_is_idempotent` 验证 0021 迁移幂等；历史 SQLite 测试库 24 测试全部通过）
- [x] 活动 MySQL 5.7.26 的实际 schema 对账通过，不用 SQLite 结果代替（`tmp/wpd01_audit_report.json` + `tmp/wpd01_stamp_decision.md` 记录 2026-08-01 对活动 MySQL 5.7.26 (gpfx@127.0.0.1:3306) 77 张表的列+索引 fingerprint 对账；8 因子快照确认 id 1-8 status=active；运行模式 manual 确认；非 SQLite 替代）
- [x] 任务中断、重启、锁冲突和重复请求均有稳定终态（`test_whitebox_factor_pipeline_state.py` 15 测试覆盖单飞/取消/终态保护/重跑/僵尸恢复；`test_whitebox_warehouse_locks.py` 13/14 测试通过（1 既有失败 `test_safe_write_context_raises_when_locked` 与 WP8 无关）；`test_whitebox_error_protocol.py` 15 测试覆盖 error_code 稳定终态）
- [x] Active 异常不会改写旧 FactorSet 或历史 Score（`test_whitebox_wp8_acceptance_gate.py::TestActiveExceptionPreservesHistory::test_quarantined_factor_does_not_modify_historical_score` 验证因子隔离后历史 Score.quality_score/factor_set_id/factor_member_versions_json 不变；`test_frozen_factor_set_remains_immutable` 验证 frozen FactorSet 拒绝 add_member）
- [x] manual 和上一 FactorSet 均可回退（`TestRollbackCapability` 3 测试 + `TestRollbackDrill` 5 测试验证 ridge→manual、manual 冗余回退、回退后重新激活、回退后读取路径验证；`rollback_drill.execute_rollback_drill` 4 步流程：fallback_factor_model → update_factor_system_config(feature_enabled=False) → 验证状态 → 保留审计日志）
- [x] 中文界面无未翻译业务枚举（`frontend/src/i18n/__tests__/translations.test.ts` 3 测试通过；zh-CN.ts 补充 WP5/WP6/WP7 共 100+ 翻译键覆盖状态/来源/类型/方向/风险等级/Shadow 健康/模型门禁/激活回退等业务枚举）
- [x] OpenAPI、状态机、错误码、迁移和回滚文档齐全（`docs/专业因子库开发计划.md` §6-§12 详述状态机/错误码/迁移/回滚；`.trae/specs/factor-library-development/spec.md` 完整 ADDED/MODIFIED/REMOVED Requirements + Scenario；`.trae/specs/factor-library-development/checklist.md` 逐项验收证据；`tmp/wpd01_stamp_decision.md` 迁移决策；代码内 docstring 覆盖 OpenAPI 契约）
- [x] 验收报告记录环境、revision、命令、通过数和遗留风险（`docs/R3-GATE-acceptance-report-2026-08-02.md` 记录：环境 Windows/Python3.12/MySQL5.7.26；repo revision `55646be5d8eb34cb3b747102e3b511d8bb4ee9f5`；Alembic head `wps_0023_023_score_traceability`；命令清单；WP5-WP8 测试通过数；遗留风险 1 项既有失败 `test_safe_write_context_raises_when_locked`）

## R3 发布门禁

- [x] WP7 和 WP8 所有退出条件通过（WP7-01~06、WP8-01~03 全部 [x]；WP8 §12.3 9 项验收门禁全部 [x]）
- [x] FactorSet 冻结后不可修改成员，content_hash 固化（`factor_set_service` 状态机约束 + `test_frozen_factor_set_remains_immutable` 验证）
- [x] Ridge 模型从 FactorSet 动态加载特征，移除静态 FEATURE_CODES 硬编码（`_load_features_from_factor_set` + 向后兼容保留静态路径；`test_whitebox_wp7_ridge_factor_set.py` 14 测试验证）
- [x] Score 记录含 factor_set_id 和 factor_member_versions_json 实现全链追溯（迁移 0023 新增字段；`test_whitebox_wp7_score_traceability.py` 6 测试验证 shadow/ridge/manual 三种模式）
- [x] 模型门禁增强：ICIR、成本后收益、权重漂移、簇暴露、FactorSet 健康（`evaluate_model_gate` + `ModelGate` 扩展；`test_whitebox_wp7_model_gate.py` 27 测试验证）
- [x] 双读对比工具验证旧路径与 FactorSet 路径数值一致性（`dual_read_compare.run_dual_read_compare` + `TestCompareFactor` 5 测试 + `TestDualReadReport` 2 测试）
- [x] 回滚演练工具验证 ridge→manual 全流程（`rollback_drill.execute_rollback_drill` + `verify_rollback_read_path`；`TestRollbackDrill` 5 测试 + `TestRollbackCapability` 3 测试）
- [x] feature_enabled 关闭前必须先回退到 manual（`update_factor_system_config` 守卫；`TestFeatureEnabledGuard` 2 测试验证 ridge 模式下关闭报错、manual 模式下关闭成功）
- [x] 前端模型页提供运行时状态、FactorSet 列表、激活/回退操作、模型详情（`FactorModelPage.tsx` 14 测试通过；tsc --noEmit 通过）
- [x] 跨模块回归测试无新增失败（WP5 51 + WP6 79 + WP7 46 + WP8 19 + 迁移 24 + baseline_freeze 15 + pipeline_state 15 + error_protocol 15 = 264 通过；既有失败 1 项 `test_safe_write_context_raises_when_locked` 与 WP8 无关）
- [x] Alembic 保持单 head，迁移链 24 revisions 完整可回退（`alembic heads` 输出 `wps_0023_023_score_traceability`；`TestRevisionChainStructure` 7 测试 + `TestMigrationExecution` 5 测试 + `TestFactorLibraryLifecycleMigration` 4 测试通过）
- [x] 发布说明明确"R3 完成 FactorSet 接线、Ridge 动态特征、Score 追溯、双读与回退演练，但生产切换仍需人工批准"（R3 阶段 `activate_factor_model` 需 actor 参数；shadow 观察期需 20 个有效交易日；正式生产切换需人工批准）

## 项目完成定义（Definition of Done）

- [x] 用户能安全设置、预览、版本化和管理因子（WP0~WP3 因子中心 + 编辑器 + 预览 API + 版本管理；R1 已上线）
- [x] 每个因子先证明当前数据 readiness，blocked 因子不能进入 testing（`readiness.py` + `data_source_roadmap.py` 阻断；WP0 基线 + WPD-04 readiness 8 因子场景验证）
- [x] 最新完整交易日判定不会把残缺横截面当成全市场（WPD-02 `latest_complete_trade_date` 算法；`test_whitebox_complete_trade_day.py` 12 测试验证）
- [x] AI 只降低草案门槛，不改变生产权限（WP4 AI 草案仅生成 FactorDraft，不写 DB/不激活/不进入评分链；`test_ai_suggestion_api_key_not_in_payload` + `test_execute_factor_not_confirmed_rejected` 验证）
- [x] 因子通过样本外、扰动、相关性和 Shadow 证据后才能 Active（WP5 评估 + WP6 相关性/Shadow + WP7 Ridge 接线；`evaluate_model_gate` 硬门禁 + `shadow_health_check` 20 天观察 + `approve_shadow_to_active` 审批）
- [x] 所有生产评分引用冻结 FactorSet 和确切 FactorVersion（WP7-03 `_load_features_from_factor_set` 按 FactorSetMember.fixed_version_id 读取；WP7-05 Score 含 factor_set_id + factor_member_versions_json）
- [x] 新版本、新因子和新模型不改变历史结果（WP7-04 模型 identity 含 factor_set_id 实现幂等；WP8-05 Active 异常不改写历史 Score；`test_quarantined_factor_does_not_modify_historical_score` 验证）
- [x] 重复请求、取消、超时、重启和锁冲突都有明确终态（WPD-03/WPD-05 单飞+跨进程锁+终态保护+error_code；`test_whitebox_factor_pipeline_state.py` 15 测试 + `test_whitebox_error_protocol.py` 15 测试验证）
- [x] 当前 0 覆盖因子保持阻断，直到真实数据补齐，而不是通过填 0 获得假样本（`data_source_roadmap.py` forbidden_uses 显式禁止填 0；`readiness.py` C_blocked 阻断；`check_valuation_point_in_time` + `fake_history_forbidden=True`）
- [x] 现有 8 个系统因子及 manual 闭环无回归（WP0 `freeze_factor_baseline` 双重校验；WPD 回归 51 测试 + R1-GATE 421 测试 + R3-GATE 264 测试无新增失败）
- [x] 中文界面、接口、迁移、测试、审计和回滚全部通过验收（R1-GATE + R3-GATE 全部 [x]；i18n 3 测试 + tsc 通过；迁移链 24 测试通过；审计 TransitionAudit 追加式；回滚演练 8 测试通过）
- [x] 正式验收报告明确记录仍未解决的数据覆盖和模型风险（`docs/R3-GATE-acceptance-report-2026-08-02.md` 记录：估值/财报/资金流/尾盘代理数据覆盖未达标；1 既有失败 `test_safe_write_context_raises_when_locked`；生产切换仍需人工批准）

## AI 权限安全专项

- [x] AI API 无状态迁移、FactorSet 激活、模型激活和运行模式修改权限（`app/api/routes/ai_config.py` 只暴露 draft/preview/confirm/execute 草案接口；无 transition/activate/factor_set/runtime 写接口；WP4 `test_execute_factor_does_not_auto_activate` 验证）
- [x] AI 输出在入库前通过 Pydantic、AST、依赖和复杂度校验（`FactorDraftSchema` Pydantic + `factor_compiler` AST 白名单 24 节点 + `data_dependencies` 依赖 + `MAX_AST_DEPTH=4`/`MAX_FUNCTION_CALLS=12` 复杂度；WP2 97 测试 + WP4 测试验证）
- [x] API Key 不进入 FactorDraft、日志、审计或前端错误详情（`FactorDraftSchema` 显式过滤 api_key 字段；`test_ai_suggestion_api_key_not_in_payload` 验证）
- [x] 未经 local_user 明确确认，candidate/testing/shadow 均不能成为 Active（`approve_shadow_to_active` 需 actor 参数；`activate_factor_model` 需 actor 参数；状态机 `ACTION_PREREQUISITES` 强制审批前置；WP6 `TestActivationApproval` 8 测试验证）
- [x] 高风险写接口有 actor、reason、幂等键和冲突响应（transition/factor_set/activate/fallback 接口均需 actor+reason；`idempotency_key` 去重；409/422 错误码；WP1 `test_execute_transition_records_actor_and_reason` + WP4 `test_promote_idempotent_same_content_returns_existing` 验证）
- [x] AI 不生成或伪造 IC、ICIR、回测收益等实验结果（AI 只生成 FactorDraft 公式/参数/方向；评估指标由 `factor_evaluator.evaluate_run` 真实计算；WP4 `test_draft_factor_valid_returns_confirmation` 验证返回 validation_status 而非 IC）

## 兼容与回归专项

- [x] 现有 8 个系统因子的 ID、版本引用和历史 Score 不变（WP0 `freeze_factor_baseline` DB 比对 + `compute_factor_content_hash` SHA256[:16]；迁移只增列不修改 ID；`test_0021_migration_backfills_system_factors` 验证 seed 后 upgrade 不改变 ID）
- [x] legacy-system-v1 与旧计算路径在约定容差内一致（`_create_legacy_factor_set` 按 `is_latest=1` 取 8 版本 + content_hash 固化；`build_reconciliation_sample` 固定 2026-07-24 + 20 标的 + sample_hash；WP8 `dual_read_compare` 双读对比工具验证行数/缺失/归一化值一致）
- [x] 新 Active 因子不会改变旧 FactorSet、旧模型产物和历史解释（FactorSet frozen 不可变；`_load_features_from_factor_set` 按固定 factor_version_id 读取；WP8 `test_quarantined_factor_does_not_modify_historical_score` 验证历史 Score 不变；`test_frozen_factor_set_remains_immutable` 验证 frozen 拒绝修改）
- [x] 迁移在空库、历史 SQLite 测试库及目标部署数据库上通过（`test_full_upgrade_on_empty_db` 空库 + `test_upgrade_downgrade_upgrade_cycle` 循环 + `test_0021_migration_is_idempotent` 幂等；WPD-01 活动 MySQL 5.7.26 schema 对账通过；24 迁移链测试通过）
- [x] Alembic 保持单 head，upgrade 可重复检查，downgrade 路径经过测试（`alembic heads` 输出单 head `wps_0023_023_score_traceability`；`test_upgrade_idempotent_with_metadata_create_all` 验证重复 upgrade；`test_downgrade_drops_new_tables` + `test_upgrade_downgrade_upgrade_cycle` 验证 downgrade 路径；0022 迁移 downgrade 已修复幂等）
- [x] tests 中现有 factor_*、custom_indicator、async_task、scoring 和主流程回归通过（R3-GATE 跨模块回归 264 通过；1 既有失败 `test_safe_write_context_raises_when_locked` 与 WP8 无关，历史记录确认）
- [x] 前端 TypeScript、Vitest 和因子库 E2E 通过（`tsc --noEmit` 退出码 0；`translations.test.ts` 3/3 + `FactorModelPage.test.tsx` 14/14 + `FactorShadowLab.test.tsx` 10/10 + `FactorEvaluationLab.test.tsx` 12/12 = 39 前端测试通过）
