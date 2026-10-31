"""行情 DataFrame 标准化函数。

从 market_data.py 抽取的共享 normalize 函数,供各 source adapter 使用。
放在独立模块避免循环引用(sources/* ↔ market_data.py)。

所有 source adapter 返回的 DataFrame 都经过这些函数标准化,
保证列名和数据类型与现有 _standard_history_frame 一致。
"""
from __future__ import annotations

from datetime import date

import pandas as pd
from app.services.market_history_contract import resolve_cn_prefixed_symbol, tencent_quantity_to_shares_multiplier
from app.services.data_source_contracts import DataContractError, normalize_units


def attach_cn_stock_history_contract(frame: pd.DataFrame, provider: str) -> pd.DataFrame:
    """Label adapter units, without claiming raw-response or adjustment proof."""
    endpoints = {
        "eastmoney": "stock_zh_a_hist",
        "sina": "stock_zh_a_daily",
        "tencent": "stock_zh_a_hist_tx",
    }
    normalized = frame.copy()
    normalized.attrs["history_contract"] = {
        "provider": provider,
        "endpoint": endpoints[provider],
        "version": "cn-stock-shares-v1",
        "volume_unit": "share",
        "amount_unit": "CNY",
        "turnover_unit": "percentage_point",
    }
    return normalized


def standard_history_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """标准化行情 DataFrame:统一列名、类型、过滤无效行。

    Args:
        frame: 原始 DataFrame(已重命名列名)

    Returns:
        标准化后的 DataFrame,列:trade_date, open, high, low, close, volume, amount, turnover_rate。
        turnover_rate 使用百分数（percentage points）：2.0 表示 2%，
        各源的比例到百分数转换由其 adapter 明确完成。
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


def normalize_cn_stock_em_history(frame: pd.DataFrame) -> pd.DataFrame:
    """A-share EM quantity is lots: convert to shares; ETF keeps its own path."""
    normalized = normalize_cn_em_history(frame).copy()
    normalized = normalize_units(normalized, "stock_zh_a_hist")
    return attach_cn_stock_history_contract(normalized, "eastmoney")


def normalize_cn_etf_em_history(frame: pd.DataFrame) -> pd.DataFrame:
    """EM ETF K-line quantity is lots of 100 fund units; cash stays in CNY."""
    normalized = normalize_cn_em_history(frame).copy()
    normalized = normalize_units(normalized, "fund_etf_hist_em")
    normalized.attrs['history_contract'] = {
        'provider': 'eastmoney_etf', 'endpoint': 'fund_etf_hist_em',
        'version': 'cn-etf-shares-v1', 'volume_unit': 'share',
        'amount_unit': 'CNY', 'turnover_unit': 'percentage_point',
    }
    return normalized


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
    """标准化新浪 stock_zh_a_daily；turnover 的比例转为百分数。"""
    if frame.empty:
        return attach_cn_stock_history_contract(frame, "sina")
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
    # Sina computes volume / outstanding_share (0.02 means 2%); Eastmoney's
    # 换手率 already uses percentage points. Never infer units from magnitude.
    normalized = normalize_units(normalized, "stock_zh_a_daily")
    return attach_cn_stock_history_contract(standard_history_frame(normalized), "sina")


def normalize_cn_stock_tx_history(frame: pd.DataFrame, start_date: date, end_date: date) -> pd.DataFrame:
    """腾讯第六字段为数量：688为股，其余已核实板块为手；成交额未知。"""
    # Only the verified legacy SDK schema uses "amount" for lots. Newer SDK
    # schemas expose volume/turnover and may use "amount" for actual CNY.
    # Reject structural changes before interpreting a field's numeric value.
    legacy_columns = {"date", "open", "close", "high", "low", "amount"}
    if not frame.columns.is_unique or not set(frame.columns).issubset(legacy_columns):
        raise DataContractError("unsupported Tencent history schema")
    if frame.empty:
        return attach_cn_stock_history_contract(frame, "tencent")
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
    # The legacy field alias is "amount", but its wire unit is symbol-bound:
    # sh688 uses shares; verified main/ChiNext families use 100-share lots.
    # Never select a conversion by numeric magnitude or invent cash
    # turnover from quantity or price. Missing/invalid quantity remains unknown.
    # Validate the symbol-bound wire evidence before choosing the shared rule.
    # The registry owns the conversion; this adapter only resolves the alias.
    try:
        tencent_quantity_to_shares_multiplier(frame)
    except ValueError as error:
        raise DataContractError(str(error)) from error
    normalized["volume"] = normalized.get("amount")
    normalized["amount"] = None
    normalized["turnover_rate"] = None
    normalized = normalize_units(normalized, "stock_zh_a_hist_tx",
                                 instrument_family=frame.attrs["tencent_quantity_contract"]["instrument_family"])
    return attach_cn_stock_history_contract(standard_history_frame(normalized), "tencent")


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
    """明确sh/sz/bj优先；泛CN只按已知股票/ETF代码族推断，未知拒绝。"""
    return resolve_cn_prefixed_symbol(symbol.symbol, symbol.market, getattr(symbol, "asset_type", "stock"))
