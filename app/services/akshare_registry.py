"""第三方 akshare 接口注册表与防风控策略管理。

本模块是「接口管理」功能的核心：
1. AKSHARE_API_REGISTRY：所有第三方接口的元数据（不可修改），用于前端展示
2. ANTI_RISK_STRATEGIES：防风控策略档位（用户可选），每档对应不同延时区间和重试次数
3. 内存配置缓存 + DB 持久化：runtime 配置变更后即时生效，无需重启
4. apply_delay()：在调用 akshare 前根据 api_key 配置 sleep 随机延时，规避风控

调用方使用方式：
    from app.services.akshare_utils import call_akshare_with_retry
    df = call_akshare_with_retry(ak.stock_zh_a_spot_em, api_key="stock_zh_a_spot_em")
"""
from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.akshare_api_config import AkshareApiConfig

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# 防风控策略档位（用户可选）
# ----------------------------------------------------------------------------
# 每档对应：延时区间（毫秒）+ 最大重试次数 + 描述
# - fast：100-300ms，适合局域网/私有接口，重试 2 次
# - standard：300-800ms，默认档位，适合大多数公开接口，重试 3 次
# - conservative：1000-2000ms，适合易风控接口（如东财实时行情），重试 3 次
# - extreme：3000-5000ms，适合严重风控场景，重试 5 次
# - custom：用户自定义延时区间（delay_min_ms/delay_max_ms），重试 3 次
# ----------------------------------------------------------------------------
ANTI_RISK_STRATEGIES: dict[str, dict[str, Any]] = {
    "fast": {
        "key": "fast",
        "name_zh": "快速",
        "name_en": "Fast",
        "delay_min_ms": 100,
        "delay_max_ms": 300,
        "max_retries": 2,
        "desc_zh": "延时 100-300ms，适合私有/局域网接口",
        "desc_en": "Delay 100-300ms, for private/LAN APIs",
    },
    "standard": {
        "key": "standard",
        "name_zh": "标准",
        "name_en": "Standard",
        "delay_min_ms": 300,
        "delay_max_ms": 800,
        "max_retries": 3,
        "desc_zh": "延时 300-800ms，默认档位，适合大多数公开接口",
        "desc_en": "Delay 300-800ms, default, for most public APIs",
    },
    "conservative": {
        "key": "conservative",
        "name_zh": "保守",
        "name_en": "Conservative",
        "delay_min_ms": 1000,
        "delay_max_ms": 2000,
        "max_retries": 3,
        "desc_zh": "延时 1-2s，适合易风控接口（东财实时行情等）",
        "desc_en": "Delay 1-2s, for rate-limit-prone APIs (e.g. EastMoney realtime)",
    },
    "extreme": {
        "key": "extreme",
        "name_zh": "极保守",
        "name_en": "Extreme",
        "delay_min_ms": 3000,
        "delay_max_ms": 5000,
        "max_retries": 5,
        "desc_zh": "延时 3-5s，适合严重风控场景",
        "desc_en": "Delay 3-5s, for heavily rate-limited scenarios",
    },
    "custom": {
        "key": "custom",
        "name_zh": "自定义",
        "name_en": "Custom",
        "delay_min_ms": None,  # 由用户配置 delay_min_ms/delay_max_ms 决定
        "delay_max_ms": None,
        "max_retries": 3,
        "desc_zh": "用户自定义延时区间",
        "desc_en": "User-defined delay range",
    },
}

DEFAULT_STRATEGY = "standard"


# ----------------------------------------------------------------------------
# 接口元数据注册表（不可修改）
# ----------------------------------------------------------------------------
# category 分类：行情历史 / 实时行情 / ETF专项 / 资金流 / 个股信息 / 新闻公告 / 标的元数据
# default_strategy：该接口推荐默认档位（用户仍可调整）
# probe_args：探测时使用的最小参数（避免拉全量数据）
# ----------------------------------------------------------------------------
AKSHARE_API_REGISTRY: list[dict[str, Any]] = [
    # ── 行情历史（K线） ──
    {
        "key": "stock_zh_a_hist",
        "name_zh": "A股日K（东财）",
        "name_en": "A-share Daily K-line (EastMoney)",
        "category_zh": "行情历史",
        "category_en": "History",
        "module": "market_data",
        "desc_zh": "A股日K线主数据源",
        "desc_en": "A-share daily K-line primary source",
        "default_strategy": "standard",
        "probe_args": {"symbol": "000001", "period": "daily", "start_date": "20240101", "end_date": "20240110", "adjust": "qfq"},
    },
    {
        "key": "stock_zh_a_daily",
        "name_zh": "A股日K（新浪）",
        "name_en": "A-share Daily K-line (Sina)",
        "category_zh": "行情历史",
        "category_en": "History",
        "module": "market_data",
        "desc_zh": "A股日K线备用数据源",
        "desc_en": "A-share daily K-line fallback source",
        "default_strategy": "standard",
        "probe_args": {"symbol": "sz000001", "start_date": "20240101", "end_date": "20240110", "adjust": "qfq"},
    },
    {
        "key": "stock_zh_a_hist_tx",
        "name_zh": "A股日K（腾讯）",
        "name_en": "A-share Daily K-line (Tencent)",
        "category_zh": "行情历史",
        "category_en": "History",
        "module": "market_data",
        "desc_zh": "A股日K线备用数据源",
        "desc_en": "A-share daily K-line fallback source",
        "default_strategy": "standard",
        "probe_args": {"symbol": "sz000001", "start_date": "20240101", "end_date": "20240110", "adjust": "qfq"},
    },
    {
        "key": "index_zh_a_hist",
        "name_zh": "指数日K（东财）",
        "name_en": "Index Daily K-line (EastMoney)",
        "category_zh": "行情历史",
        "category_en": "History",
        "module": "index_data",
        "desc_zh": "市场指数日K线（沪深300/上证等），Benchmark 对比曲线数据源；主源，易被东财 WAF 风控",
        "desc_en": "Market index daily K-line (CSI300/SH etc.), benchmark comparison data source; primary source, prone to WAF rate-limiting",
        "default_strategy": "standard",
        "probe_args": {"symbol": "000300", "period": "daily", "start_date": "20240101", "end_date": "20240110"},
    },
    {
        "key": "index_zh_a_daily",
        "name_zh": "指数日K（新浪）",
        "name_en": "Index Daily K-line (Sina)",
        "category_zh": "行情历史",
        "category_en": "History",
        "module": "index_data",
        "desc_zh": "市场指数日K线备用数据源 1（新浪），稳定性高，返回全量历史需本地过滤；东财风控时自动 fallback",
        "desc_en": "Market index daily K-line fallback source 1 (Sina), stable, returns full history requiring local filtering; auto-fallback when EastMoney rate-limited",
        "default_strategy": "standard",
        "probe_args": {"symbol": "sh000300"},
    },
    {
        "key": "stock_zh_index_daily_tx",
        "name_zh": "指数日K（腾讯）",
        "name_en": "Index Daily K-line (Tencent)",
        "category_zh": "行情历史",
        "category_en": "History",
        "module": "index_data",
        "desc_zh": "市场指数日K线备用数据源 2（腾讯），稳定性高，返回全量历史需本地过滤；东财+新浪均失败时最终 fallback",
        "desc_en": "Market index daily K-line fallback source 2 (Tencent), stable, returns full history requiring local filtering; final fallback when EastMoney and Sina both fail",
        "default_strategy": "standard",
        "probe_args": {"symbol": "sh000300"},
    },
    {
        "key": "fund_etf_hist_em",
        "name_zh": "ETF日K（东财）",
        "name_en": "ETF Daily K-line (EastMoney)",
        "category_zh": "行情历史",
        "category_en": "History",
        "module": "market_data",
        "desc_zh": "ETF日K线主数据源",
        "desc_en": "ETF daily K-line primary source",
        "default_strategy": "standard",
        "probe_args": {"symbol": "510300", "period": "daily", "start_date": "20240101", "end_date": "20240110", "adjust": "qfq"},
    },
    {
        "key": "fund_etf_hist_sina",
        "name_zh": "ETF日K（新浪）",
        "name_en": "ETF Daily K-line (Sina)",
        "category_zh": "行情历史",
        "category_en": "History",
        "module": "market_data",
        "desc_zh": "ETF日K线备用数据源",
        "desc_en": "ETF daily K-line fallback source",
        "default_strategy": "standard",
        "probe_args": {"symbol": "sh510300"},
    },
    {
        "key": "stock_us_daily",
        "name_zh": "美股日K",
        "name_en": "US Stock Daily K-line",
        "category_zh": "行情历史",
        "category_en": "History",
        "module": "market_data",
        "desc_zh": "美股日K线",
        "desc_en": "US stock daily K-line",
        "default_strategy": "standard",
        "probe_args": {"symbol": "AAPL", "adjust": "qfq"},
    },
    # ── 实时行情/估值 ──
    {
        "key": "stock_zh_a_spot_em",
        "name_zh": "A股实时行情（东财）",
        "name_en": "A-share Realtime (EastMoney)",
        "category_zh": "实时行情",
        "category_en": "Realtime",
        "module": "fundamental_data",
        "desc_zh": "全A股PE/PB/市值快照，P2 估值数据源",
        "desc_en": "All A-share PE/PB/market cap snapshot, P2 valuation source",
        "default_strategy": "conservative",
        "probe_args": {},
    },
    {
        "key": "stock_value_em",
        "name_zh": "个股历史估值（东财）",
        "name_en": "Stock Historical Valuation (EastMoney)",
        "category_zh": "基本面因子",
        "category_en": "Fundamental Factors",
        "module": "factors.fundamental",
        "desc_zh": "个股日度PE/PB等历史估值；启用前必须通过字段契约探测",
        "desc_en": "Daily PE/PB valuation history; requires contract validation",
        "default_strategy": "conservative",
        "probe_args": {"symbol": "600519"},
    },
    {
        "key": "stock_financial_analysis_indicator_em",
        "name_zh": "个股财务分析指标（东财）",
        "name_en": "Stock Financial Analysis Indicators (EastMoney)",
        "category_zh": "基本面因子",
        "category_en": "Fundamental Factors",
        "module": "factors.fundamental",
        "desc_zh": "ROE、收入和利润等财务分析指标",
        "desc_en": "ROE, revenue and profit analysis indicators",
        "default_strategy": "conservative",
        "probe_args": {"symbol": "600519.SH"},
    },
    {
        "key": "stock_lrb_em",
        "name_zh": "全市场利润表（东财）",
        "name_en": "Market Income Statements (EastMoney)",
        "category_zh": "基本面因子",
        "category_en": "Fundamental Factors",
        "module": "factors.fundamental",
        "desc_zh": "指定报告期的全市场利润表数据",
        "desc_en": "Market-wide income statements for a report period",
        "default_strategy": "conservative",
        "probe_args": {"date": "20250331"},
    },
    {
        "key": "fund_etf_spot_em",
        "name_zh": "ETF实时行情（东财）",
        "name_en": "ETF Realtime (EastMoney)",
        "category_zh": "实时行情",
        "category_en": "Realtime",
        "module": "etf_basic_data",
        "desc_zh": "ETF实时净值/折价率，P2 ETF 指标数据源",
        "desc_en": "ETF realtime NAV/discount, P2 ETF indicator source",
        "default_strategy": "conservative",
        "probe_args": {},
    },
    # ── ETF 专项 ──
    {
        "key": "fund_etf_fund_info_em",
        "name_zh": "ETF历史净值（东财）",
        "name_en": "ETF Historical NAV (EastMoney)",
        "category_zh": "ETF专项",
        "category_en": "ETF Specific",
        "module": "etf_basic_data",
        "desc_zh": "ETF历史净值序列",
        "desc_en": "ETF historical NAV series",
        "default_strategy": "standard",
        "probe_args": {"fund": "510300"},
    },
    {
        "key": "fund_etf_fund_daily_em",
        "name_zh": "ETF每日份额（东财）",
        "name_en": "ETF Daily Shares (EastMoney)",
        "category_zh": "ETF专项",
        "category_en": "ETF Specific",
        "module": "etf_basic_data",
        "desc_zh": "ETF每日份额/规模/折价率",
        "desc_en": "ETF daily shares/size/discount",
        "default_strategy": "standard",
        "probe_args": {},
    },
    {
        "key": "fund_etf_category_sina",
        "name_zh": "ETF分类（新浪）",
        "name_en": "ETF Category (Sina)",
        "category_zh": "ETF专项",
        "category_en": "ETF Specific",
        "module": "discovery_tasks",
        "desc_zh": "ETF标的发现数据源",
        "desc_en": "ETF symbol discovery source",
        "default_strategy": "standard",
        "probe_args": {},
    },
    # ── 资金流 ──
    {
        "key": "stock_individual_fund_flow",
        "name_zh": "个股资金流",
        "name_en": "Individual Fund Flow",
        "category_zh": "资金流",
        "category_en": "Capital Flow",
        "module": "capital_flow_data",
        "desc_zh": "个股主力/超大单/大单净流入，P2 资金流数据源",
        "desc_en": "Individual main/super-large/large net inflow, P2 capital flow source",
        "default_strategy": "conservative",
        "probe_args": {"stock": "000001", "market": "sz"},
    },
    {
        "key": "stock_lhb_detail_em",
        "name_zh": "龙虎榜明细（东财）",
        "name_en": "Dragon-Tiger List Details (EastMoney)",
        "category_zh": "资金流",
        "category_en": "Capital Flow",
        "module": "factors.capital_flow",
        "desc_zh": "龙虎榜买卖明细；不等同于Level-2逐笔数据",
        "desc_en": "Dragon-Tiger list transactions; not Level-2 tick data",
        "default_strategy": "conservative",
        "probe_args": {"start_date": "20250303", "end_date": "20250307"},
    },
    {
        "key": "stock_lhb_jgmmtj_em",
        "name_zh": "龙虎榜机构买卖统计（东财）",
        "name_en": "Dragon-Tiger Institution Trades (EastMoney)",
        "category_zh": "资金流",
        "category_en": "Capital Flow",
        "module": "lhb_data",
        "desc_zh": "机构席位买入、卖出和净买额；与龙虎榜总净额严格区分",
        "desc_en": "Institution-seat buy, sell and net amounts; distinct from total list net",
        "default_strategy": "conservative",
        "probe_args": {"start_date": "20250303", "end_date": "20250307"},
    },
    {
        "key": "stock_hot_rank_em",
        "name_zh": "个股人气榜（东财）",
        "name_en": "Stock Popularity Ranking (EastMoney)",
        "category_zh": "情绪因子",
        "category_en": "Sentiment Factors",
        "module": "factors.sentiment",
        "desc_zh": "全市场个股人气排名快照，免费接口仅返回前100名",
        "desc_en": "Market popularity snapshot; free endpoint returns top 100",
        "default_strategy": "conservative",
        "probe_args": {},
    },
    {
        "key": "stock_zh_a_hist_min_em",
        "name_zh": "A股分钟行情（东财）",
        "name_en": "A-share Minute Bars (EastMoney)",
        "category_zh": "资金流",
        "category_en": "Capital Flow",
        "module": "tail_proxy_data",
        "desc_zh": "候选池尾盘量价代理；不是Level-2逐笔或大单主买数据",
        "desc_en": "Candidate tail-session price-volume proxy; not Level-2 order flow",
        "default_strategy": "conservative",
        "probe_args": {
            "symbol": "600519",
            "period": "1",
            "adjust": ""
        },
    },
    {
        "key": "stock_hsgt_north_net_flow_in",
        "name_zh": "北向资金",
        "name_en": "Northbound Capital Flow",
        "category_zh": "资金流",
        "category_en": "Capital Flow",
        "module": "capital_flow_data",
        "desc_zh": "北向资金每日净流入（市场层面）",
        "desc_en": "Northbound daily net inflow (market level)",
        "default_strategy": "standard",
        "probe_args": {"symbol": "北上"},
    },
    # ── 宏观因子 ──
    {
        "key": "bond_zh_us_rate",
        "name_zh": "中美国债收益率",
        "name_en": "China and US Treasury Yields",
        "category_zh": "宏观因子",
        "category_en": "Macro Factors",
        "module": "factors.macro",
        "desc_zh": "中美2/5/10/30年期国债收益率历史序列",
        "desc_en": "China and US 2/5/10/30-year treasury yield history",
        "default_strategy": "standard",
        "probe_args": {"start_date": "20250101"},
    },
    {
        "key": "macro_china_market_margin_sh",
        "name_zh": "沪市融资融券",
        "name_en": "SSE Margin Trading",
        "category_zh": "宏观因子",
        "category_en": "Macro Factors",
        "module": "factors.macro",
        "desc_zh": "上海市场融资融券余额历史序列",
        "desc_en": "SSE margin trading balance history",
        "default_strategy": "standard",
        "probe_args": {},
    },
    {
        "key": "macro_china_market_margin_sz",
        "name_zh": "深市融资融券",
        "name_en": "SZSE Margin Trading",
        "category_zh": "宏观因子",
        "category_en": "Macro Factors",
        "module": "factors.macro",
        "desc_zh": "深圳市场融资融券余额历史序列",
        "desc_en": "SZSE margin trading balance history",
        "default_strategy": "standard",
        "probe_args": {},
    },
    # ── 个股信息 ──
    {
        "key": "stock_individual_info_em",
        "name_zh": "个股基本信息（东财）",
        "name_en": "Individual Info (EastMoney)",
        "category_zh": "个股信息",
        "category_en": "Stock Info",
        "module": "fundamental_data",
        "desc_zh": "个股行业等基本信息，P2 估值辅助",
        "desc_en": "Individual stock industry info, P2 valuation auxiliary",
        "default_strategy": "standard",
        "probe_args": {"symbol": "000001"},
    },
    # ── 新闻公告 ──
    {
        "key": "stock_news_em",
        "name_zh": "股票新闻",
        "name_en": "Stock News",
        "category_zh": "新闻公告",
        "category_en": "News",
        "module": "news",
        "desc_zh": "个股相关新闻抓取",
        "desc_en": "Individual stock news fetch",
        "default_strategy": "conservative",
        "probe_args": {"symbol": "000001"},
    },
    {
        "key": "stock_zh_a_disclosure_report_cninfo",
        "name_zh": "巨潮公告",
        "name_en": "CNInfo Disclosure",
        "category_zh": "新闻公告",
        "category_en": "News",
        "module": "news",
        "desc_zh": "巨潮资讯网公告",
        "desc_en": "CNInfo disclosure reports",
        "default_strategy": "standard",
        "probe_args": {"symbol": "000001", "market": "sz"},
    },
    # ── 标的元数据 ──
    {
        "key": "stock_info_a_code_name",
        "name_zh": "A股代码名称表",
        "name_en": "A-share Code-Name Table",
        "category_zh": "标的元数据",
        "category_en": "Metadata",
        "module": "symbol_names",
        "desc_zh": "A股代码与名称映射表",
        "desc_en": "A-share code-to-name mapping",
        "default_strategy": "standard",
        "probe_args": {},
    },
    {
        "key": "fund_name_em",
        "name_zh": "基金名称表",
        "name_en": "Fund Name Table",
        "category_zh": "标的元数据",
        "category_en": "Metadata",
        "module": "symbol_names",
        "desc_zh": "基金代码与名称映射表",
        "desc_en": "Fund code-to-name mapping",
        "default_strategy": "standard",
        "probe_args": {},
    },
]


def get_registry_entry(api_key: str) -> dict[str, Any] | None:
    """根据 api_key 查询 registry 元数据。"""
    for entry in AKSHARE_API_REGISTRY:
        if entry["key"] == api_key:
            return entry
    return None


# ----------------------------------------------------------------------------
# 内存配置缓存
# ----------------------------------------------------------------------------
# 进程级缓存，避免每次 akshare 调用都查 DB。配置变更后由 refresh_config_cache 刷新。
# 缓存结构：{ api_key: { enabled, strategy, delay_min_ms, delay_max_ms, max_retries } }
# ----------------------------------------------------------------------------
_config_cache: dict[str, dict[str, Any]] = {}
_config_cache_lock = threading.Lock()
_config_cache_loaded = False


def _resolve_strategy_params(strategy: str, delay_min_ms: int, delay_max_ms: int) -> tuple[int, int, int]:
    """根据策略档位 + 自定义延时，解析出最终的 (delay_min_ms, delay_max_ms, max_retries)。"""
    strat = ANTI_RISK_STRATEGIES.get(strategy, ANTI_RISK_STRATEGIES[DEFAULT_STRATEGY])
    if strategy == "custom":
        dmin = max(0, int(delay_min_ms))
        dmax = max(dmin, int(delay_max_ms))
        return dmin, dmax, strat["max_retries"]
    return int(strat["delay_min_ms"]), int(strat["delay_max_ms"]), int(strat["max_retries"])


def load_config_cache(db: Session) -> None:
    """从 DB 加载所有接口配置到内存缓存（启动时或批量变更后调用）。"""
    global _config_cache_loaded
    rows = db.execute(select(AkshareApiConfig)).scalars().all()
    cache: dict[str, dict[str, Any]] = {}
    for row in rows:
        dmin, dmax, retries = _resolve_strategy_params(
            row.anti_risk_strategy, row.delay_min_ms, row.delay_max_ms
        )
        cache[row.api_key] = {
            "enabled": bool(row.enabled),
            "strategy": row.anti_risk_strategy,
            "delay_min_ms": dmin,
            "delay_max_ms": dmax,
            "max_retries": retries,
        }
    # 对 registry 中存在但 DB 未配置的接口，用 default_strategy 填充缓存
    for entry in AKSHARE_API_REGISTRY:
        if entry["key"] not in cache:
            strat = entry["default_strategy"]
            s = ANTI_RISK_STRATEGIES[strat]
            cache[entry["key"]] = {
                "enabled": True,
                "strategy": strat,
                "delay_min_ms": int(s["delay_min_ms"]),
                "delay_max_ms": int(s["delay_max_ms"]),
                "max_retries": int(s["max_retries"]),
            }
    with _config_cache_lock:
        _config_cache.clear()
        _config_cache.update(cache)
        _config_cache_loaded = True


def refresh_config_cache_for(api_key: str, db: Session) -> None:
    """刷新单个 api_key 的缓存（配置变更后调用）。"""
    entry_meta = get_registry_entry(api_key)
    if entry_meta is None:
        return
    row = db.execute(
        select(AkshareApiConfig).where(AkshareApiConfig.api_key == api_key)
    ).scalars().first()
    if row is None:
        strat = entry_meta["default_strategy"]
        s = ANTI_RISK_STRATEGIES[strat]
        cfg = {
            "enabled": True,
            "strategy": strat,
            "delay_min_ms": int(s["delay_min_ms"]),
            "delay_max_ms": int(s["delay_max_ms"]),
            "max_retries": int(s["max_retries"]),
        }
    else:
        dmin, dmax, retries = _resolve_strategy_params(
            row.anti_risk_strategy, row.delay_min_ms, row.delay_max_ms
        )
        cfg = {
            "enabled": bool(row.enabled),
            "strategy": row.anti_risk_strategy,
            "delay_min_ms": dmin,
            "delay_max_ms": dmax,
            "max_retries": retries,
        }
    with _config_cache_lock:
        _config_cache[api_key] = cfg


def get_runtime_config(api_key: str) -> dict[str, Any]:
    """获取某个接口的运行时配置（延时/重试/启用）。

    若缓存未加载或该接口未注册，返回默认 standard 档位。
    """
    if not _config_cache_loaded:
        # 缓存未加载，返回安全默认值
        s = ANTI_RISK_STRATEGIES[DEFAULT_STRATEGY]
        return {
            "enabled": True,
            "strategy": DEFAULT_STRATEGY,
            "delay_min_ms": int(s["delay_min_ms"]),
            "delay_max_ms": int(s["delay_max_ms"]),
            "max_retries": int(s["max_retries"]),
        }
    with _config_cache_lock:
        return _config_cache.get(api_key, {
            "enabled": True,
            "strategy": DEFAULT_STRATEGY,
            "delay_min_ms": 300,
            "delay_max_ms": 800,
            "max_retries": 3,
        })


def apply_delay(api_key: str) -> None:
    """在调用 akshare 前，根据 api_key 的配置 sleep 随机延时。

    若接口被禁用，调用方应自行判断是否跳过（本函数不抛异常，仅记录日志）。
    """
    cfg = get_runtime_config(api_key)
    dmin = cfg["delay_min_ms"]
    dmax = cfg["delay_max_ms"]
    if dmax <= 0:
        return
    delay = random.uniform(dmin, dmax) / 1000.0
    if delay > 0:
        time.sleep(delay)


def is_api_enabled(api_key: str) -> bool:
    """检查某个接口是否启用（禁用时调用方应跳过该数据源）。"""
    return get_runtime_config(api_key).get("enabled", True)


def get_max_retries(api_key: str) -> int:
    """获取某个接口的最大重试次数。"""
    return int(get_runtime_config(api_key).get("max_retries", 3))


# ----------------------------------------------------------------------------
# 运行时状态记录
# ----------------------------------------------------------------------------

def record_call_result(db: Session, api_key: str, success: bool, error: str | None = None) -> None:
    """记录一次实际调用的结果（成功/失败 + 错误信息），更新 total_calls/total_failures。"""
    try:
        row = db.execute(
            select(AkshareApiConfig).where(AkshareApiConfig.api_key == api_key)
        ).scalars().first()
        if row is None:
            # 自动创建配置行（用 registry 默认档位）
            entry = get_registry_entry(api_key)
            if entry is None:
                return
            row = AkshareApiConfig(
                api_key=api_key,
                enabled=True,
                anti_risk_strategy=entry["default_strategy"],
                delay_min_ms=300,
                delay_max_ms=800,
            )
            db.add(row)
        row.last_call_at = datetime.now(timezone.utc)
        row.last_call_success = success
        row.last_call_error = error if not success else None
        row.total_calls = (row.total_calls or 0) + 1
        if not success:
            row.total_failures = (row.total_failures or 0) + 1
        db.flush()
    except Exception as exc:
        logger.debug("record_call_result failed for %s: %s", api_key, exc)


def record_probe_result(
    db: Session,
    api_key: str,
    success: bool,
    latency_ms: int | None,
    error: str | None = None,
) -> None:
    """记录一次主动探测的结果。"""
    try:
        row = db.execute(
            select(AkshareApiConfig).where(AkshareApiConfig.api_key == api_key)
        ).scalars().first()
        if row is None:
            entry = get_registry_entry(api_key)
            if entry is None:
                return
            row = AkshareApiConfig(
                api_key=api_key,
                enabled=True,
                anti_risk_strategy=entry["default_strategy"],
                delay_min_ms=300,
                delay_max_ms=800,
            )
            db.add(row)
        row.last_probe_at = datetime.now(timezone.utc)
        row.last_probe_success = success
        row.last_probe_latency_ms = latency_ms
        row.last_probe_error = error if not success else None
        db.flush()
    except Exception as exc:
        logger.debug("record_probe_result failed for %s: %s", api_key, exc)
