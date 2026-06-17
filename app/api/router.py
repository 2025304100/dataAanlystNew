from fastapi import APIRouter

from app.api.routes import dashboard, discovery, journals, market_data, news, portfolios, scans, scores, signal_rules, sim_accounts, symbols, trade_setups, watchlists
from app.core.config import settings


api_router = APIRouter(prefix=settings.api_prefix)
api_router.include_router(symbols.router, tags=["symbols"])
api_router.include_router(watchlists.router, tags=["watchlists"])
api_router.include_router(market_data.router, tags=["market-data"])
api_router.include_router(news.router, tags=["news"])
api_router.include_router(discovery.router, tags=["discovery"])
api_router.include_router(portfolios.router, tags=["portfolios"])
api_router.include_router(sim_accounts.router, tags=["sim-accounts"])
api_router.include_router(signal_rules.router, tags=["signal-rules"])
api_router.include_router(scores.router, tags=["scores"])
api_router.include_router(scans.router, tags=["scans"])
api_router.include_router(trade_setups.router, tags=["trade-setups"])
api_router.include_router(journals.router, tags=["journals"])
api_router.include_router(dashboard.router, tags=["dashboard"])
