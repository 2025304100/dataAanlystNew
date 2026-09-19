"""行情 DataFrame 标准化函数。

从 market_data.py 抽取的共享 normalize 函数,供各 source adapter 使用。
放在独立模块避免循环引用(sources/* ↔ market_data.py)。

所有 source adapter 返回的 DataFrame 都经过这些函数标准化,
保证列名和数据类型与现有 _standard_history_frame 一致。
"""
from __future__ import annotations

from datetime import date

import pandas as pd


def standard_history_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """标准化行情 DataFrame:统一列名、类型、过滤无效行。

    Args:
        frame: 原始 DataFrame(已重命名列名)

    Returns:
        标准化后的 DataFrame,列:trade_date, open, high, low, close, volume, amount, turnover_rate
    """
    expected = ["trade_date", "open", "high", "low", "close", "volume", "amount", "turnover_rate"]
    normalized = frame.copy()
    for column in expected:
        if column not in normalized.columns:
            normalized[column] = None
    normalized = normalized[expected]
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    for column in ["open", "high", "low", "close", "volume", "amount", "turnover_rate"]:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    return normalized.dropna(subset=["trade_date", "open", "high", "low", "close"]).reset_index(drop=True)


def normalize_cn_em_history(frame: pd.DataFrame) -> pd.DataFrame:
    """标准化东财(em)返回的 DataFrame(中文列名)。"""
    if frame.empty:
        return frame
    normalized = frame.rename(
        columns={
            "\u65e5\u671f": "trade_date",
            "\u5f00\u76d8": "open",
            "\u6700\u9ad8": "high",
            "\u6700\u4f4e": "low",
            "\u6536\u76d8": "close",
            "\u6210\u4ea4\u91cf": "volume",
            "\u6210\u4ea4\u989d": "amount",
            "\u6362\u624b\u7387": "turnover_rate",
        }
    )
    return standard_history_frame(normalized)


def normalize_us_history(frame: pd.DataFrame, start_date: date, end_date: date) -> pd.DataFrame:
    """标准化美股返回的 DataFrame,并按日期过滤。"""
    if frame.empty:
        return frame
    normalized = frame.rename(
        columns={
            "date": "trade_date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
        }
    ).copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    normalized = normalized[
        (normalized["trade_date"] >= pd.Timestamp(start_date)) & (normalized["trade_date"] <= pd.Timestamp(end_date))
    ]
    normalized["amount"] = None
    normalized["turnover_rate"] = None
    return standard_history_frame(normalized)


def normalize_cn_stock_sina_history(frame: pd.DataFrame, start_date: date, end_date: date) -> pd.DataFrame:
    """标准化新浪(stock_zh_a_daily)返回的 DataFrame。"""
    if frame.empty:
        return frame
    normalized = frame.rename(
        columns={
            "date": "trade_date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "amount": "amount",
            "turnover": "turnover_rate",
        }
    ).copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    normalized = normalized[
        (normalized["trade_date"] >= pd.Timestamp(start_date)) & (normalized["trade_date"] <= pd.Timestamp(end_date))
    ]
    return standard_history_frame(normalized)


def normalize_cn_stock_tx_history(frame: pd.DataFrame, start_date: date, end_date: date) -> pd.DataFrame:
    """标准化腾讯(stock_zh_a_hist_tx)返回的 DataFrame。"""
    if frame.empty:
        return frame
    normalized = frame.rename(
        columns={
            "date": "trade_date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "amount": "amount",
        }
    ).copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    normalized = normalized[
        (normalized["trade_date"] >= pd.Timestamp(start_date)) & (normalized["trade_date"] <= pd.Timestamp(end_date))
    ]
    normalized["volume"] = None
    normalized["turnover_rate"] = None
    return standard_history_frame(normalized)


def normalize_cn_etf_sina_history(frame: pd.DataFrame, start_date: date, end_date: date) -> pd.DataFrame:
    """标准化新浪(fund_etf_hist_sina)返回的 ETF DataFrame。"""
    if frame.empty:
        return frame
    normalized = frame.rename(
        columns={
            "date": "trade_date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "amount": "amount",
        }
    ).copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    normalized = normalized[
        (normalized["trade_date"] >= pd.Timestamp(start_date)) & (normalized["trade_date"] <= pd.Timestamp(end_date))
    ]
    normalized["turnover_rate"] = None
    return standard_history_frame(normalized)


def cn_prefixed_symbol(symbol) -> str:
    """Symbol 模型转 akshare 新浪/腾讯需要的前缀代码。

    代码前缀规则:
        6/5/9 开头(非 920) → sh(上交所)
        920 开头            → bj(北交所,新浪源需要)
        其他                → sz(深交所)

    注意:920 开头是北交所代码,但数据库中 market 字段可能标记为 'sh'(历史脏数据),
    此处按 symbol 代码前缀判断更可靠。

    Examples:
        Symbol(symbol="000001", market="SZ") → "sz000001"
        Symbol(symbol="600000", market="SH") → "sh600000"
        Symbol(symbol="920072", market="SH") → "bj920072"  (北交所,market 字段可能标错)
    """
    market = (symbol.market or "").lower()
    # 920 开头是北交所,优先按代码前缀判断(数据库 market 字段可能标错)
    if symbol.symbol.startswith("920"):
        return f"bj{symbol.symbol}"
    prefix = "sh" if market in {"sh", "cn"} or symbol.symbol.startswith(("5", "6", "9")) else "sz"
    return f"{prefix}{symbol.symbol}"
