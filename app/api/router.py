from fastapi import APIRouter

from app.api.routes import alerts, backtest, custom_indicators, dashboard, db_config, discovery, discovery_plans, external_data, akshare_apis, journals, macro, market_data, market_events, news, portfolios, scans, scoring_configs, scores, signal_rules, sim_accounts, symbols, system, trade_setups, watchlists, universe
from app.core.config import settings


api_router = APIRouter(prefix=settings.api_prefix)
api_router.include_router(symbols.router, tags=["symbols"])
api_router.include_router(watchlists.router, tags=["watchlists"])
api_router.include_router(market_data.router, tags=["market-data"])
api_router.include_router(news.router, tags=["news"])
api_router.include_router(macro.router, tags=["macro"])
api_router.include_router(market_events.router, tags=["market-events"])
api_router.include_router(discovery.router, tags=["discovery"])
api_router.include_router(portfolios.router, tags=["portfolios"])
api_router.include_router(sim_accounts.router, tags=["sim-accounts"])
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
