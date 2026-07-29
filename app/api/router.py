from fastapi import APIRouter

from app.api.routes import alerts, auto_trade, backtest, custom_indicators, dashboard, db_config, discovery, discovery_plans, external_data, akshare_apis, factor_models, factor_pipeline, factors, journals, linkage, macro, market_data, market_events, news, notifications, portfolios, scheduled_tasks, scans, scoring_configs, scores, signal_rules, sim_accounts, symbols, system, trade_setups, watchlists, universe, ai_config, ai_profiles, ai_sessions
from app.core.config import settings


api_router = APIRouter(prefix=settings.api_prefix)
api_router.include_router(factors.router, tags=['factors'])
api_router.include_router(factor_models.router, tags=['factor-models'])
api_router.include_router(factor_pipeline.router, tags=['factor-pipeline'])
api_router.include_router(scheduled_tasks.router, tags=["scheduled-tasks"])
api_router.include_router(symbols.router, tags=["symbols"])
api_router.include_router(watchlists.router, tags=["watchlists"])
api_router.include_router(market_data.router, tags=["market-data"])
api_router.include_router(news.router, tags=["news"])
api_router.include_router(macro.router, tags=["macro"])
api_router.include_router(market_events.router, tags=["market-events"])
api_router.include_router(discovery.router, tags=["discovery"])
api_router.include_router(portfolios.router, tags=["portfolios"])
api_router.include_router(sim_accounts.router, tags=["sim-accounts"])
# WP6.6：自动交易双跑与成员级状态路由（/portfolios/{id}/auto-trade/*）
api_router.include_router(auto_trade.router, tags=["auto-trade"])
api_router.include_router(signal_rules.router, tags=["signal-rules"])
api_router.include_router(scores.router, tags=["scores"])
api_router.include_router(scans.router, tags=["scans"])
api_router.include_router(trade_setups.router, tags=["trade-setups"])
api_router.include_router(journals.router, tags=["journals"])
api_router.include_router(dashboard.router, tags=["dashboard"])
api_router.include_router(system.router, tags=["system"])
api_router.include_router(db_config.router, tags=["settings"])
api_router.include_router(custom_indicators.router, tags=["settings"])
api_router.include_router(discovery_plans.router, tags=["settings"])
api_router.include_router(scoring_configs.router, tags=["settings"])
# P2：外部数据同步（估值/资金流/ETF 指标）
api_router.include_router(external_data.router, tags=["external-data"])
# P2-E：第三方接口管理（状态查看 + 防风控策略配置）
api_router.include_router(akshare_apis.router, tags=["external-data"])
api_router.include_router(backtest.router, tags=["backtest"])
api_router.include_router(alerts.router, tags=["alerts"])
# 基础数据隔离层：全市场标的 + K线初始化同步
api_router.include_router(universe.router, tags=["universe"])
# AI 接口配置与对话代理
api_router.include_router(ai_config.router, tags=["settings"])
# WP-AI.2：AI Profile 多 Profile 主备降级管理
api_router.include_router(ai_profiles.router, tags=["ai-profiles"])
# WP8.2：跨模块联动 API（订单/成交/告警/今日决策/回测成员快照/归因建议复盘）
api_router.include_router(linkage.router, tags=["linkage"])
# WP-AI.6：AI 会话与审计管理
api_router.include_router(ai_sessions.router, tags=["ai-sessions"])
# WP-MSG.6：通知消息管理（渠道/策略/模板/发送记录）
api_router.include_router(notifications.router, tags=["notifications"])
