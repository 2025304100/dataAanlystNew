"""白盒测试：市场指数日线同步服务（P3+ Benchmark 基础设施）。

覆盖：
1. _to_prefixed_symbol：沪市/深市前缀转换
2. _normalize_eastmoney_frame：中文列名 → 英文列名
3. _normalize_sina_frame / _normalize_tencent_frame：英文列重命名 + 日期过滤
4. _filter_by_date：日期范围过滤
5. _fetch_index_daily fallback 链：东财→新浪→腾讯，全失败降级
6. sync_index_daily：Upsert 语义、空数据、symbol 校验、source 字段记录
7. list_index_prices：日期过滤、升序
8. 边界：akshare 返回空 / 部分字段缺失 / 重复同步幂等
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest
from sqlalchemy import select

from app.models.index_price import IndexPrice
from app.services.index_data import (
    IndexSyncResult,
    _fetch_from_eastmoney,
    _fetch_from_sina,
    _fetch_from_tencent,
    _fetch_index_daily,
    _filter_by_date,
    _normalize_eastmoney_frame,
    _normalize_sina_frame,
    _normalize_tencent_frame,
    _to_prefixed_symbol,
    list_index_prices,
    sync_index_daily,
)


pytestmark = pytest.mark.whitebox


def _eastmoney_frame() -> pd.DataFrame:
    """模拟 ak.index_zh_a_hist 返回的中文列 DataFrame（东财主源）。"""
    return pd.DataFrame(
        [
            {"日期": "2026-07-14", "开盘": 4000.0, "收盘": 4050.0, "最高": 4080.0, "最低": 3990.0, "成交量": 1000000.0, "成交额": 4.02e9},
            {"日期": "2026-07-15", "开盘": 4050.0, "收盘": 4100.0, "最高": 4120.0, "最低": 4040.0, "成交量": 1100000.0, "成交额": 4.51e9},
            {"日期": "2026-07-16", "开盘": 4100.0, "收盘": 4080.0, "最高": 4130.0, "最低": 4070.0, "成交量": 950000.0, "成交额": 3.88e9},
        ]
    )


def _sina_frame() -> pd.DataFrame:
    """模拟 ak.stock_zh_index_daily 返回的英文列 DataFrame（新浪备源）。

    新浪返回全量历史，列：date / open / high / low / close / volume（无 amount）
    """
    return pd.DataFrame(
        [
            {"date": "2026-07-14", "open": 4000.0, "high": 4080.0, "low": 3990.0, "close": 4050.0, "volume": 1000000.0},
            {"date": "2026-07-15", "open": 4050.0, "high": 4120.0, "low": 4040.0, "close": 4100.0, "volume": 1100000.0},
            {"date": "2026-07-16", "open": 4100.0, "high": 4130.0, "low": 4070.0, "close": 4080.0, "volume": 950000.0},
            # 范围外的数据（应被过滤）
            {"date": "2026-07-10", "open": 3900.0, "high": 3950.0, "low": 3880.0, "close": 3920.0, "volume": 800000.0},
            {"date": "2026-07-20", "open": 4200.0, "high": 4250.0, "low": 4180.0, "close": 4230.0, "volume": 1200000.0},
        ]
    )


def _tencent_frame() -> pd.DataFrame:
    """模拟 ak.stock_zh_index_daily_tx 返回的英文列 DataFrame（腾讯备源）。

    腾讯返回全量历史，列：date / open / close / high / low / amount（无 volume）
    """
    return pd.DataFrame(
        [
            {"date": "2026-07-14", "open": 4000.0, "close": 4050.0, "high": 4080.0, "low": 3990.0, "amount": 4.02e9},
            {"date": "2026-07-15", "open": 4050.0, "close": 4100.0, "high": 4120.0, "low": 4040.0, "amount": 4.51e9},
            {"date": "2026-07-16", "open": 4100.0, "close": 4080.0, "high": 4130.0, "low": 4070.0, "amount": 3.88e9},
            # 范围外的数据（应被过滤）
            {"date": "2026-07-10", "open": 3900.0, "close": 3920.0, "high": 3950.0, "low": 3880.0, "amount": 3.0e9},
        ]
    )


# ----------------------------------------------------------------------------
# _to_prefixed_symbol
# ----------------------------------------------------------------------------

class TestToPrefixedSymbol:
    def test_sh_prefix_for_csi300(self):
        """沪深300（000300）属于沪市，加 sh 前缀。"""
        assert _to_prefixed_symbol("000300") == "sh000300"

    def test_sh_prefix_for_shanghai_index(self):
        """上证指数（000001）加 sh 前缀。"""
        assert _to_prefixed_symbol("000001") == "sh000001"

    def test_sz_prefix_for_shenzhen_component(self):
        """深证成指（399001）加 sz 前缀。"""
        assert _to_prefixed_symbol("399001") == "sz399001"

    def test_sz_prefix_for_chinext(self):
        """创业板指（399006）加 sz 前缀。"""
        assert _to_prefixed_symbol("399006") == "sz399006"

    def test_empty_symbol_raises(self):
        with pytest.raises(ValueError, match="symbol is required"):
            _to_prefixed_symbol("")


# ----------------------------------------------------------------------------
# _normalize_eastmoney_frame
# ----------------------------------------------------------------------------

class TestNormalizeEastmoneyFrame:
    def test_renames_chinese_columns(self):
        frame = _eastmoney_frame()
        normalized = _normalize_eastmoney_frame(frame)
        assert list(normalized.columns) == ["trade_date", "open", "high", "low", "close", "volume", "amount"]
        assert len(normalized) == 3

    def test_empty_frame_returns_empty_with_columns(self):
        normalized = _normalize_eastmoney_frame(pd.DataFrame())
        assert list(normalized.columns) == ["trade_date", "open", "high", "low", "close", "volume", "amount"]
        assert len(normalized) == 0

    def test_none_frame_returns_empty_with_columns(self):
        normalized = _normalize_eastmoney_frame(None)
        assert list(normalized.columns) == ["trade_date", "open", "high", "low", "close", "volume", "amount"]
        assert len(normalized) == 0

    def test_partial_columns_kept(self):
        """akshare 偶尔会少返回某列（如成交额），不应报错。"""
        frame = pd.DataFrame([{"日期": "2026-07-14", "开盘": 4000.0, "收盘": 4050.0, "最高": 4080.0, "最低": 3990.0}])
        normalized = _normalize_eastmoney_frame(frame)
        assert "trade_date" in normalized.columns
        assert "amount" not in normalized.columns
        assert len(normalized) == 1

    def test_trade_date_converted_to_date_type(self):
        """normalize 后 trade_date 应为 date 类型（便于后续过滤与比较）。"""
        frame = _eastmoney_frame()
        normalized = _normalize_eastmoney_frame(frame)
        assert all(isinstance(d, date) for d in normalized["trade_date"])


# ----------------------------------------------------------------------------
# _normalize_sina_frame
# ----------------------------------------------------------------------------

class TestNormalizeSinaFrame:
    def test_renames_date_column(self):
        normalized = _normalize_sina_frame(
            _sina_frame(),
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
        )
        # 新浪无 amount 列
        assert list(normalized.columns) == ["trade_date", "open", "high", "low", "close", "volume"]
        # _sina_frame() 有 5 条数据都在 7/1~7/31 范围内
        assert len(normalized) == 5

    def test_filters_by_date_range(self):
        """新浪返回全量历史，需本地按日期过滤。"""
        normalized = _normalize_sina_frame(
            _sina_frame(),
            start=date(2026, 7, 15),
            end=date(2026, 7, 16),
        )
        assert len(normalized) == 2
        assert all(d >= date(2026, 7, 15) and d <= date(2026, 7, 16) for d in normalized["trade_date"])

    def test_empty_frame_returns_empty(self):
        normalized = _normalize_sina_frame(pd.DataFrame(), start=date(2026, 7, 1), end=date(2026, 7, 31))
        assert list(normalized.columns) == ["trade_date", "open", "high", "low", "close", "volume", "amount"]
        assert len(normalized) == 0

    def test_none_returns_empty(self):
        normalized = _normalize_sina_frame(None, start=date(2026, 7, 1), end=date(2026, 7, 31))
        assert len(normalized) == 0


# ----------------------------------------------------------------------------
# _normalize_tencent_frame
# ----------------------------------------------------------------------------

class TestNormalizeTencentFrame:
    def test_renames_date_column(self):
        normalized = _normalize_tencent_frame(
            _tencent_frame(),
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
        )
        # 腾讯无 volume 列，有 amount；列顺序按 keep 逻辑统一排列
        assert list(normalized.columns) == ["trade_date", "open", "high", "low", "close", "amount"]
        # _tencent_frame() 有 4 条数据都在 7/1~7/31 范围内
        assert len(normalized) == 4

    def test_filters_by_date_range(self):
        normalized = _normalize_tencent_frame(
            _tencent_frame(),
            start=date(2026, 7, 14),
            end=date(2026, 7, 15),
        )
        assert len(normalized) == 2
        assert all(d >= date(2026, 7, 14) and d <= date(2026, 7, 15) for d in normalized["trade_date"])

    def test_empty_returns_empty(self):
        normalized = _normalize_tencent_frame(None, start=date(2026, 7, 1), end=date(2026, 7, 31))
        assert len(normalized) == 0


# ----------------------------------------------------------------------------
# _filter_by_date
# ----------------------------------------------------------------------------

class TestFilterByDate:
    def test_filters_outside_range(self):
        df = pd.DataFrame(
            [
                {"trade_date": date(2026, 7, 10), "close": 3900.0},
                {"trade_date": date(2026, 7, 14), "close": 4000.0},
                {"trade_date": date(2026, 7, 16), "close": 4080.0},
                {"trade_date": date(2026, 7, 20), "close": 4200.0},
            ]
        )
        filtered = _filter_by_date(df, start=date(2026, 7, 14), end=date(2026, 7, 16))
        assert len(filtered) == 2
        assert list(filtered["trade_date"]) == [date(2026, 7, 14), date(2026, 7, 16)]

    def test_empty_returns_empty(self):
        df = pd.DataFrame(columns=["trade_date", "close"])
        filtered = _filter_by_date(df, start=date(2026, 7, 14), end=date(2026, 7, 16))
        assert len(filtered) == 0

    def test_sorted_ascending(self):
        df = pd.DataFrame(
            [
                {"trade_date": date(2026, 7, 16), "close": 4080.0},
                {"trade_date": date(2026, 7, 14), "close": 4000.0},
                {"trade_date": date(2026, 7, 15), "close": 4050.0},
            ]
        )
        filtered = _filter_by_date(df, start=date(2026, 7, 1), end=date(2026, 7, 31))
        assert list(filtered["trade_date"]) == [date(2026, 7, 14), date(2026, 7, 15), date(2026, 7, 16)]


# ----------------------------------------------------------------------------
# _fetch_index_daily fallback 链
# ----------------------------------------------------------------------------

class TestFetchIndexDailyFallback:
    """测试东财→新浪→腾讯 fallback 链。

    通过 patch 三个 _fetch_from_xxx 函数模拟成功/失败/空响应。
    """

    def test_eastmoney_success_no_fallback(self, db_session):
        """东财主源成功时，不应调用新浪/腾讯。"""
        sina_calls = []
        tencent_calls = []

        def _sina_spy(*args, **kwargs):
            sina_calls.append(1)
            return pd.DataFrame()

        def _tencent_spy(*args, **kwargs):
            tencent_calls.append(1)
            return pd.DataFrame()

        with patch("app.services.index_data._fetch_from_eastmoney", return_value=_normalize_eastmoney_frame(_eastmoney_frame())), \
             patch("app.services.index_data._fetch_from_sina", side_effect=_sina_spy), \
             patch("app.services.index_data._fetch_from_tencent", side_effect=_tencent_spy):
            frame, source = _fetch_index_daily(db_session, "000300", date(2026, 7, 14), date(2026, 7, 16))

        assert not frame.empty
        assert source == "akshare"
        assert len(sina_calls) == 0
        assert len(tencent_calls) == 0

    def test_eastmoney_fail_sina_success(self, db_session):
        """东财抛异常时，自动 fallback 到新浪。"""
        def _eastmoney_boom(*args, **kwargs):
            raise ConnectionError("EastMoney WAF blocked")

        with patch("app.services.index_data._fetch_from_eastmoney", side_effect=_eastmoney_boom), \
             patch("app.services.index_data._fetch_from_sina", return_value=_normalize_sina_frame(_sina_frame(), date(2026, 7, 14), date(2026, 7, 16))), \
             patch("app.services.index_data._fetch_from_tencent") as tencent_mock:
            frame, source = _fetch_index_daily(db_session, "000300", date(2026, 7, 14), date(2026, 7, 16))

        assert not frame.empty
        assert source == "sina"
        assert len(frame) == 3
        # 腾讯不应被调用
        tencent_mock.assert_not_called()

    def test_eastmoney_empty_sina_success(self, db_session):
        """东财返回空 DataFrame（非异常）时，也应 fallback 到新浪。"""
        from app.services.index_data import _empty_frame
        with patch("app.services.index_data._fetch_from_eastmoney", return_value=_empty_frame()), \
             patch("app.services.index_data._fetch_from_sina", return_value=_normalize_sina_frame(_sina_frame(), date(2026, 7, 14), date(2026, 7, 16))):
            frame, source = _fetch_index_daily(db_session, "000300", date(2026, 7, 14), date(2026, 7, 16))

        assert not frame.empty
        assert source == "sina"

    def test_eastmoney_sina_fail_tencent_success(self, db_session):
        """东财+新浪都失败时，最终 fallback 到腾讯。"""
        with patch("app.services.index_data._fetch_from_eastmoney", side_effect=ConnectionError("em blocked")), \
             patch("app.services.index_data._fetch_from_sina", side_effect=ConnectionError("sina blocked")), \
             patch("app.services.index_data._fetch_from_tencent", return_value=_normalize_tencent_frame(_tencent_frame(), date(2026, 7, 14), date(2026, 7, 16))):
            frame, source = _fetch_index_daily(db_session, "000300", date(2026, 7, 14), date(2026, 7, 16))

        assert not frame.empty
        assert source == "tencent"
        assert len(frame) == 3

    def test_all_fail_returns_empty_with_default_source(self, db_session):
        """三个数据源全部失败时，返回空 frame + 默认 source='akshare'。"""
        with patch("app.services.index_data._fetch_from_eastmoney", side_effect=ConnectionError("em")), \
             patch("app.services.index_data._fetch_from_sina", side_effect=ConnectionError("sina")), \
             patch("app.services.index_data._fetch_from_tencent", side_effect=ConnectionError("tx")):
            frame, source = _fetch_index_daily(db_session, "000300", date(2026, 7, 14), date(2026, 7, 16))

        assert frame.empty
        assert source == "akshare"  # 向后兼容默认值

    def test_all_empty_returns_empty_with_default_source(self, db_session):
        """三个数据源都返回空（非异常）时，同样返回空 + 默认 source。"""
        from app.services.index_data import _empty_frame
        with patch("app.services.index_data._fetch_from_eastmoney", return_value=_empty_frame()), \
             patch("app.services.index_data._fetch_from_sina", return_value=_empty_frame()), \
             patch("app.services.index_data._fetch_from_tencent", return_value=_empty_frame()):
            frame, source = _fetch_index_daily(db_session, "000300", date(2026, 7, 14), date(2026, 7, 16))

        assert frame.empty
        assert source == "akshare"


# ----------------------------------------------------------------------------
# sync_index_daily（保留原测试，更新 patch path）
# ----------------------------------------------------------------------------

class TestSyncIndexDaily:
    def test_sync_inserts_rows(self, db_session):
        with patch(
            "app.services.index_data.call_akshare_with_retry",
            return_value=_eastmoney_frame(),
        ) as fetch:
            result = sync_index_daily(
                db_session, "000300",
                start_date=date(2026, 7, 14),
                end_date=date(2026, 7, 16),
            )

        assert result.symbol == "000300"
        assert result.received == 3
        assert result.written == 3
        assert result.skipped == 0
        assert result.date_range == (date(2026, 7, 14), date(2026, 7, 16))
        # 校验 api_key 透传（东财主源）
        assert fetch.call_args.kwargs["api_key"] == "index_zh_a_hist"
        assert fetch.call_args.kwargs["symbol"] == "000300"
        assert fetch.call_args.kwargs["period"] == "daily"

        rows = db_session.execute(
            select(IndexPrice).where(IndexPrice.symbol == "000300").order_by(IndexPrice.trade_date)
        ).scalars().all()
        assert len(rows) == 3
        assert rows[0].trade_date == date(2026, 7, 14)
        assert rows[0].open == 4000.0
        assert rows[0].close == 4050.0
        assert rows[0].high == 4080.0
        assert rows[0].low == 3990.0
        assert rows[0].volume == 1000000.0
        assert rows[0].amount == pytest.approx(4.02e9)
        assert rows[0].source == "akshare"

    def test_sync_idempotent_upsert(self, db_session):
        """重复同步相同日期范围：不新增行，但更新已有行。"""
        modified_frame = pd.DataFrame(
            [{"日期": "2026-07-14", "开盘": 4100.0, "收盘": 4200.0, "最高": 4250.0, "最低": 4090.0, "成交量": 1200000.0, "成交额": 5.0e9}]
        )
        with patch("app.services.index_data.call_akshare_with_retry", return_value=_eastmoney_frame()):
            first = sync_index_daily(
                db_session, "000300",
                start_date=date(2026, 7, 14),
                end_date=date(2026, 7, 16),
            )
        with patch("app.services.index_data.call_akshare_with_retry", return_value=modified_frame):
            second = sync_index_daily(
                db_session, "000300",
                start_date=date(2026, 7, 14),
                end_date=date(2026, 7, 14),
            )

        assert first.written == 3
        assert second.written == 1  # 只更新了 1 行
        rows = db_session.execute(
            select(IndexPrice).where(IndexPrice.symbol == "000300", IndexPrice.trade_date == date(2026, 7, 14))
        ).scalars().all()
        assert len(rows) == 1
        # 数据已被覆盖
        assert rows[0].open == 4100.0
        assert rows[0].close == 4200.0
        assert rows[0].volume == 1200000.0

    def test_sync_empty_response_returns_zero(self, db_session):
        """所有数据源都返回空时，sync_index_daily 返回 0 统计。"""
        with patch("app.services.index_data.call_akshare_with_retry", return_value=pd.DataFrame()):
            result = sync_index_daily(
                db_session, "000300",
                start_date=date(2026, 7, 14),
                end_date=date(2026, 7, 16),
            )
        assert result.received == 0
        assert result.written == 0
        assert result.date_range == (None, None)
        # 无行入库
        rows = db_session.execute(select(IndexPrice)).scalars().all()
        assert len(rows) == 0

    def test_sync_none_response_returns_zero(self, db_session):
        with patch("app.services.index_data.call_akshare_with_retry", return_value=None):
            result = sync_index_daily(
                db_session, "000300",
                start_date=date(2026, 7, 14),
                end_date=date(2026, 7, 16),
            )
        assert result.received == 0
        assert result.written == 0

    def test_sync_empty_symbol_raises(self, db_session):
        with pytest.raises(ValueError, match="symbol is required"):
            sync_index_daily(db_session, "")

    def test_sync_default_dates_5_year_backfill(self, db_session):
        """未传 start_date/end_date 时默认回补 5 年。"""
        with patch("app.services.index_data.call_akshare_with_retry", return_value=_eastmoney_frame()) as fetch:
            sync_index_daily(db_session, "000300")

        # 校验调用时 start_date 是 5 年前
        start_arg = fetch.call_args.kwargs["start_date"]
        end_arg = fetch.call_args.kwargs["end_date"]
        # YYYYMMDD 格式
        assert len(start_arg) == 8
        assert len(end_arg) == 8
        # end_date 应该是今天
        today_str = date.today().strftime("%Y%m%d")
        assert end_arg == today_str
        # start_date 的年份应该是今天年份 - 5
        start_year = int(start_arg[:4])
        assert start_year == date.today().year - 5

    def test_sync_skips_invalid_date_row(self, db_session):
        """某行日期为空/NaN 时跳过，不阻塞其他行。"""
        bad_frame = pd.DataFrame(
            [
                {"日期": "2026-07-14", "开盘": 4000.0, "收盘": 4050.0, "最高": 4080.0, "最低": 3990.0, "成交量": 1000000.0, "成交额": 4.02e9},
                {"日期": None, "开盘": 4000.0, "收盘": 4050.0, "最高": 4080.0, "最低": 3990.0, "成交量": 1000000.0, "成交额": 4.02e9},
            ]
        )
        with patch("app.services.index_data.call_akshare_with_retry", return_value=bad_frame):
            result = sync_index_daily(
                db_session, "000300",
                start_date=date(2026, 7, 14),
                end_date=date(2026, 7, 16),
            )

        assert result.received == 2
        assert result.written == 1
        assert result.skipped == 1

    def test_sync_different_symbols_isolated(self, db_session):
        """不同 symbol 的数据互相隔离。"""
        with patch("app.services.index_data.call_akshare_with_retry", return_value=_eastmoney_frame()):
            sync_index_daily(db_session, "000300", start_date=date(2026, 7, 14), end_date=date(2026, 7, 16))
            sync_index_daily(db_session, "000001", start_date=date(2026, 7, 14), end_date=date(2026, 7, 16))

        csi300_rows = db_session.execute(
            select(IndexPrice).where(IndexPrice.symbol == "000300")
        ).scalars().all()
        sh_rows = db_session.execute(
            select(IndexPrice).where(IndexPrice.symbol == "000001")
        ).scalars().all()
        assert len(csi300_rows) == 3
        assert len(sh_rows) == 3

    def test_sync_records_source_from_sina_fallback(self, db_session):
        """东财失败走新浪 fallback 时，写入的 IndexPrice.source 应为 'sina'。"""
        # 直接 patch _fetch_from_eastmoney / _fetch_from_sina，避免 call_akshare_with_retry 内部重试干扰
        with patch("app.services.index_data._fetch_from_eastmoney", side_effect=ConnectionError("WAF blocked")), \
             patch("app.services.index_data._fetch_from_sina",
                   return_value=_normalize_sina_frame(_sina_frame(), date(2026, 7, 14), date(2026, 7, 16))):
            result = sync_index_daily(
                db_session, "000300",
                start_date=date(2026, 7, 14),
                end_date=date(2026, 7, 16),
            )

        assert result.written == 3
        rows = db_session.execute(
            select(IndexPrice).where(IndexPrice.symbol == "000300")
        ).scalars().all()
        assert all(r.source == "sina" for r in rows)


# ----------------------------------------------------------------------------
# list_index_prices
# ----------------------------------------------------------------------------

class TestListIndexPrices:
    def _seed(self, db_session, symbol: str):
        with patch("app.services.index_data.call_akshare_with_retry", return_value=_eastmoney_frame()):
            sync_index_daily(db_session, symbol, start_date=date(2026, 7, 14), end_date=date(2026, 7, 16))

    def test_returns_ascending_by_date(self, db_session):
        self._seed(db_session, "000300")
        rows = list_index_prices(db_session, "000300")
        assert len(rows) == 3
        assert rows[0].trade_date == date(2026, 7, 14)
        assert rows[1].trade_date == date(2026, 7, 15)
        assert rows[2].trade_date == date(2026, 7, 16)

    def test_date_filter(self, db_session):
        self._seed(db_session, "000300")
        rows = list_index_prices(
            db_session, "000300",
            start_date=date(2026, 7, 15),
            end_date=date(2026, 7, 15),
        )
        assert len(rows) == 1
        assert rows[0].trade_date == date(2026, 7, 15)

    def test_unknown_symbol_returns_empty(self, db_session):
        self._seed(db_session, "000300")
        rows = list_index_prices(db_session, "999999")
        assert rows == []

    def test_limit_applied(self, db_session):
        self._seed(db_session, "000300")
        rows = list_index_prices(db_session, "000300", limit=2)
        assert len(rows) == 2
        # limit 取最早的 2 条（asc 排序）
        assert rows[0].trade_date == date(2026, 7, 14)
        assert rows[1].trade_date == date(2026, 7, 15)
