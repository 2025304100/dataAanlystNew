"""BFG (Backtest Filter Governance) 服务包入口。

显式导出：
- config: BacktestFilterConfig + compute_config_hash + validate_production_fidelity
- bridge: pre_rebalance_pipeline 与 Input/Output DTO
- alerts_registry: FILTER_GOVERNANCE 告警注册与 post-run emit 钩子

接入指南：
  1) 启动 / 初始化时调用 register_filter_governance_alerts()
     完成 3 条 outbox 告警规则的元数据注册（返回 rule_code 集合）。

  2) 每次回测 run 结束后（主循环 finalize 阶段），在同一 session 事务内
     调用 emit_filter_alerts_post_run(...) 写入 outbox：

         from app.services.backtest_filters import (
             register_filter_governance_alerts,
             emit_filter_alerts_post_run,
         )
         register_filter_governance_alerts()   # 应用启动时 1 次
         ...
         # run 结束（后处理阶段）：
         alert_triggered = emit_filter_alerts_post_run(
             db=session,
             run_id=run.id,
             unknown_ratio=stats.unknown_ratio,
             unknown_count=stats.unknown_count,
             total_count=stats.total_count,
             trade_date=stats.last_trade_date,
             delisting_price_missing_symbols=stats.delisting_missing_ids,
             status_batch_p95_ms=stats.pit_batch_p95_ms,
             status_batch_p50_ms=stats.pit_batch_p50_ms,
             status_batch_max_ms=stats.pit_batch_max_ms,
             symbols_batch_size=stats.batch_symbols_n,
             days_in_run=stats.days_processed,
         )

  3) legacy_baseline 回放：构造 BacktestFilterConfig(engine_compat_version="legacy_baseline")
     传入 bridge.pre_rebalance_pipeline，会完全跳过过滤/清算/PIT，
     直接返回原始 candidates（用于和旧基线 1:1 对齐对比）。
"""
from __future__ import annotations

from app.services.backtest_filters.config import (
    BacktestFilterConfig,
    compute_config_hash,
    validate_production_fidelity,
)
from app.services.backtest_filters.bridge import (
    pre_rebalance_pipeline,
    PipelineRebalanceInput,
    PipelineRebalanceOutput,
)
from app.services.backtest_filters.alerts_registry import (
    ALERT_CATEGORY_FILTER_GOVERNANCE,
    RULE_STATUS_UNKNOWN_RATIO_HIGH,
    RULE_DELISTING_PRICE_MISSING,
    RULE_PIT_STATUS_BATCH_P95_SLOW,
    AlertRuleSpec,
    register_filter_governance_alerts,
    get_rule_specs,
    emit_filter_alerts_post_run,
    get_memory_outbox,
    clear_memory_outbox,
)

__all__ = [
    # config
    "BacktestFilterConfig",
    "compute_config_hash",
    "validate_production_fidelity",
    # bridge
    "pre_rebalance_pipeline",
    "PipelineRebalanceInput",
    "PipelineRebalanceOutput",
    # alerts_registry (Task 28)
    "ALERT_CATEGORY_FILTER_GOVERNANCE",
    "RULE_STATUS_UNKNOWN_RATIO_HIGH",
    "RULE_DELISTING_PRICE_MISSING",
    "RULE_PIT_STATUS_BATCH_P95_SLOW",
    "AlertRuleSpec",
    "register_filter_governance_alerts",
    "get_rule_specs",
    "emit_filter_alerts_post_run",
    "get_memory_outbox",
    "clear_memory_outbox",
]