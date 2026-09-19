# Schema 漂移审计报告（2026-07-23）

## 背景

启动日志报错 `Unknown column 'scan_runs.snapshot_id' in 'field list'`。
报告第 15.3 P1-01 节指出"新增字段主要依赖运行时补丁（init_db.py 的
`_ensure_sqlite_*_columns` + MySQL `information_schema.COLUMNS` 检查），
Alembic revision 不完整"。

本审计对比 `app/db/init_db.py` 的运行时补丁与 `alembic/versions/` 现有
revision，识别所有"只有运行时补丁但 Alembic 缺失 revision"的字段/表/索引。

## 现有 Alembic revision 基线

| Revision ID | 文件 | 内容 |
| --- | --- | --- |
| `wps_001_external_endpoint` | `2026_07_19_0001_external_endpoint_runtime.py` | 创建 `external_endpoint_runtime` 表（含全部列与索引） |

**结论**：除 `external_endpoint_runtime` 表外，所有其他运行时补丁字段/表
均无对应 Alembic revision。MySQL 上这些字段/表完全缺失（部分补丁只覆盖
SQLite，MySQL 路径未补；部分补丁 SQLite/MySQL 都有但 Alembic 缺失）。

## 根因分析：`scan_runs.snapshot_id` 报错

- 模型 `app/models/scan.py` 的 `ScanRun` 定义了 `snapshot_id` 等 8 个
  WP-P.6 缓存字段。
- `init_db.py` 的 `_ensure_sqlite_scan_run_cache_columns` 仅在 SQLite 路径
  补字段；**MySQL 路径无对应补丁**（`_ensure_mysql_indicator_version_columns`
  未覆盖 `scan_runs` 表）。
- 因此 MySQL 业务库的 `scan_runs` 表完全没有这些列，ORM SELECT 时直接报
  `Unknown column`。

## 差异清单（按表分组）

格式：`表名.字段` | 模型定义位置 | init_db 补丁位置 | SQLite 补丁 | MySQL 补丁 | Alembic revision

### 1. scan_runs（报错根因表）

| 字段 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| snapshot_id | scan.py:43 | `_ensure_sqlite_scan_run_cache_columns` | ✅ | ❌ 缺失 | ❌ 缺失 |
| cache_key | scan.py:45 | 同上 | ✅ | ❌ 缺失 | ❌ 缺失 |
| cache_hit | scan.py:47 | 同上 | ✅ | ❌ 缺失 | ❌ 缺失 |
| total_in_snapshot | scan.py:49 | 同上 | ✅ | ❌ 缺失 | ❌ 缺失 |
| coarse_match_count | scan.py:50 | 同上 | ✅ | ❌ 缺失 | ❌ 缺失 |
| advanced_match_count | scan.py:51 | 同上 | ✅ | ❌ 缺失 | ❌ 缺失 |
| result_rows_written | scan.py:52 | 同上 | ✅ | ❌ 缺失 | ❌ 缺失 |
| degraded_reason | scan.py:54 | 同上 | ✅ | ❌ 缺失 | ❌ 缺失 |

索引：`ix_scan_runs_snapshot_id`、`ix_scan_runs_cache_key`（模型 index=True）

### 2. scan_results

| 字段 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| warning_days | scan.py:76 | `_ensure_sqlite_scan_result_columns` | ✅ | ❌ 缺失 | ❌ 缺失 |
| valid_days | scan.py:77 | 同上 | ✅ | ❌ 缺失 | ❌ 缺失 |
| is_frozen | scan.py:78 | 同上 | ✅ | ❌ 缺失 | ❌ 缺失 |
| is_active | scan.py:81 | 同上 | ✅ | ❌ 缺失 | ❌ 缺失 |

### 3. scores（P0 评分配置快照 + 动态因子模型）

| 字段组 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| breakout_score/pullback_score/overheat_penalty | score.py:32-34 | `_ensure_sqlite_score_columns` | ✅ | ❌ 缺失 | ❌ 缺失 |
| scoring_asset_type/config_id/preset_key/preset_name/config_version/snapshot_json | score.py:38-43 | 同上 | ✅ | ✅ | ❌ 缺失 |
| dimension_scores_json/factor_scores_json | score.py:44-45 | 同上 | ✅ | ✅ | ❌ 缺失 |
| weight_mode/factor_model_run_id/factor_data_cutoff_at | score.py:47-55 | 同上 | ✅ | ✅ | ❌ 缺失 |
| factor_quality_score/timing_score/model_alpha_score | score.py:56-62 | 同上 | ✅ | ✅ | ❌ 缺失 |
| macro_regime/macro_position_multiplier | score.py:63-68 | 同上 | ✅ | ✅ | ❌ 缺失 |

### 4. factors

| 字段 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| source_type | factor.py:20 | `_ensure_sqlite_factor_columns` | ✅ | ✅ | ❌ 缺失 |
| frequency | factor.py:21 | 同上 | ✅ | ✅ | ❌ 缺失 |
| default_missing_policy | factor.py:22 | 同上 | ✅ | ✅ | ❌ 缺失 |
| is_active | factor.py:25 | 同上 | ✅ | ✅ | ❌ 缺失 |

### 5. backtest_runs

| 字段组 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| score_weight_mode/factor_model_run_id/factor_data_cutoff_at | backtest.py:20-28 | `_ensure_sqlite_backtest_columns` | ✅ | ✅ | ❌ 缺失 |
| member_snapshot_json/symbol_ids_json/excluded_members_json | backtest.py:51-61 | `_ensure_sqlite_backtest_snapshot_columns` + `_ensure_mysql_backtest_snapshot_columns` | ✅ | ✅ | ❌ 缺失 |
| portfolio_rule_version_id/score_mode/data_cutoff_at | backtest.py:62-70 | 同上 | ✅ | ✅ | ❌ 缺失 |
| engine_name/engine_version/source_type | backtest.py:71-79 | 同上 | ✅ | ✅ | ❌ 缺失 |

### 6. trade_setups

| 字段 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| manual_overrides_json | trade_setup.py:33 | `_ensure_sqlite_trade_setup_columns` | ✅ | ❌ 缺失 | ❌ 缺失 |
| field_sources_json | trade_setup.py:34 | 同上 | ✅ | ❌ 缺失 | ❌ 缺失 |
| manual_tranche_plan_json | trade_setup.py:35 | 同上 | ✅ | ❌ 缺失 | ❌ 缺失 |

### 7. custom_indicator_versions / journal_entries / discovery_tasks（杂项补丁）

| 表.字段 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| custom_indicator_versions.change_note | custom_indicator.py:39 | `_ensure_sqlite_indicator_version_columns` + `_ensure_mysql_indicator_version_columns` | ✅ | ✅ | ❌ 缺失 |
| journal_entries.review_tags_json | journal_entry.py:29 | `_ensure_sqlite_journal_columns` + `_ensure_mysql_indicator_version_columns` | ✅ | ✅ | ❌ 缺失 |
| discovery_tasks.cleanup_count | discovery.py:35 | `_ensure_sqlite_discovery_columns` + `_ensure_mysql_indicator_version_columns` | ✅ | ✅ | ❌ 缺失 |

### 8. portfolios

| 字段 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| auto_trade_enabled | portfolio.py:23 | `_ensure_sqlite_portfolio_columns` + `_ensure_mysql_indicator_version_columns` | ✅ | ✅ | ❌ 缺失 |
| auto_trade_last_run_at | portfolio.py:25 | 同上 | ✅ | ✅ | ❌ 缺失 |

### 9. discovery_tasks（WP-P.1 性能监控字段）

| 字段 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| snapshot_id/snapshot_hit | discovery.py:50-51 | `_ensure_sqlite_discovery_task_perf_columns` + `_ensure_mysql_indicator_version_columns` | ✅ | ✅ | ❌ 缺失 |
| stage_durations_json | discovery.py:53 | 同上 | ✅ | ✅ | ❌ 缺失 |
| dirty_symbol_count/reused_score_count/rescored_count | discovery.py:55-57 | 同上 | ✅ | ✅ | ❌ 缺失 |
| coarse_match_count/advanced_match_count/result_rows_written | discovery.py:58-60 | 同上 | ✅ | ✅ | ❌ 缺失 |
| cache_key/cache_hit | discovery.py:62-63 | 同上 | ✅ | ✅ | ❌ 缺失 |
| degraded_reason | discovery.py:65 | 同上 | ✅ | ✅ | ❌ 缺失 |

### 10. watchlist_items（WP-P.7 + WP2.1）

| 字段 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| score_snapshot_json | watchlist.py:34 | `_ensure_sqlite_watchlist_item_score_snapshot_column` | ✅ | ❌ 缺失 | ❌ 缺失 |
| origin_type | watchlist.py:39 | `_ensure_sqlite_watchlist_item_columns` + `_ensure_mysql_watchlist_item_columns` | ✅ | ✅ | ❌ 缺失 |
| origin_id | watchlist.py:44 | 同上 | ✅ | ✅ | ❌ 缺失 |
| reason_json | watchlist.py:46 | 同上 | ✅ | ✅ | ❌ 缺失 |
| status | watchlist.py:48 | 同上 | ✅ | ✅ | ❌ 缺失 |
| priority | watchlist.py:53 | 同上 | ✅ | ✅ | ❌ 缺失 |
| tags_json | watchlist.py:57 | 同上 | ✅ | ✅ | ❌ 缺失 |
| target_portfolio_id | watchlist.py:59 | 同上 | ✅ | ✅ | ❌ 缺失 |
| updated_at | watchlist.py:64 | 同上 | ✅ | ✅ | ❌ 缺失 |
| archived_at | watchlist.py:69 | 同上 | ✅ | ✅ | ❌ 缺失 |

索引：`idx_watchlist_items_origin_id`、`idx_watchlist_items_status`、`idx_watchlist_items_target_portfolio_id`

### 11. opportunity_transition_events（整表，WP3.1）

| 项 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| 整表 | opportunity_transition_event.py | `_ensure_sqlite_opportunity_transition_events_table` + `_ensure_mysql_opportunity_transition_events_table` | ✅ | ✅ | ❌ 缺失 |

索引：`idx_ote_symbol_event`、`idx_ote_source`、`idx_ote_target`、`idx_ote_created` + `idempotency_key` 唯一索引

### 12. portfolio_members（整表，WP4.1）

| 项 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| 整表 | portfolio_member.py | `_ensure_sqlite_portfolio_members_table` + `_ensure_mysql_portfolio_members_table` | ✅ | ✅ | ❌ 缺失 |

索引：`idx_portfolio_members_active`（部分唯一索引，SQLite sqlite_where；MySQL 无）、`idx_portfolio_members_status`、`idx_portfolio_members_source`、`idx_portfolio_members_execution`

### 13. notification_*（6 张整表，WP-MSG.1）

| 表 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| notification_channels | notification.py | `_ensure_sqlite_notification_tables` + `_ensure_mysql_notification_tables` | ✅ | ✅ | ❌ 缺失 |
| notification_policies | 同上 | 同上 | ✅ | ✅ | ❌ 缺失 |
| notification_policy_channels | 同上 | 同上 | ✅ | ✅ | ❌ 缺失 |
| notification_outbox | 同上 | 同上 | ✅ | ✅ | ❌ 缺失 |
| notification_deliveries | 同上 | 同上 | ✅ | ✅ | ❌ 缺失 |
| notification_templates | 同上 | 同上 | ✅ | ✅ | ❌ 缺失 |

索引：`idx_no_event_channel_pending`（部分唯一索引，SQLite sqlite_where；MySQL 无）、`idx_no_status`、`idx_no_source`、`idx_no_retry` + `uq_notification_policy_channels_policy_channel`

### 14. sim_orders（WP6.1 归因字段）

| 字段 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| member_id | sim_account.py:46 | `_ensure_sqlite_sim_orders_attribution_columns` + `_ensure_mysql_sim_orders_attribution_columns` | ✅ | ✅ | ❌ 缺失 |
| source_type | sim_account.py:51 | 同上 | ✅ | ✅ | ❌ 缺失 |
| source_id | sim_account.py:55 | 同上 | ✅ | ✅ | ❌ 缺失 |
| signal_id | sim_account.py:59 | 同上 | ✅ | ✅ | ❌ 缺失 |
| signal_snapshot_json | sim_account.py:63 | 同上 | ✅ | ✅ | ❌ 缺失 |
| rule_version_id | sim_account.py:67 | 同上 | ✅ | ✅ | ❌ 缺失 |
| execution_mode | sim_account.py:71 | 同上 | ✅ | ✅ | ❌ 缺失 |
| client_order_key | sim_account.py:75 | 同上 | ✅ | ✅ | ❌ 缺失 |
| decision_snapshot_json | sim_account.py:79 | 同上 | ✅ | ✅ | ❌ 缺失 |
| rejection_code | sim_account.py:83 | 同上 | ✅ | ✅ | ❌ 缺失 |
| rejection_detail | sim_account.py:87 | 同上 | ✅ | ✅ | ❌ 缺失 |

索引：`idx_sim_orders_member`、`idx_sim_orders_source`、`idx_sim_orders_signal`、`idx_sim_orders_client_key`（唯一）

### 15. ai_sessions / ai_messages / ai_action_audits（3 张整表，WP-AI.1）

| 表 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| ai_sessions | ai_session.py | `_ensure_sqlite_ai_session_tables` + `_ensure_mysql_ai_session_tables` | ✅ | ✅ | ❌ 缺失 |
| ai_messages | 同上 | 同上 | ✅ | ✅ | ❌ 缺失 |
| ai_action_audits | 同上 | 同上 | ✅ | ✅ | ❌ 缺失 |

### 16. portfolio_reviews（整表，WP8）

| 项 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| 整表 | review.py | `_ensure_sqlite_portfolio_reviews_table` + `_ensure_mysql_portfolio_reviews_table` | ✅ | ✅ | ❌ 缺失 |

### 17. ai_profiles（整表，WP-AI.2）

| 项 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| 整表 + 全部列 | ai_profile.py | `_ensure_sqlite_ai_profiles_table` + `_ensure_mysql_ai_profiles_table` | ✅ | ✅ | ❌ 缺失 |

### 18. discovery_score_snapshots / discovery_score_snapshot_items（2 张整表，WP-P.2）

| 表 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| discovery_score_snapshots | discovery_score_snapshot.py | 无（仅 Base.metadata.create_all） | ✅ | ✅ | ❌ 缺失 |
| discovery_score_snapshot_items | 同上 | 无（仅 Base.metadata.create_all） | ✅ | ✅ | ❌ 缺失 |

索引：`ix_snapshot_scope_status_date`、`uq_snapshot_scope_status_date`、`ix_snapshot_item_priority`、`ix_snapshot_item_action_stage`、`uq_snapshot_item_symbol`

### 19. async_tasks（WP-S.5 状态机字段）

| 字段 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| heartbeat_at | async_task.py:43 | `_ensure_sqlite_async_task_columns` + `_ensure_mysql_indicator_version_columns` | ✅ | ✅ | ❌ 缺失 |
| stage_budget_seconds | async_task.py:44 | 同上 | ✅ | ✅ | ❌ 缺失 |
| stage_started_at | async_task.py:45 | 同上 | ✅ | ✅ | ❌ 缺失 |
| last_progress_at | async_task.py:46 | 同上 | ✅ | ✅ | ❌ 缺失 |
| last_progress_percent | async_task.py:47 | 同上 | ✅ | ✅ | ❌ 缺失 |
| current_step_description | async_task.py:48 | 同上 | ✅ | ✅ | ❌ 缺失 |
| suggested_action | async_task.py:49 | 同上 | ✅ | ✅ | ❌ 缺失 |
| batch_recovery_json | async_task.py:50 | 同上 | ✅ | ✅ | ❌ 缺失 |
| last_patrol_at | async_task.py:51 | 同上 | ✅ | ✅ | ❌ 缺失 |
| worker_thread_id | async_task.py:52 | 同上 | ✅ | ✅ | ❌ 缺失 |
| cancel_requested | async_task.py:53 | 同上 | ✅ | ✅ | ❌ 缺失 |

### 20. api_deprecation_logs（整表，WP9.6）

| 项 | 模型 | init_db 补丁 | SQLite | MySQL | Alembic |
| --- | --- | --- | --- | --- | --- |
| 整表 | api_deprecation_log.py | 无（仅 Base.metadata.create_all） | ✅ | ✅ | ❌ 缺失 |

## 修复方案

为上述 20 组差异建立链式 Alembic revision（`2026_07_23_0001` ~
`2026_07_23_0020`），每个 revision：

- `down_revision` 指向上一个（第一个指向 `wps_001_external_endpoint`）
- 包含 `upgrade()` 和 `downgrade()`
- SQLite/MySQL 双库 DDL 兼容（MySQL 5.7 语法，先检查 information_schema
  再执行；SQLite 用 inspector 检查）
- 幂等：先检查再执行
- 可逆：downgrade 回滚 upgrade 的所有变更
- 历史数据保护：只新增字段/表/索引，新字段允许为空

`init_db.py` 的运行时补丁保留作为兜底（Alembic 未应用时的启动补齐），
但 Alembic 成为正式迁移手段。

## 链式 revision 清单

| 序号 | Revision ID | down_revision | 内容 |
| --- | --- | --- | --- |
| 1 | `wps_0023_001_scan_runs_cache` | `wps_001_external_endpoint` | scan_runs 缓存字段（报错根因） |
| 2 | `wps_0023_002_scan_results_cols` | `wps_0023_001_scan_runs_cache` | scan_results 4 字段 |
| 3 | `wps_0023_003_scores_cols` | `wps_0023_002_scan_results_cols` | scores P0 + 动态因子字段 |
| 4 | `wps_0023_004_factors_cols` | `wps_0023_003_scores_cols` | factors 4 字段 |
| 5 | `wps_0023_005_backtest_runs_cols` | `wps_0023_004_factors_cols` | backtest_runs 快照字段 |
| 6 | `wps_0023_006_trade_setups_cols` | `wps_0023_005_backtest_runs_cols` | trade_setups 3 字段 |
| 7 | `wps_0023_007_indicator_journal_discovery` | `wps_0023_006_trade_setups_cols` | 杂项 3 字段 |
| 8 | `wps_0023_008_portfolios_auto_trade` | `wps_0023_007_indicator_journal_discovery` | portfolios 2 字段 |
| 9 | `wps_0023_009_discovery_tasks_perf` | `wps_0023_008_portfolios_auto_trade` | discovery_tasks WP-P.1 |
| 10 | `wps_0023_010_watchlist_items_extend` | `wps_0023_009_discovery_tasks_perf` | watchlist_items WP-P.7 + WP2.1 |
| 11 | `wps_0023_011_opportunity_transitions` | `wps_0023_010_watchlist_items_extend` | opportunity_transition_events 整表 |
| 12 | `wps_0023_012_portfolio_members` | `wps_0023_011_opportunity_transitions` | portfolio_members 整表 |
| 13 | `wps_0023_013_notification_tables` | `wps_0023_012_portfolio_members` | notification_* 6 表 |
| 14 | `wps_0023_014_sim_orders_attribution` | `wps_0023_013_notification_tables` | sim_orders WP6.1 字段 |
| 15 | `wps_0023_015_ai_session_tables` | `wps_0023_014_sim_orders_attribution` | ai_sessions/messages/audits 3 表 |
| 16 | `wps_0023_016_portfolio_reviews` | `wps_0023_015_ai_session_tables` | portfolio_reviews 整表 |
| 17 | `wps_0023_017_ai_profiles` | `wps_0023_016_portfolio_reviews` | ai_profiles 整表 |
| 18 | `wps_0023_018_discovery_score_snapshots` | `wps_0023_017_ai_profiles` | discovery_score_snapshots/items 2 表 |
| 19 | `wps_0023_019_async_tasks_state_machine` | `wps_0023_018_discovery_score_snapshots` | async_tasks WP-S.5 字段 |
| 20 | `wps_0023_020_api_deprecation_logs` | `wps_0023_019_async_tasks_state_machine` | api_deprecation_logs 整表 |
