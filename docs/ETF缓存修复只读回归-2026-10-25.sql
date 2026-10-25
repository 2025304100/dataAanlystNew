-- 仅用于 gpfx_validation；只读对账，不修复、不补数。
-- 完整逐行、全列校验请运行 verify_data_management_integrity.py；本 SQL 提供行数与关键字段统计复核。
USE gpfx_validation;
SET TRANSACTION READ ONLY;
START TRANSACTION WITH CONSISTENT SNAPSHOT;
SELECT DATABASE() AS database_name;
SELECT version_num FROM alembic_version;
SELECT COUNT(*) AS active_tasks FROM async_tasks WHERE status IN ('running','queued');

-- 159 张基础表行数必须与本轮基线逐表完全相同。
SELECT 'ai_action_audits' AS table_name,COUNT(*) AS actual_rows,3 AS expected_rows,COUNT(*)=3 AS row_count_matches FROM `ai_action_audits`;
SELECT 'ai_deprecation_logs' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `ai_deprecation_logs`;
SELECT 'ai_drafts' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `ai_drafts`;
SELECT 'ai_messages' AS table_name,COUNT(*) AS actual_rows,43 AS expected_rows,COUNT(*)=43 AS row_count_matches FROM `ai_messages`;
SELECT 'ai_profiles' AS table_name,COUNT(*) AS actual_rows,3 AS expected_rows,COUNT(*)=3 AS row_count_matches FROM `ai_profiles`;
SELECT 'ai_sessions' AS table_name,COUNT(*) AS actual_rows,14 AS expected_rows,COUNT(*)=14 AS row_count_matches FROM `ai_sessions`;
SELECT 'akshare_api_config' AS table_name,COUNT(*) AS actual_rows,30 AS expected_rows,COUNT(*)=30 AS row_count_matches FROM `akshare_api_config`;
SELECT 'alembic_version' AS table_name,COUNT(*) AS actual_rows,1 AS expected_rows,COUNT(*)=1 AS row_count_matches FROM `alembic_version`;
SELECT 'alert_events' AS table_name,COUNT(*) AS actual_rows,240 AS expected_rows,COUNT(*)=240 AS row_count_matches FROM `alert_events`;
SELECT 'alert_rules' AS table_name,COUNT(*) AS actual_rows,3 AS expected_rows,COUNT(*)=3 AS row_count_matches FROM `alert_rules`;
SELECT 'api_deprecation_logs' AS table_name,COUNT(*) AS actual_rows,1 AS expected_rows,COUNT(*)=1 AS row_count_matches FROM `api_deprecation_logs`;
SELECT 'app_settings' AS table_name,COUNT(*) AS actual_rows,2 AS expected_rows,COUNT(*)=2 AS row_count_matches FROM `app_settings`;
SELECT 'async_tasks' AS table_name,COUNT(*) AS actual_rows,1462 AS expected_rows,COUNT(*)=1462 AS row_count_matches FROM `async_tasks`;
SELECT 'auto_trade_strategies' AS table_name,COUNT(*) AS actual_rows,16 AS expected_rows,COUNT(*)=16 AS row_count_matches FROM `auto_trade_strategies`;
SELECT 'backtest_execution_fills' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `backtest_execution_fills`;
SELECT 'backtest_filter_events' AS table_name,COUNT(*) AS actual_rows,360 AS expected_rows,COUNT(*)=360 AS row_count_matches FROM `backtest_filter_events`;
SELECT 'backtest_rule_templates' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `backtest_rule_templates`;
SELECT 'backtest_runs' AS table_name,COUNT(*) AS actual_rows,26 AS expected_rows,COUNT(*)=26 AS row_count_matches FROM `backtest_runs`;
SELECT 'backtest_trades' AS table_name,COUNT(*) AS actual_rows,4 AS expected_rows,COUNT(*)=4 AS row_count_matches FROM `backtest_trades`;
SELECT 'backtest_valuation_snapshots' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `backtest_valuation_snapshots`;
SELECT 'capital_flows' AS table_name,COUNT(*) AS actual_rows,69325 AS expected_rows,COUNT(*)=69325 AS row_count_matches FROM `capital_flows`;
SELECT 'cash_ledger' AS table_name,COUNT(*) AS actual_rows,17 AS expected_rows,COUNT(*)=17 AS row_count_matches FROM `cash_ledger`;
SELECT 'custom_indicators' AS table_name,COUNT(*) AS actual_rows,25 AS expected_rows,COUNT(*)=25 AS row_count_matches FROM `custom_indicators`;
SELECT 'custom_indicator_versions' AS table_name,COUNT(*) AS actual_rows,2 AS expected_rows,COUNT(*)=2 AS row_count_matches FROM `custom_indicator_versions`;
SELECT 'daily_bars' AS table_name,COUNT(*) AS actual_rows,1426908 AS expected_rows,COUNT(*)=1426908 AS row_count_matches FROM `daily_bars`;
SELECT 'data_governance_audit_events' AS table_name,COUNT(*) AS actual_rows,3824 AS expected_rows,COUNT(*)=3824 AS row_count_matches FROM `data_governance_audit_events`;
SELECT 'data_quality_quarantines' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `data_quality_quarantines`;
SELECT 'data_quality_snapshots' AS table_name,COUNT(*) AS actual_rows,1269 AS expected_rows,COUNT(*)=1269 AS row_count_matches FROM `data_quality_snapshots`;
SELECT 'data_sync_coverage' AS table_name,COUNT(*) AS actual_rows,37976 AS expected_rows,COUNT(*)=37976 AS row_count_matches FROM `data_sync_coverage`;
SELECT 'data_sync_partitions' AS table_name,COUNT(*) AS actual_rows,415897 AS expected_rows,COUNT(*)=415897 AS row_count_matches FROM `data_sync_partitions`;
SELECT 'data_sync_plans' AS table_name,COUNT(*) AS actual_rows,145 AS expected_rows,COUNT(*)=145 AS row_count_matches FROM `data_sync_plans`;
SELECT 'data_sync_plan_executions' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `data_sync_plan_executions`;
SELECT 'decision_evidence' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `decision_evidence`;
SELECT 'decision_order_plans' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `decision_order_plans`;
SELECT 'decision_runs' AS table_name,COUNT(*) AS actual_rows,318 AS expected_rows,COUNT(*)=318 AS row_count_matches FROM `decision_runs`;
SELECT 'discovery_candidates' AS table_name,COUNT(*) AS actual_rows,2 AS expected_rows,COUNT(*)=2 AS row_count_matches FROM `discovery_candidates`;
SELECT 'discovery_plans' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `discovery_plans`;
SELECT 'discovery_result_states' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `discovery_result_states`;
SELECT 'discovery_score_snapshots' AS table_name,COUNT(*) AS actual_rows,2 AS expected_rows,COUNT(*)=2 AS row_count_matches FROM `discovery_score_snapshots`;
SELECT 'discovery_score_snapshot_items' AS table_name,COUNT(*) AS actual_rows,5534 AS expected_rows,COUNT(*)=5534 AS row_count_matches FROM `discovery_score_snapshot_items`;
SELECT 'discovery_tasks' AS table_name,COUNT(*) AS actual_rows,188 AS expected_rows,COUNT(*)=188 AS row_count_matches FROM `discovery_tasks`;
SELECT 'etf_indicators' AS table_name,COUNT(*) AS actual_rows,345 AS expected_rows,COUNT(*)=345 AS row_count_matches FROM `etf_indicators`;
SELECT 'external_endpoint_runtime' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `external_endpoint_runtime`;
SELECT 'factors' AS table_name,COUNT(*) AS actual_rows,68 AS expected_rows,COUNT(*)=68 AS row_count_matches FROM `factors`;
SELECT 'factor_audit_logs' AS table_name,COUNT(*) AS actual_rows,647 AS expected_rows,COUNT(*)=647 AS row_count_matches FROM `factor_audit_logs`;
SELECT 'factor_data_validation_runs' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `factor_data_validation_runs`;
SELECT 'factor_drafts' AS table_name,COUNT(*) AS actual_rows,34 AS expected_rows,COUNT(*)=34 AS row_count_matches FROM `factor_drafts`;
SELECT 'factor_evaluation_runs' AS table_name,COUNT(*) AS actual_rows,16 AS expected_rows,COUNT(*)=16 AS row_count_matches FROM `factor_evaluation_runs`;
SELECT 'factor_experience' AS table_name,COUNT(*) AS actual_rows,7 AS expected_rows,COUNT(*)=7 AS row_count_matches FROM `factor_experience`;
SELECT 'factor_experience_field_deps' AS table_name,COUNT(*) AS actual_rows,7 AS expected_rows,COUNT(*)=7 AS row_count_matches FROM `factor_experience_field_deps`;
SELECT 'factor_experience_metrics' AS table_name,COUNT(*) AS actual_rows,7 AS expected_rows,COUNT(*)=7 AS row_count_matches FROM `factor_experience_metrics`;
SELECT 'factor_experience_tags' AS table_name,COUNT(*) AS actual_rows,26 AS expected_rows,COUNT(*)=26 AS row_count_matches FROM `factor_experience_tags`;
SELECT 'factor_formula_templates' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `factor_formula_templates`;
SELECT 'factor_grade_history' AS table_name,COUNT(*) AS actual_rows,4 AS expected_rows,COUNT(*)=4 AS row_count_matches FROM `factor_grade_history`;
SELECT 'factor_mining_candidates' AS table_name,COUNT(*) AS actual_rows,1172 AS expected_rows,COUNT(*)=1172 AS row_count_matches FROM `factor_mining_candidates`;
SELECT 'factor_mining_drafts' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `factor_mining_drafts`;
SELECT 'factor_mining_generations' AS table_name,COUNT(*) AS actual_rows,43 AS expected_rows,COUNT(*)=43 AS row_count_matches FROM `factor_mining_generations`;
SELECT 'factor_mining_prescreen_fingerprints' AS table_name,COUNT(*) AS actual_rows,1172 AS expected_rows,COUNT(*)=1172 AS row_count_matches FROM `factor_mining_prescreen_fingerprints`;
SELECT 'factor_mining_runs' AS table_name,COUNT(*) AS actual_rows,69 AS expected_rows,COUNT(*)=69 AS row_count_matches FROM `factor_mining_runs`;
SELECT 'factor_mining_templates' AS table_name,COUNT(*) AS actual_rows,25 AS expected_rows,COUNT(*)=25 AS row_count_matches FROM `factor_mining_templates`;
SELECT 'factor_mining_template_versions' AS table_name,COUNT(*) AS actual_rows,25 AS expected_rows,COUNT(*)=25 AS row_count_matches FROM `factor_mining_template_versions`;
SELECT 'factor_model_audit_logs' AS table_name,COUNT(*) AS actual_rows,17 AS expected_rows,COUNT(*)=17 AS row_count_matches FROM `factor_model_audit_logs`;
SELECT 'factor_model_members' AS table_name,COUNT(*) AS actual_rows,9 AS expected_rows,COUNT(*)=9 AS row_count_matches FROM `factor_model_members`;
SELECT 'factor_model_runs' AS table_name,COUNT(*) AS actual_rows,13 AS expected_rows,COUNT(*)=13 AS row_count_matches FROM `factor_model_runs`;
SELECT 'factor_quality_daily' AS table_name,COUNT(*) AS actual_rows,1 AS expected_rows,COUNT(*)=1 AS row_count_matches FROM `factor_quality_daily`;
SELECT 'factor_runtime_state' AS table_name,COUNT(*) AS actual_rows,1 AS expected_rows,COUNT(*)=1 AS row_count_matches FROM `factor_runtime_state`;
SELECT 'factor_sets' AS table_name,COUNT(*) AS actual_rows,6 AS expected_rows,COUNT(*)=6 AS row_count_matches FROM `factor_sets`;
SELECT 'factor_set_members' AS table_name,COUNT(*) AS actual_rows,17 AS expected_rows,COUNT(*)=17 AS row_count_matches FROM `factor_set_members`;
SELECT 'factor_set_snapshots' AS table_name,COUNT(*) AS actual_rows,7 AS expected_rows,COUNT(*)=7 AS row_count_matches FROM `factor_set_snapshots`;
SELECT 'factor_shadow_observations' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `factor_shadow_observations`;
SELECT 'factor_system_config' AS table_name,COUNT(*) AS actual_rows,1 AS expected_rows,COUNT(*)=1 AS row_count_matches FROM `factor_system_config`;
SELECT 'factor_training_checkpoints' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `factor_training_checkpoints`;
SELECT 'factor_transition_audits' AS table_name,COUNT(*) AS actual_rows,15 AS expected_rows,COUNT(*)=15 AS row_count_matches FROM `factor_transition_audits`;
SELECT 'factor_values' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `factor_values`;
SELECT 'factor_versions' AS table_name,COUNT(*) AS actual_rows,89 AS expected_rows,COUNT(*)=89 AS row_count_matches FROM `factor_versions`;
SELECT 'factor_weight_snapshots' AS table_name,COUNT(*) AS actual_rows,2 AS expected_rows,COUNT(*)=2 AS row_count_matches FROM `factor_weight_snapshots`;
SELECT 'financial_reports' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `financial_reports`;
SELECT 'g5_admission_controls' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `g5_admission_controls`;
SELECT 'g5_admission_events' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `g5_admission_events`;
SELECT 'g5_dual_run_reports' AS table_name,COUNT(*) AS actual_rows,10 AS expected_rows,COUNT(*)=10 AS row_count_matches FROM `g5_dual_run_reports`;
SELECT 'governance_events_outbox' AS table_name,COUNT(*) AS actual_rows,1 AS expected_rows,COUNT(*)=1 AS row_count_matches FROM `governance_events_outbox`;
SELECT 'hot_rank_snapshots' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `hot_rank_snapshots`;
SELECT 'idempotency_records' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `idempotency_records`;
SELECT 'index_prices' AS table_name,COUNT(*) AS actual_rows,12130 AS expected_rows,COUNT(*)=12130 AS row_count_matches FROM `index_prices`;
SELECT 'inspection_evidence_decisions' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `inspection_evidence_decisions`;
SELECT 'inspection_evidence_requests' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `inspection_evidence_requests`;
SELECT 'investment_themes' AS table_name,COUNT(*) AS actual_rows,1 AS expected_rows,COUNT(*)=1 AS row_count_matches FROM `investment_themes`;
SELECT 'journal_entries' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `journal_entries`;
SELECT 'lhb_institution_trades' AS table_name,COUNT(*) AS actual_rows,3695 AS expected_rows,COUNT(*)=3695 AS row_count_matches FROM `lhb_institution_trades`;
SELECT 'macro_data' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `macro_data`;
SELECT 'macro_indicator_values' AS table_name,COUNT(*) AS actual_rows,1468 AS expected_rows,COUNT(*)=1468 AS row_count_matches FROM `macro_indicator_values`;
SELECT 'macro_snapshots' AS table_name,COUNT(*) AS actual_rows,370 AS expected_rows,COUNT(*)=370 AS row_count_matches FROM `macro_snapshots`;
SELECT 'manual_price_overrides' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `manual_price_overrides`;
SELECT 'market_events' AS table_name,COUNT(*) AS actual_rows,2034 AS expected_rows,COUNT(*)=2034 AS row_count_matches FROM `market_events`;
SELECT 'news_events' AS table_name,COUNT(*) AS actual_rows,29070 AS expected_rows,COUNT(*)=29070 AS row_count_matches FROM `news_events`;
SELECT 'news_snapshots' AS table_name,COUNT(*) AS actual_rows,3352 AS expected_rows,COUNT(*)=3352 AS row_count_matches FROM `news_snapshots`;
SELECT 'northbound_flows' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `northbound_flows`;
SELECT 'notification_channels' AS table_name,COUNT(*) AS actual_rows,2 AS expected_rows,COUNT(*)=2 AS row_count_matches FROM `notification_channels`;
SELECT 'notification_deliveries' AS table_name,COUNT(*) AS actual_rows,58 AS expected_rows,COUNT(*)=58 AS row_count_matches FROM `notification_deliveries`;
SELECT 'notification_outbox' AS table_name,COUNT(*) AS actual_rows,44 AS expected_rows,COUNT(*)=44 AS row_count_matches FROM `notification_outbox`;
SELECT 'notification_policies' AS table_name,COUNT(*) AS actual_rows,1 AS expected_rows,COUNT(*)=1 AS row_count_matches FROM `notification_policies`;
SELECT 'notification_policy_channels' AS table_name,COUNT(*) AS actual_rows,2 AS expected_rows,COUNT(*)=2 AS row_count_matches FROM `notification_policy_channels`;
SELECT 'notification_templates' AS table_name,COUNT(*) AS actual_rows,1 AS expected_rows,COUNT(*)=1 AS row_count_matches FROM `notification_templates`;
SELECT 'opportunity_observation_notes' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `opportunity_observation_notes`;
SELECT 'opportunity_transition_events' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `opportunity_transition_events`;
SELECT 'outbox_events' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `outbox_events`;
SELECT 'portfolios' AS table_name,COUNT(*) AS actual_rows,14 AS expected_rows,COUNT(*)=14 AS row_count_matches FROM `portfolios`;
SELECT 'portfolio_account_baselines' AS table_name,COUNT(*) AS actual_rows,1 AS expected_rows,COUNT(*)=1 AS row_count_matches FROM `portfolio_account_baselines`;
SELECT 'portfolio_candidates' AS table_name,COUNT(*) AS actual_rows,4 AS expected_rows,COUNT(*)=4 AS row_count_matches FROM `portfolio_candidates`;
SELECT 'portfolio_cron_schedules' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `portfolio_cron_schedules`;
SELECT 'portfolio_equity_snapshots' AS table_name,COUNT(*) AS actual_rows,416 AS expected_rows,COUNT(*)=416 AS row_count_matches FROM `portfolio_equity_snapshots`;
SELECT 'portfolio_factor_usage' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `portfolio_factor_usage`;
SELECT 'portfolio_factor_usages' AS table_name,COUNT(*) AS actual_rows,5 AS expected_rows,COUNT(*)=5 AS row_count_matches FROM `portfolio_factor_usages`;
SELECT 'portfolio_members' AS table_name,COUNT(*) AS actual_rows,11 AS expected_rows,COUNT(*)=11 AS row_count_matches FROM `portfolio_members`;
SELECT 'portfolio_reconciliation_cursors' AS table_name,COUNT(*) AS actual_rows,1 AS expected_rows,COUNT(*)=1 AS row_count_matches FROM `portfolio_reconciliation_cursors`;
SELECT 'portfolio_reconciliation_days' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `portfolio_reconciliation_days`;
SELECT 'portfolio_reconciliation_reports' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `portfolio_reconciliation_reports`;
SELECT 'portfolio_reviews' AS table_name,COUNT(*) AS actual_rows,1 AS expected_rows,COUNT(*)=1 AS row_count_matches FROM `portfolio_reviews`;
SELECT 'portfolio_rules' AS table_name,COUNT(*) AS actual_rows,15 AS expected_rows,COUNT(*)=15 AS row_count_matches FROM `portfolio_rules`;
SELECT 'positions' AS table_name,COUNT(*) AS actual_rows,2 AS expected_rows,COUNT(*)=2 AS row_count_matches FROM `positions`;
SELECT 'reconciliation_recheck_requests' AS table_name,COUNT(*) AS actual_rows,1 AS expected_rows,COUNT(*)=1 AS row_count_matches FROM `reconciliation_recheck_requests`;
SELECT 'reconciliation_request_annotations' AS table_name,COUNT(*) AS actual_rows,1 AS expected_rows,COUNT(*)=1 AS row_count_matches FROM `reconciliation_request_annotations`;
SELECT 'reconciliation_source_events' AS table_name,COUNT(*) AS actual_rows,27 AS expected_rows,COUNT(*)=27 AS row_count_matches FROM `reconciliation_source_events`;
SELECT 'reviews' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `reviews`;
SELECT 'scan_presets' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `scan_presets`;
SELECT 'scan_results' AS table_name,COUNT(*) AS actual_rows,18851 AS expected_rows,COUNT(*)=18851 AS row_count_matches FROM `scan_results`;
SELECT 'scan_runs' AS table_name,COUNT(*) AS actual_rows,172 AS expected_rows,COUNT(*)=172 AS row_count_matches FROM `scan_runs`;
SELECT 'scheduled_tasks' AS table_name,COUNT(*) AS actual_rows,17 AS expected_rows,COUNT(*)=17 AS row_count_matches FROM `scheduled_tasks`;
SELECT 'scheduled_task_runs' AS table_name,COUNT(*) AS actual_rows,859 AS expected_rows,COUNT(*)=859 AS row_count_matches FROM `scheduled_task_runs`;
SELECT 'scheduled_task_seed_state' AS table_name,COUNT(*) AS actual_rows,8 AS expected_rows,COUNT(*)=8 AS row_count_matches FROM `scheduled_task_seed_state`;
SELECT 'scores' AS table_name,COUNT(*) AS actual_rows,53791 AS expected_rows,COUNT(*)=53791 AS row_count_matches FROM `scores`;
SELECT 'scoring_configs' AS table_name,COUNT(*) AS actual_rows,10 AS expected_rows,COUNT(*)=10 AS row_count_matches FROM `scoring_configs`;
SELECT 'scoring_lineage' AS table_name,COUNT(*) AS actual_rows,13 AS expected_rows,COUNT(*)=13 AS row_count_matches FROM `scoring_lineage`;
SELECT 'security_status_daily' AS table_name,COUNT(*) AS actual_rows,5337 AS expected_rows,COUNT(*)=5337 AS row_count_matches FROM `security_status_daily`;
SELECT 'signal_rules' AS table_name,COUNT(*) AS actual_rows,4 AS expected_rows,COUNT(*)=4 AS row_count_matches FROM `signal_rules`;
SELECT 'sim_accounts' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `sim_accounts`;
SELECT 'sim_orders' AS table_name,COUNT(*) AS actual_rows,10 AS expected_rows,COUNT(*)=10 AS row_count_matches FROM `sim_orders`;
SELECT 'sim_orders_attribution' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `sim_orders_attribution`;
SELECT 'sim_trades' AS table_name,COUNT(*) AS actual_rows,10 AS expected_rows,COUNT(*)=10 AS row_count_matches FROM `sim_trades`;
SELECT 'stock_financial_reports' AS table_name,COUNT(*) AS actual_rows,55940 AS expected_rows,COUNT(*)=55940 AS row_count_matches FROM `stock_financial_reports`;
SELECT 'stock_hot_rank_snapshots' AS table_name,COUNT(*) AS actual_rows,7136 AS expected_rows,COUNT(*)=7136 AS row_count_matches FROM `stock_hot_rank_snapshots`;
SELECT 'stock_valuations' AS table_name,COUNT(*) AS actual_rows,9469383 AS expected_rows,COUNT(*)=9469383 AS row_count_matches FROM `stock_valuations`;
SELECT 'strategy_execution_snapshots' AS table_name,COUNT(*) AS actual_rows,5 AS expected_rows,COUNT(*)=5 AS row_count_matches FROM `strategy_execution_snapshots`;
SELECT 'symbols' AS table_name,COUNT(*) AS actual_rows,7639 AS expected_rows,COUNT(*)=7639 AS row_count_matches FROM `symbols`;
SELECT 'symbol_theme_mappings' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `symbol_theme_mappings`;
SELECT 'tail_accumulation_snapshots' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `tail_accumulation_snapshots`;
SELECT 'task_idempotencies' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `task_idempotencies`;
SELECT 'task_locks' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `task_locks`;
SELECT 'theme_catalysts' AS table_name,COUNT(*) AS actual_rows,1 AS expected_rows,COUNT(*)=1 AS row_count_matches FROM `theme_catalysts`;
SELECT 'trade_calendar' AS table_name,COUNT(*) AS actual_rows,1999 AS expected_rows,COUNT(*)=1999 AS row_count_matches FROM `trade_calendar`;
SELECT 'trade_setups' AS table_name,COUNT(*) AS actual_rows,8026 AS expected_rows,COUNT(*)=8026 AS row_count_matches FROM `trade_setups`;
SELECT 'trade_signals' AS table_name,COUNT(*) AS actual_rows,0 AS expected_rows,COUNT(*)=0 AS row_count_matches FROM `trade_signals`;
SELECT 'training_candidate_pools' AS table_name,COUNT(*) AS actual_rows,7 AS expected_rows,COUNT(*)=7 AS row_count_matches FROM `training_candidate_pools`;
SELECT 'training_candidate_pool_members' AS table_name,COUNT(*) AS actual_rows,10371 AS expected_rows,COUNT(*)=10371 AS row_count_matches FROM `training_candidate_pool_members`;
SELECT 'training_candidate_pool_snapshots' AS table_name,COUNT(*) AS actual_rows,4 AS expected_rows,COUNT(*)=4 AS row_count_matches FROM `training_candidate_pool_snapshots`;
SELECT 'universe_daily_bars' AS table_name,COUNT(*) AS actual_rows,11734896 AS expected_rows,COUNT(*)=11734896 AS row_count_matches FROM `universe_daily_bars`;
SELECT 'universe_symbols' AS table_name,COUNT(*) AS actual_rows,7272 AS expected_rows,COUNT(*)=7272 AS row_count_matches FROM `universe_symbols`;
SELECT 'watchlists' AS table_name,COUNT(*) AS actual_rows,4 AS expected_rows,COUNT(*)=4 AS row_count_matches FROM `watchlists`;
SELECT 'watchlist_items' AS table_name,COUNT(*) AS actual_rows,7 AS expected_rows,COUNT(*)=7 AS row_count_matches FROM `watchlist_items`;

-- 关键字段：原 NULL/空串数量必须保持一致；既有缺失不计为本轮新增缺失。
SELECT
  'universe_symbols' AS table_name,
  COALESCE(SUM(`id` IS NULL),0) AS `id_nulls`,
  COALESCE(SUM(`id` IS NULL),0)=0 AS `id_nulls_match`,
  COALESCE(SUM(`symbol` IS NULL),0) AS `symbol_nulls`,
  COALESCE(SUM(`symbol` IS NULL),0)=0 AS `symbol_nulls_match`,
  COALESCE(SUM(`symbol` IS NOT NULL AND TRIM(`symbol`)=''),0)=0 AS `symbol_empty_match`,
  COALESCE(SUM(`asset_type` IS NULL),0) AS `asset_type_nulls`,
  COALESCE(SUM(`asset_type` IS NULL),0)=0 AS `asset_type_nulls_match`,
  COALESCE(SUM(`asset_type` IS NOT NULL AND TRIM(`asset_type`)=''),0)=0 AS `asset_type_empty_match`,
  COALESCE(SUM(`market` IS NULL),0) AS `market_nulls`,
  COALESCE(SUM(`market` IS NULL),0)=0 AS `market_nulls_match`,
  COALESCE(SUM(`market` IS NOT NULL AND TRIM(`market`)=''),0)=0 AS `market_empty_match`,
  COALESCE(SUM(`region` IS NULL),0) AS `region_nulls`,
  COALESCE(SUM(`region` IS NULL),0)=0 AS `region_nulls_match`,
  COALESCE(SUM(`region` IS NOT NULL AND TRIM(`region`)=''),0)=0 AS `region_empty_match`
FROM `universe_symbols`;
SELECT
  'symbols' AS table_name,
  COALESCE(SUM(`id` IS NULL),0) AS `id_nulls`,
  COALESCE(SUM(`id` IS NULL),0)=0 AS `id_nulls_match`,
  COALESCE(SUM(`symbol` IS NULL),0) AS `symbol_nulls`,
  COALESCE(SUM(`symbol` IS NULL),0)=0 AS `symbol_nulls_match`,
  COALESCE(SUM(`symbol` IS NOT NULL AND TRIM(`symbol`)=''),0)=0 AS `symbol_empty_match`,
  COALESCE(SUM(`asset_type` IS NULL),0) AS `asset_type_nulls`,
  COALESCE(SUM(`asset_type` IS NULL),0)=0 AS `asset_type_nulls_match`,
  COALESCE(SUM(`asset_type` IS NOT NULL AND TRIM(`asset_type`)=''),0)=0 AS `asset_type_empty_match`,
  COALESCE(SUM(`market` IS NULL),0) AS `market_nulls`,
  COALESCE(SUM(`market` IS NULL),0)=0 AS `market_nulls_match`,
  COALESCE(SUM(`market` IS NOT NULL AND TRIM(`market`)=''),0)=0 AS `market_empty_match`
FROM `symbols`;
SELECT
  'universe_daily_bars' AS table_name,
  COALESCE(SUM(`id` IS NULL),0) AS `id_nulls`,
  COALESCE(SUM(`id` IS NULL),0)=0 AS `id_nulls_match`,
  COALESCE(SUM(`universe_symbol_id` IS NULL),0) AS `universe_symbol_id_nulls`,
  COALESCE(SUM(`universe_symbol_id` IS NULL),0)=0 AS `universe_symbol_id_nulls_match`,
  COALESCE(SUM(`trade_date` IS NULL),0) AS `trade_date_nulls`,
  COALESCE(SUM(`trade_date` IS NULL),0)=0 AS `trade_date_nulls_match`,
  COALESCE(SUM(`open` IS NULL),0) AS `open_nulls`,
  COALESCE(SUM(`open` IS NULL),0)=0 AS `open_nulls_match`,
  COALESCE(SUM(`high` IS NULL),0) AS `high_nulls`,
  COALESCE(SUM(`high` IS NULL),0)=0 AS `high_nulls_match`,
  COALESCE(SUM(`low` IS NULL),0) AS `low_nulls`,
  COALESCE(SUM(`low` IS NULL),0)=0 AS `low_nulls_match`,
  COALESCE(SUM(`close` IS NULL),0) AS `close_nulls`,
  COALESCE(SUM(`close` IS NULL),0)=0 AS `close_nulls_match`,
  COALESCE(SUM(`source` IS NULL),0) AS `source_nulls`,
  COALESCE(SUM(`source` IS NULL),0)=0 AS `source_nulls_match`,
  COALESCE(SUM(`source` IS NOT NULL AND TRIM(`source`)=''),0)=0 AS `source_empty_match`,
  COALESCE(SUM(`volume` IS NULL),0) AS `volume_nulls`,
  COALESCE(SUM(`volume` IS NULL),0)=10898 AS `volume_nulls_match`,
  COALESCE(SUM(`amount` IS NULL),0) AS `amount_nulls`,
  COALESCE(SUM(`amount` IS NULL),0)=23957 AS `amount_nulls_match`,
  COALESCE(SUM(`turnover_rate` IS NULL),0) AS `turnover_rate_nulls`,
  COALESCE(SUM(`turnover_rate` IS NULL),0)=1096141 AS `turnover_rate_nulls_match`
FROM `universe_daily_bars`;
SELECT
  'daily_bars' AS table_name,
  COALESCE(SUM(`id` IS NULL),0) AS `id_nulls`,
  COALESCE(SUM(`id` IS NULL),0)=0 AS `id_nulls_match`,
  COALESCE(SUM(`symbol_id` IS NULL),0) AS `symbol_id_nulls`,
  COALESCE(SUM(`symbol_id` IS NULL),0)=0 AS `symbol_id_nulls_match`,
  COALESCE(SUM(`trade_date` IS NULL),0) AS `trade_date_nulls`,
  COALESCE(SUM(`trade_date` IS NULL),0)=0 AS `trade_date_nulls_match`,
  COALESCE(SUM(`open` IS NULL),0) AS `open_nulls`,
  COALESCE(SUM(`open` IS NULL),0)=0 AS `open_nulls_match`,
  COALESCE(SUM(`high` IS NULL),0) AS `high_nulls`,
  COALESCE(SUM(`high` IS NULL),0)=0 AS `high_nulls_match`,
  COALESCE(SUM(`low` IS NULL),0) AS `low_nulls`,
  COALESCE(SUM(`low` IS NULL),0)=0 AS `low_nulls_match`,
  COALESCE(SUM(`close` IS NULL),0) AS `close_nulls`,
  COALESCE(SUM(`close` IS NULL),0)=0 AS `close_nulls_match`,
  COALESCE(SUM(`source` IS NULL),0) AS `source_nulls`,
  COALESCE(SUM(`source` IS NULL),0)=0 AS `source_nulls_match`,
  COALESCE(SUM(`source` IS NOT NULL AND TRIM(`source`)=''),0)=0 AS `source_empty_match`,
  COALESCE(SUM(`volume` IS NULL),0) AS `volume_nulls`,
  COALESCE(SUM(`volume` IS NULL),0)=375 AS `volume_nulls_match`,
  COALESCE(SUM(`amount` IS NULL),0) AS `amount_nulls`,
  COALESCE(SUM(`amount` IS NULL),0)=427 AS `amount_nulls_match`,
  COALESCE(SUM(`turnover_rate` IS NULL),0) AS `turnover_rate_nulls`,
  COALESCE(SUM(`turnover_rate` IS NULL),0)=86433 AS `turnover_rate_nulls_match`
FROM `daily_bars`;
SELECT
  'stock_valuations' AS table_name,
  COALESCE(SUM(`id` IS NULL),0) AS `id_nulls`,
  COALESCE(SUM(`id` IS NULL),0)=0 AS `id_nulls_match`,
  COALESCE(SUM(`symbol_id` IS NULL),0) AS `symbol_id_nulls`,
  COALESCE(SUM(`symbol_id` IS NULL),0)=0 AS `symbol_id_nulls_match`,
  COALESCE(SUM(`trade_date` IS NULL),0) AS `trade_date_nulls`,
  COALESCE(SUM(`trade_date` IS NULL),0)=0 AS `trade_date_nulls_match`,
  COALESCE(SUM(`pe_ttm` IS NULL),0) AS `pe_ttm_nulls`,
  COALESCE(SUM(`pe_ttm` IS NULL),0)=0 AS `pe_ttm_nulls_match`,
  COALESCE(SUM(`pb` IS NULL),0) AS `pb_nulls`,
  COALESCE(SUM(`pb` IS NULL),0)=0 AS `pb_nulls_match`,
  COALESCE(SUM(`total_market_cap` IS NULL),0) AS `total_market_cap_nulls`,
  COALESCE(SUM(`total_market_cap` IS NULL),0)=0 AS `total_market_cap_nulls_match`,
  COALESCE(SUM(`circulating_market_cap` IS NULL),0) AS `circulating_market_cap_nulls`,
  COALESCE(SUM(`circulating_market_cap` IS NULL),0)=0 AS `circulating_market_cap_nulls_match`,
  COALESCE(SUM(`source` IS NULL),0) AS `source_nulls`,
  COALESCE(SUM(`source` IS NULL),0)=0 AS `source_nulls_match`,
  COALESCE(SUM(`source` IS NOT NULL AND TRIM(`source`)=''),0)=0 AS `source_empty_match`
FROM `stock_valuations`;
SELECT
  'capital_flows' AS table_name,
  COALESCE(SUM(`id` IS NULL),0) AS `id_nulls`,
  COALESCE(SUM(`id` IS NULL),0)=0 AS `id_nulls_match`,
  COALESCE(SUM(`symbol_id` IS NULL),0) AS `symbol_id_nulls`,
  COALESCE(SUM(`symbol_id` IS NULL),0)=0 AS `symbol_id_nulls_match`,
  COALESCE(SUM(`trade_date` IS NULL),0) AS `trade_date_nulls`,
  COALESCE(SUM(`trade_date` IS NULL),0)=0 AS `trade_date_nulls_match`,
  COALESCE(SUM(`main_net_inflow` IS NULL),0) AS `main_net_inflow_nulls`,
  COALESCE(SUM(`main_net_inflow` IS NULL),0)=0 AS `main_net_inflow_nulls_match`,
  COALESCE(SUM(`main_net_inflow_pct` IS NULL),0) AS `main_net_inflow_pct_nulls`,
  COALESCE(SUM(`main_net_inflow_pct` IS NULL),0)=0 AS `main_net_inflow_pct_nulls_match`,
  COALESCE(SUM(`super_large_net_inflow` IS NULL),0) AS `super_large_net_inflow_nulls`,
  COALESCE(SUM(`super_large_net_inflow` IS NULL),0)=0 AS `super_large_net_inflow_nulls_match`,
  COALESCE(SUM(`large_net_inflow` IS NULL),0) AS `large_net_inflow_nulls`,
  COALESCE(SUM(`large_net_inflow` IS NULL),0)=0 AS `large_net_inflow_nulls_match`,
  COALESCE(SUM(`medium_net_inflow` IS NULL),0) AS `medium_net_inflow_nulls`,
  COALESCE(SUM(`medium_net_inflow` IS NULL),0)=0 AS `medium_net_inflow_nulls_match`,
  COALESCE(SUM(`small_net_inflow` IS NULL),0) AS `small_net_inflow_nulls`,
  COALESCE(SUM(`small_net_inflow` IS NULL),0)=0 AS `small_net_inflow_nulls_match`,
  COALESCE(SUM(`source` IS NULL),0) AS `source_nulls`,
  COALESCE(SUM(`source` IS NULL),0)=0 AS `source_nulls_match`,
  COALESCE(SUM(`source` IS NOT NULL AND TRIM(`source`)=''),0)=0 AS `source_empty_match`
FROM `capital_flows`;
SELECT
  'stock_financial_reports' AS table_name,
  COALESCE(SUM(`id` IS NULL),0) AS `id_nulls`,
  COALESCE(SUM(`id` IS NULL),0)=0 AS `id_nulls_match`,
  COALESCE(SUM(`symbol_id` IS NULL),0) AS `symbol_id_nulls`,
  COALESCE(SUM(`symbol_id` IS NULL),0)=0 AS `symbol_id_nulls_match`,
  COALESCE(SUM(`report_period` IS NULL),0) AS `report_period_nulls`,
  COALESCE(SUM(`report_period` IS NULL),0)=0 AS `report_period_nulls_match`,
  COALESCE(SUM(`announcement_date` IS NULL),0) AS `announcement_date_nulls`,
  COALESCE(SUM(`announcement_date` IS NULL),0)=0 AS `announcement_date_nulls_match`,
  COALESCE(SUM(`report_type` IS NULL),0) AS `report_type_nulls`,
  COALESCE(SUM(`report_type` IS NULL),0)=0 AS `report_type_nulls_match`,
  COALESCE(SUM(`report_type` IS NOT NULL AND TRIM(`report_type`)=''),0)=0 AS `report_type_empty_match`,
  COALESCE(SUM(`net_profit` IS NULL),0) AS `net_profit_nulls`,
  COALESCE(SUM(`net_profit` IS NULL),0)=54789 AS `net_profit_nulls_match`,
  COALESCE(SUM(`revenue` IS NULL),0) AS `revenue_nulls`,
  COALESCE(SUM(`revenue` IS NULL),0)=54789 AS `revenue_nulls_match`,
  COALESCE(SUM(`net_profit_yoy` IS NULL),0) AS `net_profit_yoy_nulls`,
  COALESCE(SUM(`net_profit_yoy` IS NULL),0)=54789 AS `net_profit_yoy_nulls_match`,
  COALESCE(SUM(`revenue_yoy` IS NULL),0) AS `revenue_yoy_nulls`,
  COALESCE(SUM(`revenue_yoy` IS NULL),0)=54789 AS `revenue_yoy_nulls_match`,
  COALESCE(SUM(`source` IS NULL),0) AS `source_nulls`,
  COALESCE(SUM(`source` IS NULL),0)=0 AS `source_nulls_match`,
  COALESCE(SUM(`source` IS NOT NULL AND TRIM(`source`)=''),0)=0 AS `source_empty_match`
FROM `stock_financial_reports`;
SELECT
  'index_prices' AS table_name,
  COALESCE(SUM(`id` IS NULL),0) AS `id_nulls`,
  COALESCE(SUM(`id` IS NULL),0)=0 AS `id_nulls_match`,
  COALESCE(SUM(`symbol` IS NULL),0) AS `symbol_nulls`,
  COALESCE(SUM(`symbol` IS NULL),0)=0 AS `symbol_nulls_match`,
  COALESCE(SUM(`symbol` IS NOT NULL AND TRIM(`symbol`)=''),0)=0 AS `symbol_empty_match`,
  COALESCE(SUM(`trade_date` IS NULL),0) AS `trade_date_nulls`,
  COALESCE(SUM(`trade_date` IS NULL),0)=0 AS `trade_date_nulls_match`,
  COALESCE(SUM(`open` IS NULL),0) AS `open_nulls`,
  COALESCE(SUM(`open` IS NULL),0)=0 AS `open_nulls_match`,
  COALESCE(SUM(`high` IS NULL),0) AS `high_nulls`,
  COALESCE(SUM(`high` IS NULL),0)=0 AS `high_nulls_match`,
  COALESCE(SUM(`low` IS NULL),0) AS `low_nulls`,
  COALESCE(SUM(`low` IS NULL),0)=0 AS `low_nulls_match`,
  COALESCE(SUM(`close` IS NULL),0) AS `close_nulls`,
  COALESCE(SUM(`close` IS NULL),0)=0 AS `close_nulls_match`,
  COALESCE(SUM(`source` IS NULL),0) AS `source_nulls`,
  COALESCE(SUM(`source` IS NULL),0)=0 AS `source_nulls_match`,
  COALESCE(SUM(`source` IS NOT NULL AND TRIM(`source`)=''),0)=0 AS `source_empty_match`
FROM `index_prices`;
SELECT
  'stock_hot_rank_snapshots' AS table_name,
  COALESCE(SUM(`id` IS NULL),0) AS `id_nulls`,
  COALESCE(SUM(`id` IS NULL),0)=0 AS `id_nulls_match`,
  COALESCE(SUM(`symbol` IS NULL),0) AS `symbol_nulls`,
  COALESCE(SUM(`symbol` IS NULL),0)=0 AS `symbol_nulls_match`,
  COALESCE(SUM(`symbol` IS NOT NULL AND TRIM(`symbol`)=''),0)=0 AS `symbol_empty_match`,
  COALESCE(SUM(`trade_date` IS NULL),0) AS `trade_date_nulls`,
  COALESCE(SUM(`trade_date` IS NULL),0)=0 AS `trade_date_nulls_match`,
  COALESCE(SUM(`hot_rank` IS NULL),0) AS `hot_rank_nulls`,
  COALESCE(SUM(`hot_rank` IS NULL),0)=0 AS `hot_rank_nulls_match`,
  COALESCE(SUM(`hot_rank_total` IS NULL),0) AS `hot_rank_total_nulls`,
  COALESCE(SUM(`hot_rank_total` IS NULL),0)=0 AS `hot_rank_total_nulls_match`,
  COALESCE(SUM(`hot_rank_pct` IS NULL),0) AS `hot_rank_pct_nulls`,
  COALESCE(SUM(`hot_rank_pct` IS NULL),0)=0 AS `hot_rank_pct_nulls_match`,
  COALESCE(SUM(`source` IS NULL),0) AS `source_nulls`,
  COALESCE(SUM(`source` IS NULL),0)=0 AS `source_nulls_match`,
  COALESCE(SUM(`source` IS NOT NULL AND TRIM(`source`)=''),0)=0 AS `source_empty_match`
FROM `stock_hot_rank_snapshots`;
SELECT
  'etf_indicators' AS table_name,
  COALESCE(SUM(`id` IS NULL),0) AS `id_nulls`,
  COALESCE(SUM(`id` IS NULL),0)=0 AS `id_nulls_match`,
  COALESCE(SUM(`symbol_id` IS NULL),0) AS `symbol_id_nulls`,
  COALESCE(SUM(`symbol_id` IS NULL),0)=0 AS `symbol_id_nulls_match`,
  COALESCE(SUM(`trade_date` IS NULL),0) AS `trade_date_nulls`,
  COALESCE(SUM(`trade_date` IS NULL),0)=0 AS `trade_date_nulls_match`,
  COALESCE(SUM(`source` IS NULL),0) AS `source_nulls`,
  COALESCE(SUM(`source` IS NULL),0)=0 AS `source_nulls_match`,
  COALESCE(SUM(`source` IS NOT NULL AND TRIM(`source`)=''),0)=0 AS `source_empty_match`
FROM `etf_indicators`;
SELECT
  'lhb_institution_trades' AS table_name,
  COALESCE(SUM(`id` IS NULL),0) AS `id_nulls`,
  COALESCE(SUM(`id` IS NULL),0)=0 AS `id_nulls_match`,
  COALESCE(SUM(`symbol` IS NULL),0) AS `symbol_nulls`,
  COALESCE(SUM(`symbol` IS NULL),0)=0 AS `symbol_nulls_match`,
  COALESCE(SUM(`symbol` IS NOT NULL AND TRIM(`symbol`)=''),0)=0 AS `symbol_empty_match`,
  COALESCE(SUM(`trade_date` IS NULL),0) AS `trade_date_nulls`,
  COALESCE(SUM(`trade_date` IS NULL),0)=0 AS `trade_date_nulls_match`,
  COALESCE(SUM(`source` IS NULL),0) AS `source_nulls`,
  COALESCE(SUM(`source` IS NULL),0)=0 AS `source_nulls_match`,
  COALESCE(SUM(`source` IS NOT NULL AND TRIM(`source`)=''),0)=0 AS `source_empty_match`
FROM `lhb_institution_trades`;
SELECT
  'security_status_daily' AS table_name,
  COALESCE(SUM(`id` IS NULL),0) AS `id_nulls`,
  COALESCE(SUM(`id` IS NULL),0)=0 AS `id_nulls_match`,
  COALESCE(SUM(`symbol_id` IS NULL),0) AS `symbol_id_nulls`,
  COALESCE(SUM(`symbol_id` IS NULL),0)=0 AS `symbol_id_nulls_match`,
  COALESCE(SUM(`trade_date` IS NULL),0) AS `trade_date_nulls`,
  COALESCE(SUM(`trade_date` IS NULL),0)=0 AS `trade_date_nulls_match`
FROM `security_status_daily`;

-- 本轮缓存修复后，下方差异查询应返回零行。
SELECT u.id,u.symbol,u.listed_at,u.last_bar_date,u.bar_count,COUNT(b.id) AS actual_bar_count,MAX(b.trade_date) AS actual_last_bar_date
FROM universe_symbols u LEFT JOIN universe_daily_bars b ON b.universe_symbol_id=u.id
WHERE u.region='cn' AND u.asset_type='etf'
GROUP BY u.id,u.symbol,u.listed_at,u.last_bar_date,u.bar_count
HAVING NOT (u.last_bar_date <=> MAX(b.trade_date)) OR NOT (u.bar_count <=> COUNT(b.id));
SELECT COUNT(*) AS etf_symbols,SUM(listed_at IS NULL) AS missing_listing_dates FROM universe_symbols WHERE region='cn' AND asset_type='etf';


-- 仅核对已授权的缓存结果及三条登记记录的其他全部原字段。
SELECT 6549 AS symbol_id,COUNT(*)=1 AS cache_matches FROM universe_symbols WHERE id=6549 AND bar_count=971 AND last_bar_date='2026-07-01';
SELECT 6549 AS symbol_id,COUNT(*)=1 AS other_fields_match FROM universe_symbols WHERE `id` <=> 6549 AND `symbol` <=> '560650' AND `name` <=> '核心50ETF民生加银' AND `asset_type` <=> 'etf' AND `market` <=> 'sh' AND `region` <=> 'cn' AND `board` <=> NULL AND `industry` <=> NULL AND `listed_at` <=> NULL AND `last_synced_at` <=> '2026-10-23 11:51:51' AND `is_synced` <=> 1 AND `sync_failed` <=> 1 AND `created_at` <=> '2026-07-08 14:52:25' AND `updated_at` <=> '2026-10-23 11:51:51';
SELECT 6604 AS symbol_id,COUNT(*)=1 AS cache_matches FROM universe_symbols WHERE id=6604 AND bar_count=1108 AND last_bar_date='2026-04-30';
SELECT 6604 AS symbol_id,COUNT(*)=1 AS other_fields_match FROM universe_symbols WHERE `id` <=> 6604 AND `symbol` <=> '560000' AND `name` <=> '智能电车ETF浦银' AND `asset_type` <=> 'etf' AND `market` <=> 'sh' AND `region` <=> 'cn' AND `board` <=> NULL AND `industry` <=> NULL AND `listed_at` <=> NULL AND `last_synced_at` <=> '2026-10-23 11:51:51' AND `is_synced` <=> 1 AND `sync_failed` <=> 1 AND `created_at` <=> '2026-07-08 14:52:25' AND `updated_at` <=> '2026-10-23 11:51:51';
SELECT 7098 AS symbol_id,COUNT(*)=1 AS cache_matches FROM universe_symbols WHERE id=7098 AND bar_count=1280 AND last_bar_date='2026-10-23';
SELECT 7098 AS symbol_id,COUNT(*)=1 AS other_fields_match FROM universe_symbols WHERE `id` <=> 7098 AND `symbol` <=> '510300' AND `name` <=> '沪深300ETF华泰柏瑞' AND `asset_type` <=> 'etf' AND `market` <=> 'sh' AND `region` <=> 'cn' AND `board` <=> NULL AND `industry` <=> NULL AND `listed_at` <=> NULL AND `last_synced_at` <=> '2026-10-21 06:22:56' AND `is_synced` <=> 1 AND `sync_failed` <=> 0 AND `created_at` <=> '2026-07-08 14:52:25' AND `updated_at` <=> '2026-10-21 06:22:56';
SELECT COUNT(*)=0 AS all_etf_caches_match FROM (SELECT u.id FROM universe_symbols u LEFT JOIN universe_daily_bars b ON b.universe_symbol_id=u.id WHERE u.region='cn' AND u.asset_type='etf' GROUP BY u.id,u.bar_count,u.last_bar_date HAVING NOT (u.bar_count <=> COUNT(b.id)) OR NOT (u.last_bar_date <=> MAX(b.trade_date))) differences;
SELECT COUNT(*)=1 AND MIN(version_num)='wps_0024_082_status_dt6' AS revision_matches FROM alembic_version;
SELECT COUNT(*)=0 AS idle_matches FROM async_tasks WHERE status IN ('running','queued');
ROLLBACK;
