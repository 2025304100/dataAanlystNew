"""app.models 包：自动扫描本目录下所有子模块并 import，确保 Base.metadata 注册完整。

设计意图：
- 新增 ORM 模型文件（如 app/models/my_new_module.py）后，无需手动修改本文件或
  init_db.py 的 import 列表；启动时自动扫描 + 导入，Base.metadata.create_all 与
  通用对齐器即可自动识别新表/新列。
- 与之前的显式 import 列表共存：显式 import 仍然保留用于文档/追溯；自动扫描作为
  兜底补齐层，防止漏写。
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import List

# ── 显式 import（保留，便于追溯各模块引入背景） ─────────────────────────
from app.models import async_task, backtest, market_event
from app.models import custom_indicator, discovery_plan
from app.models import scoring_config
from app.models import factor, factor_model, factor_runtime, scheduled_task
from app.models import factor_evaluation  # noqa: F401
# P2：外部数据因子表（股票估值 / 资金流 / ETF 指标）
from app.models import (
    capital_flow,
    etf_indicator,
    financial_report,
    hot_rank_snapshot,
    lhb_institution_trade,
    stock_valuation,
    tail_accumulation_snapshot,
)
# P2-E：第三方接口管理配置表
from app.models import akshare_api_config
# 基础数据隔离层：全市场标的元数据 + K线 + 挖掘结果独立存储
from app.models import universe, discovery_candidate
# P0-8：组合每日净值快照（绩效统计基础数据）
from app.models import portfolio_equity_snapshot
# P3+：市场指数日线（Benchmark 对比曲线基础设施）
from app.models import index_price
# WP-S：外部接口运行时状态（熔断器 + 计数器）
from app.models import external_endpoint_runtime
# WP-P.2：评分快照（挖掘性能改造基础）
from app.models import discovery_score_snapshot
# WP3.1：机会状态流转审计事件
from app.models import opportunity_transition_event
# WP4.1：组合成员
from app.models import portfolio_member
# 组合专属候选池（当前组合回测/成员页使用）
from app.models import portfolio_candidate
# WP-MSG.1：通知数据模型
from app.models import notification
# WP-AI.2：AI Profile 多 Profile 主备降级
from app.models import ai_profile
# WP8：组合复盘记录（绩效归因）
from app.models import review
# WP-AI.1：AI 会话与审计数据模型
from app.models import ai_session
# WP9.6：API 废弃访问日志
from app.models import api_deprecation_log
# G0-WP0-2a：决策引擎核心表（契约快照+运行+逐证券证据+因子绑定）
from app.models import decision_engine  # noqa: F401
from app.models.decision_engine import (  # noqa: F401
    DecisionEvidence,
    DecisionRun,
    DecisionOrderPlanRecord,
    PortfolioFactorUsage,
    StrategyExecutionSnapshot,
)
# G0-WP0-2d：幂等记录 + 组合调度时间表（防重 + 默认20:30调度）
from app.models import idempotency_record, portfolio_cron_schedule  # noqa: F401


# ── 兜底：自动扫描本目录所有 .py 模块并 import（新模块无需手动加） ──────
def _auto_discover_models() -> List[str]:
    """自动扫描 app.models 包下所有非下划线开头的模块并 import。

    返回值：本次自动导入的模块名列表（仅用于日志/调试）。
    """
    imported: List[str] = []
    pkg_path = Path(__file__).resolve().parent

    for module_info in pkgutil.iter_modules([str(pkg_path)]):
        mod_name = module_info.name
        # 跳过显式导入过的模块（避免重复副作用）+ 私有模块
        if mod_name.startswith("_") or mod_name == "strategy_execution_snapshot":
            continue
        full_mod_name = f"app.models.{mod_name}"
        try:
            importlib.import_module(full_mod_name)
            imported.append(mod_name)
        except Exception:
            # 导入失败不阻塞启动：可能模块内部有可选依赖；显式 import 层若该模块真正
            # 被使用会再抛一次真实异常，这里只做 best-effort。
            continue
    return imported


# 模块被 import 时立即执行一次自动扫描，保证 Base.metadata 注册完整
_AUTO_IMPORTED = _auto_discover_models()

__all__ = [
    "async_task", "backtest", "market_event", "custom_indicator",
    "discovery_plan", "scoring_config", "factor", "factor_model",
    "factor_runtime", "scheduled_task", "factor_evaluation", "capital_flow",
    "etf_indicator", "financial_report", "hot_rank_snapshot",
    "lhb_institution_trade", "stock_valuation", "tail_accumulation_snapshot",
    "akshare_api_config", "universe", "discovery_candidate",
    "portfolio_equity_snapshot", "index_price", "external_endpoint_runtime",
    "discovery_score_snapshot", "opportunity_transition_event",
    "portfolio_member", "portfolio_candidate", "notification", "ai_profile", "review",
    "ai_session", "api_deprecation_log",
    # G0-WP0-2a
    "decision_engine",
    "DecisionEvidence", "DecisionRun", "DecisionOrderPlanRecord",
    "StrategyExecutionSnapshot", "PortfolioFactorUsage",
    # G0-WP0-2d
    "idempotency_record", "portfolio_cron_schedule",
]
