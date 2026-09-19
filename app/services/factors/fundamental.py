"""Pure field adapters for fundamental AkShare interfaces."""
from __future__ import annotations

from datetime import date

import pandas as pd

from app.services.factors.contracts import (
    normalize_dates,
    normalize_numbers,
    normalize_symbol,
    resolve_contract,
)


VALUATION_COLUMNS = [
    "symbol",
    "trade_date",
    "pe_ttm",
    "pb",
    "dividend_yield",
    "total_market_cap",
    "circulating_market_cap",
    "source",
]

FINANCIAL_COLUMNS = [
    "symbol",
    "report_period",
    "announcement_date",
    "report_type",
    "roe_ttm",
    "net_profit",
    "revenue",
    "net_profit_yoy",
    "revenue_yoy",
    "source",
]


def normalize_stock_value_frame(
    frame: pd.DataFrame, *, symbol: str
) -> pd.DataFrame:
    aliases = {
        "trade_date": ("数据日期", "日期", "TRADE_DATE"),
        "pe_ttm": ("PE(TTM)", "PE_TTM", "市盈率TTM"),
        "pb": ("市净率", "PB_MRQ", "PB"),
        "dividend_yield": ("股息率", "DIVIDEND_YIELD"),
        "total_market_cap": ("总市值", "TOTAL_MARKET_CAP"),
        "circulating_market_cap": (
            "流通市值",
            "NOTLIMITED_MARKETCAP_A",
        ),
    }
    normalized = resolve_contract(
        frame,
        api_key="stock_value_em",
        aliases=aliases,
        required=("trade_date", "pe_ttm", "pb"),
    )
    if normalized.empty:
        return pd.DataFrame(columns=VALUATION_COLUMNS)
    normalized["trade_date"] = normalize_dates(normalized["trade_date"])
    for column in aliases:
        if column != "trade_date":
            normalized[column] = normalize_numbers(normalized[column])
    normalized.insert(0, "symbol", normalize_symbol(symbol))
    normalized["source"] = "akshare:stock_value_em"
    return normalized.dropna(subset=["trade_date"])[VALUATION_COLUMNS]


def normalize_income_statement_frame(
    frame: pd.DataFrame, *, report_period: date
) -> pd.DataFrame:
    aliases = {
        "symbol": ("股票代码", "代码", "SECURITY_CODE"),
        "announcement_date": (
            "最新公告日期",
            "公告日期",
            "NOTICE_DATE",
        ),
        "net_profit": ("净利润", "PARENT_NETPROFIT", "NETPROFIT"),
        "revenue": (
            "营业总收入",
            "营业收入",
            "TOTAL_OPERATE_INCOME",
        ),
        "net_profit_yoy": (
            "净利润同比",
            "PARENT_NETPROFIT_YOY",
        ),
        "revenue_yoy": (
            "营业总收入同比",
            "TOTAL_OPERATE_INCOME_YOY",
        ),
    }
    normalized = resolve_contract(
        frame,
        api_key="stock_lrb_em",
        aliases=aliases,
        required=("symbol", "announcement_date", "net_profit", "revenue"),
    )
    if normalized.empty:
        return pd.DataFrame(columns=FINANCIAL_COLUMNS)
    normalized["symbol"] = normalized["symbol"].map(normalize_symbol)
    normalized["announcement_date"] = normalize_dates(
        normalized["announcement_date"]
    )
    for column in (
        "net_profit",
        "revenue",
        "net_profit_yoy",
        "revenue_yoy",
    ):
        normalized[column] = normalize_numbers(normalized[column])
    normalized["report_period"] = report_period
    normalized["report_type"] = "income_statement"
    normalized["roe_ttm"] = pd.NA
    normalized["source"] = "akshare:stock_lrb_em"
    return normalized.dropna(
        subset=["symbol", "announcement_date"]
    )[FINANCIAL_COLUMNS]


def normalize_financial_analysis_frame(
    frame: pd.DataFrame, *, symbol: str
) -> pd.DataFrame:
    aliases = {
        "report_period": ("REPORT_DATE", "报告期", "报告日期"),
        "announcement_date": ("NOTICE_DATE", "公告日期", "最新公告日期"),
        "roe_ttm": ("ROEJQ", "ROE", "净资产收益率"),
        "net_profit": ("PARENT_NETPROFIT", "净利润"),
        "revenue": ("TOTAL_OPERATE_INCOME", "营业总收入"),
        "net_profit_yoy": ("PARENT_NETPROFIT_YOY", "净利润同比"),
        "revenue_yoy": (
            "TOTAL_OPERATE_INCOME_YOY",
            "营业总收入同比",
        ),
    }
    normalized = resolve_contract(
        frame,
        api_key="stock_financial_analysis_indicator_em",
        aliases=aliases,
        required=("report_period", "announcement_date", "roe_ttm"),
    )
    if normalized.empty:
        return pd.DataFrame(columns=FINANCIAL_COLUMNS)
    normalized["report_period"] = normalize_dates(
        normalized["report_period"]
    )
    normalized["announcement_date"] = normalize_dates(
        normalized["announcement_date"]
    )
    for column in (
        "roe_ttm",
        "net_profit",
        "revenue",
        "net_profit_yoy",
        "revenue_yoy",
    ):
        normalized[column] = normalize_numbers(normalized[column])
    normalized.insert(0, "symbol", normalize_symbol(symbol))
    normalized["report_type"] = "financial_analysis"
    normalized["source"] = (
        "akshare:stock_financial_analysis_indicator_em"
    )
    return normalized.dropna(
        subset=["report_period", "announcement_date"]
    )[FINANCIAL_COLUMNS]
