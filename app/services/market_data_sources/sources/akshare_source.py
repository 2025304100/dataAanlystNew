"""akshare 数据源 adapter 集合。

每个 adapter 包装一个 akshare 接口,实现 HistorySource Protocol。
失败时抛 SourceUnavailable 触发降级链 next。

6 个 adapter:
- EMStockSource: 东财 A 股(stock_zh_a_hist)
- EMETFSource:   东财 ETF(fund_etf_hist_em)
- SinaStockSource: 新浪 A 股(stock_zh_a_daily)
- SinaETFSource:   新浪 ETF(fund_etf_hist_sina)
- TxStockSource: 腾讯 A 股(stock_zh_a_hist_tx)
- USStockSource: 东财美股(stock_us_daily)

设计要点:
- 每个源 available() 恒为 True(akshare 已安装即可用,具体失败在 fetch 时判断)
- supports() 按 region + asset_type 匹配
- fetch() 用 call_akshare_with_retry 包装(max_attempts=2,给瞬时风控一次重试)
- 所有源都用 _proxy_bypass()(在 _fetch_history 外层调用)避免代理干扰
"""
from __future__ import annotations

import logging
from datetime import date

import akshare as ak
import pandas as pd

from app.services.akshare_utils import call_akshare_with_retry
from app.services.market_data_sources.sources._normalize import (
    cn_prefixed_symbol,
    normalize_cn_em_history,
    normalize_cn_etf_sina_history,
    normalize_cn_stock_sina_history,
    normalize_cn_stock_tx_history,
    normalize_us_history,
)
from app.services.market_data_sources.sources.base import HistorySource, SourceUnavailable

logger = logging.getLogger(__name__)


def _format_date(d: date) -> str:
    return d.strftime("%Y%m%d")


class EMStockSource:
    """东财 A 股(stock_zh_a_hist)。"""

    name: str = "em_stock"
    region: str = "cn"
    asset_types: list[str] = ["stock"]

    def available(self) -> bool:
        return True

    def supports(self, symbol) -> bool:
        return symbol.asset_type == "stock" and (symbol.market or "").lower() in {"sh", "sz", "bj", "cn"}

    def fetch(self, symbol, start_date: date, end_date: date, adjust: str) -> pd.DataFrame:
        try:
            frame = call_akshare_with_retry(
                ak.stock_zh_a_hist,
                symbol=symbol.symbol,
                period="daily",
                start_date=_format_date(start_date),
                end_date=_format_date(end_date),
                adjust=adjust,
                api_key="stock_zh_a_hist",
                max_attempts=2,
            )
            return normalize_cn_em_history(frame)
        except Exception as e:
            raise SourceUnavailable(f"em_stock failed for {symbol.symbol}: {e}") from e


class EMETFSource:
    """东财 ETF(fund_etf_hist_em)。"""

    name: str = "em_etf"
    region: str = "cn"
    asset_types: list[str] = ["etf"]

    def available(self) -> bool:
        return True

    def supports(self, symbol) -> bool:
        return symbol.asset_type == "etf" and (symbol.market or "").lower() in {"sh", "sz", "cn"}

    def fetch(self, symbol, start_date: date, end_date: date, adjust: str) -> pd.DataFrame:
        try:
            frame = call_akshare_with_retry(
                ak.fund_etf_hist_em,
                symbol=symbol.symbol,
                period="daily",
                start_date=_format_date(start_date),
                end_date=_format_date(end_date),
                adjust=adjust,
                api_key="fund_etf_hist_em",
                max_attempts=2,
            )
            return normalize_cn_em_history(frame)
        except Exception as e:
            raise SourceUnavailable(f"em_etf failed for {symbol.symbol}: {e}") from e


class SinaStockSource:
    """新浪 A 股(stock_zh_a_daily)。

    新浪源支持北交所(BJ)股票,但需要用 bj 前缀(由 cn_prefixed_symbol 处理)。
    cn_prefixed_symbol 会对 920 开头的代码返回 bj 前缀。
    """

    name: str = "sina_stock"
    region: str = "cn"
    asset_types: list[str] = ["stock"]

    def available(self) -> bool:
        return True

    def supports(self, symbol) -> bool:
        # 新浪源支持 SH/SZ/BJ(北交所用 bj 前缀,cn_prefixed_symbol 会处理)
        return symbol.asset_type == "stock" and (symbol.market or "").lower() in {"sh", "sz", "bj", "cn"}

    def fetch(self, symbol, start_date: date, end_date: date, adjust: str) -> pd.DataFrame:
        try:
            frame = call_akshare_with_retry(
                ak.stock_zh_a_daily,
                symbol=cn_prefixed_symbol(symbol),
                start_date=_format_date(start_date),
                end_date=_format_date(end_date),
                adjust=adjust,
                api_key="stock_zh_a_daily",
                max_attempts=2,
            )
            return normalize_cn_stock_sina_history(frame, start_date=start_date, end_date=end_date)
        except Exception as e:
            raise SourceUnavailable(f"sina_stock failed for {symbol.symbol}: {e}") from e


class SinaETFSource:
    """新浪 ETF(fund_etf_hist_sina)。"""

    name: str = "sina_etf"
    region: str = "cn"
    asset_types: list[str] = ["etf"]

    def available(self) -> bool:
        return True

    def supports(self, symbol) -> bool:
        return symbol.asset_type == "etf" and (symbol.market or "").lower() in {"sh", "sz", "cn"}

    def fetch(self, symbol, start_date: date, end_date: date, adjust: str) -> pd.DataFrame:
        try:
            frame = call_akshare_with_retry(
                ak.fund_etf_hist_sina,
                symbol=cn_prefixed_symbol(symbol),
                api_key="fund_etf_hist_sina",
                max_attempts=2,
            )
            return normalize_cn_etf_sina_history(frame, start_date=start_date, end_date=end_date)
        except Exception as e:
            raise SourceUnavailable(f"sina_etf failed for {symbol.symbol}: {e}") from e


class TxStockSource:
    """腾讯 A 股(stock_zh_a_hist_tx)。

    腾讯源不支持北交所(BJ)股票,内部解析返回空列表导致 IndexError。
    920 开头是北交所代码(即使 market 字段标记为 sh),需要排除。
    仅支持 SH/SZ。
    """

    name: str = "tx_stock"
    region: str = "cn"
    asset_types: list[str] = ["stock"]

    def available(self) -> bool:
        return True

    def supports(self, symbol) -> bool:
        if symbol.asset_type != "stock":
            return False
        # 腾讯源不支持北交所,排除 bj 和 920 开头代码(market 字段可能标错为 sh)
        if (symbol.market or "").lower() == "bj":
            return False
        if symbol.symbol.startswith("920"):
            return False
        return (symbol.market or "").lower() in {"sh", "sz", "cn"}

    def fetch(self, symbol, start_date: date, end_date: date, adjust: str) -> pd.DataFrame:
        try:
            frame = call_akshare_with_retry(
                ak.stock_zh_a_hist_tx,
                symbol=cn_prefixed_symbol(symbol),
                start_date=_format_date(start_date),
                end_date=_format_date(end_date),
                adjust=adjust,
                api_key="stock_zh_a_hist_tx",
                max_attempts=2,
            )
            return normalize_cn_stock_tx_history(frame, start_date=start_date, end_date=end_date)
        except Exception as e:
            raise SourceUnavailable(f"tx_stock failed for {symbol.symbol}: {e}") from e


class USStockSource:
    """东财美股(stock_us_daily)。"""

    name: str = "us_em"
    region: str = "us"
    asset_types: list[str] = ["stock"]

    def available(self) -> bool:
        return True

    def supports(self, symbol) -> bool:
        return symbol.asset_type == "stock" and (symbol.market or "").lower() in {"us", "nasdaq", "nyse", "amex"}

    def fetch(self, symbol, start_date: date, end_date: date, adjust: str) -> pd.DataFrame:
        try:
            # 美股仅支持 "" 不复权 / "qfq" 前复权,其他值降级为不复权
            us_adjust = adjust if adjust in {"", "qfq"} else ""
            frame = call_akshare_with_retry(
                ak.stock_us_daily,
                symbol=symbol.symbol,
                adjust=us_adjust,
                api_key="stock_us_daily",
                max_attempts=2,
            )
            return normalize_us_history(frame=frame, start_date=start_date, end_date=end_date)
        except Exception as e:
            raise SourceUnavailable(f"us stock failed for {symbol.symbol}: {e}") from e
