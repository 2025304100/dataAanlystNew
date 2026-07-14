from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.services.factors.capital_flow import (
    normalize_fund_flow_frame,
    normalize_lhb_detail_frame,
)
from app.services.factors.contracts import DataContractError
from app.services.factors.fundamental import (
    normalize_financial_analysis_frame,
    normalize_income_statement_frame,
    normalize_stock_value_frame,
)
from app.services.factors.macro import (
    normalize_bond_rate_frame,
    normalize_margin_frame,
)
from app.services.factors.sentiment import normalize_hot_rank_frame

pytestmark = pytest.mark.whitebox


def test_stock_value_contract_normalizes_aliases_and_types():
    frame = pd.DataFrame(
        [
            {
                "数据日期": "2026-07-10",
                "PE(TTM)": "20.5",
                "市净率": "6.1",
                "总市值": "1,000",
                "流通市值": 900,
            }
        ]
    )

    result = normalize_stock_value_frame(frame, symbol="SH600519")

    assert result.iloc[0]["symbol"] == "600519"
    assert result.iloc[0]["trade_date"] == date(2026, 7, 10)
    assert result.iloc[0]["pe_ttm"] == 20.5
    assert result.iloc[0]["total_market_cap"] == 1000


def test_stock_value_contract_rejects_schema_drift():
    frame = pd.DataFrame([{"日期": "2026-07-10", "未知PE": 20}])

    with pytest.raises(DataContractError) as exc_info:
        normalize_stock_value_frame(frame, symbol="600519")

    assert exc_info.value.api_key == "stock_value_em"
    assert set(exc_info.value.missing) == {"pe_ttm", "pb"}


def test_empty_contract_frame_is_valid_empty_result():
    result = normalize_stock_value_frame(pd.DataFrame(), symbol="600519")

    assert result.empty
    assert {"symbol", "trade_date", "pe_ttm", "pb"} <= set(
        result.columns
    )


def test_financial_contracts_preserve_announcement_date():
    income = normalize_income_statement_frame(
        pd.DataFrame(
            [
                {
                    "股票代码": "600519",
                    "最新公告日期": "2026-04-02",
                    "净利润": 100,
                    "营业总收入": 200,
                    "净利润同比": "10%",
                    "营业总收入同比": "8%",
                }
            ]
        ),
        report_period=date(2026, 3, 31),
    )
    analysis = normalize_financial_analysis_frame(
        pd.DataFrame(
            [
                {
                    "REPORT_DATE": "2026-03-31",
                    "NOTICE_DATE": "2026-04-02",
                    "ROEJQ": "18.2",
                    "PARENT_NETPROFIT": 100,
                    "TOTAL_OPERATE_INCOME": 200,
                }
            ]
        ),
        symbol="600519.SH",
    )

    assert income.iloc[0]["announcement_date"] == date(2026, 4, 2)
    assert income.iloc[0]["report_period"] == date(2026, 3, 31)
    assert analysis.iloc[0]["symbol"] == "600519"
    assert analysis.iloc[0]["roe_ttm"] == 18.2


def test_fund_flow_and_lhb_contracts_do_not_mix_institution_net():
    flow = normalize_fund_flow_frame(
        pd.DataFrame(
            [
                {
                    "日期": "2026-07-10",
                    "主力净流入-净额": "1,200",
                    "主力净流入-净占比": "2.5%",
                }
            ]
        ),
        symbol="SZ000001",
    )
    lhb = normalize_lhb_detail_frame(
        pd.DataFrame(
            [
                {
                    "代码": "000001",
                    "上榜日": "2026-07-10",
                    "龙虎榜净买额": 500,
                    "龙虎榜买入额": 900,
                    "龙虎榜卖出额": 400,
                }
            ]
        )
    )

    assert flow.iloc[0]["main_net_inflow"] == 1200
    assert flow.iloc[0]["main_net_inflow_pct"] == 2.5
    assert lhb.iloc[0]["lhb_net_buy"] == 500
    assert "lhb_institution_net" not in lhb.columns


def test_hot_rank_contract_calculates_rank_percentile():
    result = normalize_hot_rank_frame(
        pd.DataFrame(
            [
                {"当前排名": 1, "代码": "SZ000001"},
                {"当前排名": 2, "代码": "SH600519"},
            ]
        ),
        as_of=date(2026, 7, 10),
    )

    assert result["symbol"].tolist() == ["000001", "600519"]
    assert result["hot_rank_total"].tolist() == [2, 2]
    assert result["hot_rank_pct"].tolist() == [50.0, 100.0]


def test_macro_contracts_sort_history_before_previous_value():
    bond = normalize_bond_rate_frame(
        pd.DataFrame(
            [
                {
                    "日期": "2026-07-11",
                    "中国国债收益率10年": 1.7,
                    "美国国债收益率10年": 4.2,
                },
                {
                    "日期": "2026-07-10",
                    "中国国债收益率10年": 1.6,
                    "美国国债收益率10年": 4.1,
                },
            ]
        )
    )
    margin = normalize_margin_frame(
        pd.DataFrame(
            [
                {"日期": "2026-07-11", "融资融券余额": 110},
                {"日期": "2026-07-10", "融资融券余额": 100},
            ]
        ),
        market="sh",
    )

    cn = bond[bond["indicator_key"] == "cn_10y_yield"].reset_index(
        drop=True
    )
    assert cn["period"].tolist() == [
        date(2026, 7, 10),
        date(2026, 7, 11),
    ]
    assert cn.iloc[1]["previous_value"] == 1.6
    assert margin["period"].tolist() == [
        date(2026, 7, 10),
        date(2026, 7, 11),
    ]
    assert margin.iloc[1]["previous_value"] == 100


def test_margin_contract_validates_market():
    with pytest.raises(ValueError, match="market"):
        normalize_margin_frame(pd.DataFrame(), market="bj")
