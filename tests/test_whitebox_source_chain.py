"""SourceChain + TDX 数据源白盒测试。

测试范围:
- TestTDXParser: tdx_parser.parse_day_file / detect_tdx_path / symbol_to_tdx_code / tdx_file_path
- TestTDXHelpers: 辅助函数(symbol_to_tdx_code / tdx_file_path)
- TestTDXSource: TDXHistorySource adapter (available/supports/fetch)
- TestSourceChain: SourceChain 降级链编排(short-circuit/fallback/all fail/skip/empty)

不依赖 DB,纯单元测试。
"""
from __future__ import annotations

import struct
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

pytestmark = pytest.mark.whitebox

from app.services.market_data_sources.chain import SourceChain
from app.services.market_data_sources.sources.base import HistorySource, SourceUnavailable
from app.services.market_data_sources.sources.tdx_source import TDXHistorySource
from app.services.market_data_sources.tdx_parser import (
    RECORD_FORMAT,
    detect_tdx_path,
    parse_day_file,
    symbol_to_tdx_code,
    tdx_file_path,
)


def _make_day_record(
    date_int: int,
    open_cents: int,
    high: float,
    low: float,
    close: float,
    amount: float,
    volume: int,
) -> bytes:
    """构造一条 32 字节的 .day 记录。"""
    return struct.pack(RECORD_FORMAT, date_int, open_cents, high, low, close, amount, volume, 0)


def _make_symbol(symbol: str = "000001", market: str = "SZ", asset_type: str = "stock"):
    """构造一个 mock Symbol 对象。"""
    sym = MagicMock()
    sym.symbol = symbol
    sym.market = market
    sym.asset_type = asset_type
    return sym


# ====================================================================
# TestTDXParser: parse_day_file 测试
# ====================================================================
class TestTDXParser:
    """parse_day_file 二进制解析测试。"""

    def test_parse_normal_file(self, tmp_path: Path):
        """正常文件:多条记录,验证字段解析(open /100,其他原值)。"""
        day_file = tmp_path / "sz000001.day"
        day_file.write_bytes(
            _make_day_record(20240101, 1000, 10.5, 9.8, 10.2, 1.0e8, 100000)
            + _make_day_record(20240102, 1020, 10.8, 10.1, 10.6, 1.2e8, 110000)
        )
        df = parse_day_file(day_file)
        assert len(df) == 2
        assert list(df.columns) == ["trade_date", "open", "high", "low", "close", "amount", "volume"]
        # open 是"分",需 /100(open 是 int32 精确存储,无精度问题)
        assert df.iloc[0]["open"] == 10.0  # 1000 / 100
        assert df.iloc[1]["open"] == 10.2  # 1020 / 100
        # high/low/close 是 float32 存储有精度误差,用 approx 比较
        assert df.iloc[0]["high"] == pytest.approx(10.5, abs=1e-5)
        assert df.iloc[0]["close"] == pytest.approx(10.2, abs=1e-5)
        assert df.iloc[0]["volume"] == 100000

    def test_parse_empty_file(self, tmp_path: Path):
        """空文件(0 字节):返回空 DataFrame,不抛异常。"""
        day_file = tmp_path / "empty.day"
        day_file.write_bytes(b"")
        df = parse_day_file(day_file)
        assert df.empty
        assert list(df.columns) == ["trade_date", "open", "high", "low", "close", "amount", "volume"]

    def test_parse_nonexistent_file(self, tmp_path: Path):
        """不存在的文件:返回空 DataFrame,不抛异常。"""
        df = parse_day_file(tmp_path / "nonexistent.day")
        assert df.empty
        assert list(df.columns) == ["trade_date", "open", "high", "low", "close", "amount", "volume"]

    def test_parse_with_date_filter(self, tmp_path: Path):
        """日期过滤:start/end 参数过滤掉范围外的记录。"""
        day_file = tmp_path / "sz000001.day"
        day_file.write_bytes(
            _make_day_record(20240101, 1000, 10.5, 9.8, 10.2, 1.0e8, 100000)
            + _make_day_record(20240115, 1020, 10.8, 10.1, 10.6, 1.2e8, 110000)
            + _make_day_record(20240131, 1040, 11.0, 10.3, 10.8, 1.5e8, 120000)
        )
        df = parse_day_file(day_file, start=date(2024, 1, 10), end=date(2024, 1, 20))
        assert len(df) == 1
        assert df.iloc[0]["open"] == 10.2  # 20240115 记录

    def test_open_divided_by_100(self, tmp_path: Path):
        """open 字段除以 100:通达信存储单位是"分"。"""
        day_file = tmp_path / "test.day"
        day_file.write_bytes(_make_day_record(20240101, 12345, 123.0, 122.0, 122.5, 1.0e8, 1000))
        df = parse_day_file(day_file)
        assert df.iloc[0]["open"] == 123.45  # 12345 / 100

    def test_records_sorted_by_date(self, tmp_path: Path):
        """记录按日期升序排序:即使文件中乱序存储。"""
        day_file = tmp_path / "test.day"
        day_file.write_bytes(
            _make_day_record(20240103, 1030, 0.0, 0.0, 0.0, 0.0, 0)
            + _make_day_record(20240101, 1010, 0.0, 0.0, 0.0, 0.0, 0)
            + _make_day_record(20240102, 1020, 0.0, 0.0, 0.0, 0.0, 0)
        )
        df = parse_day_file(day_file)
        assert len(df) == 3
        # 验证按日期升序
        dates = list(df["trade_date"])
        assert dates == sorted(dates)
        assert df.iloc[0]["open"] == 10.10  # 20240101 的 open

    def test_partial_record_ignored(self, tmp_path: Path):
        """不完整记录(不足 32 字节)被忽略,不报错。"""
        day_file = tmp_path / "test.day"
        # 完整记录 + 16 字节残缺记录
        day_file.write_bytes(
            _make_day_record(20240101, 1000, 10.0, 9.5, 9.8, 1.0e8, 100000)
            + b"\x00" * 16  # 不足 32 字节
        )
        df = parse_day_file(day_file)
        assert len(df) == 1  # 只解析到完整的那条


# ====================================================================
# TestTDXHelpers: 辅助函数测试
# ====================================================================
class TestTDXHelpers:
    """symbol_to_tdx_code / tdx_file_path 辅助函数测试。"""

    def test_symbol_to_tdx_code_sz(self):
        """深交所股票 -> sz 前缀。"""
        sym = _make_symbol("000001", "SZ")
        assert symbol_to_tdx_code(sym) == "sz000001"

    def test_symbol_to_tdx_code_sh(self):
        """上交所股票 -> sh 前缀。"""
        sym = _make_symbol("600000", "SH")
        assert symbol_to_tdx_code(sym) == "sh600000"

    def test_symbol_to_tdx_code_by_prefix(self):
        """无 market 字段时按 symbol 前缀推断:6/5/9 开头 -> sh。"""
        sym = _make_symbol("600000", None)
        assert symbol_to_tdx_code(sym) == "sh600000"
        sym2 = _make_symbol("000001", None)
        assert symbol_to_tdx_code(sym2) == "sz000001"

    def test_tdx_file_path_construction(self):
        """文件路径构造:vipdoc/<sh|sz>/lday/<code>.day。"""
        sym = _make_symbol("600000", "SH")
        path = tdx_file_path("C:\\new_tdx", sym)
        assert path == Path("C:\\new_tdx\\vipdoc\\sh\\lday\\sh600000.day")


# ====================================================================
# TestTDXSource: TDXHistorySource adapter 测试
# ====================================================================
class TestTDXSource:
    """TDXHistorySource adapter 测试。"""

    def test_available_true_when_path_set(self):
        """检测到路径时 available()=True。"""
        source = TDXHistorySource()
        source.tdx_path = "/fake/tdx"
        assert source.available() is True

    def test_available_false_when_path_none(self):
        """未检测到路径时 available()=False。"""
        source = TDXHistorySource()
        source.tdx_path = None
        assert source.available() is False

    def test_supports_cn_stock_sh(self):
        """支持 cn 区域 + stock + SH 市场。"""
        source = TDXHistorySource()
        assert source.supports(_make_symbol("600000", "SH", "stock")) is True

    def test_supports_cn_stock_sz(self):
        """支持 cn 区域 + stock + SZ 市场。"""
        source = TDXHistorySource()
        assert source.supports(_make_symbol("000001", "SZ", "stock")) is True

    def test_not_supports_etf(self):
        """不支持 ETF。"""
        source = TDXHistorySource()
        assert source.supports(_make_symbol("510300", "SH", "etf")) is False

    def test_not_supports_bj(self):
        """不支持北交所(BJ)。"""
        source = TDXHistorySource()
        assert source.supports(_make_symbol("430047", "BJ", "stock")) is False

    def test_not_supports_us(self):
        """不支持美股。"""
        source = TDXHistorySource()
        assert source.supports(_make_symbol("AAPL", "US", "stock")) is False

    def test_fetch_success(self, tmp_path: Path):
        """fetch 成功:从 .day 文件读取并标准化。"""
        # 准备 .day 文件
        day_file = tmp_path / "vipdoc" / "sz" / "lday" / "sz000001.day"
        day_file.parent.mkdir(parents=True)
        day_file.write_bytes(
            _make_day_record(20240101, 1000, 10.5, 9.8, 10.2, 1.0e8, 100000)
            + _make_day_record(20240102, 1020, 10.8, 10.1, 10.6, 1.2e8, 110000)
        )

        source = TDXHistorySource()
        source.tdx_path = str(tmp_path)
        sym = _make_symbol("000001", "SZ", "stock")
        df = source.fetch(sym, date(2024, 1, 1), date(2024, 1, 31), "qfq")
        assert len(df) == 2
        assert "trade_date" in df.columns
        assert "open" in df.columns
        assert df.iloc[0]["open"] == 10.0

    def test_fetch_unavailable_raises(self):
        """available()=False 时 fetch 抛 SourceUnavailable。"""
        source = TDXHistorySource()
        source.tdx_path = None
        sym = _make_symbol("000001", "SZ", "stock")
        with pytest.raises(SourceUnavailable):
            source.fetch(sym, date(2024, 1, 1), date(2024, 1, 31), "qfq")

    def test_fetch_empty_file_raises(self, tmp_path: Path):
        """空 .day 文件 -> SourceUnavailable(触发降级)。"""
        day_file = tmp_path / "vipdoc" / "sz" / "lday" / "sz000001.day"
        day_file.parent.mkdir(parents=True)
        day_file.write_bytes(b"")

        source = TDXHistorySource()
        source.tdx_path = str(tmp_path)
        sym = _make_symbol("000001", "SZ", "stock")
        with pytest.raises(SourceUnavailable):
            source.fetch(sym, date(2024, 1, 1), date(2024, 1, 31), "qfq")


# ====================================================================
# TestSourceChain: SourceChain 降级链编排测试
# ====================================================================
class TestSourceChain:
    """SourceChain 降级链测试。"""

    def _make_source(
        self,
        name: str = "mock",
        available: bool = True,
        supports: bool = True,
        frame: pd.DataFrame | None = None,
        raise_unavailable: bool = False,
        raise_other: Exception | None = None,
    ):
        """构造 mock source。"""
        source = MagicMock()
        source.name = name
        source.available.return_value = available
        source.supports.return_value = supports
        if raise_other is not None:
            source.fetch.side_effect = raise_other
        elif raise_unavailable:
            source.fetch.side_effect = SourceUnavailable(f"{name} unavailable")
        else:
            source.fetch.return_value = frame if frame is not None else pd.DataFrame([{"open": 1.0}])
        return source

    def test_short_circuit_on_first_success(self):
        """第一个源成功 -> 短路,不调用后续源。"""
        s1 = self._make_source(name="s1", frame=pd.DataFrame([{"open": 10.0}]))
        s2 = self._make_source(name="s2")
        chain = SourceChain([s1, s2])
        sym = _make_symbol()
        df = chain.fetch(sym, date(2024, 1, 1), date(2024, 1, 31), "qfq")
        assert len(df) == 1
        assert df.iloc[0]["open"] == 10.0
        s2.fetch.assert_not_called()

    def test_fallback_on_source_unavailable(self):
        """第一个源 SourceUnavailable -> 尝试第二个 -> 成功。"""
        s1 = self._make_source(name="s1", raise_unavailable=True)
        s2 = self._make_source(name="s2", frame=pd.DataFrame([{"open": 20.0}]))
        chain = SourceChain([s1, s2])
        df = chain.fetch(_make_symbol(), date(2024, 1, 1), date(2024, 1, 31), "qfq")
        assert df.iloc[0]["open"] == 20.0
        s1.fetch.assert_called_once()
        s2.fetch.assert_called_once()

    def test_all_fail_raises_runtime_error(self):
        """所有源都 SourceUnavailable -> RuntimeError。"""
        s1 = self._make_source(name="s1", raise_unavailable=True)
        s2 = self._make_source(name="s2", raise_unavailable=True)
        chain = SourceChain([s1, s2])
        with pytest.raises(RuntimeError, match="All 2 sources failed"):
            chain.fetch(_make_symbol(), date(2024, 1, 1), date(2024, 1, 31), "qfq")

    def test_skip_unavailable_source(self):
        """available()=False 的源被跳过,不计入失败。"""
        s1 = self._make_source(name="s1", available=False)
        s2 = self._make_source(name="s2", frame=pd.DataFrame([{"open": 30.0}]))
        chain = SourceChain([s1, s2])
        df = chain.fetch(_make_symbol(), date(2024, 1, 1), date(2024, 1, 31), "qfq")
        assert df.iloc[0]["open"] == 30.0
        s1.fetch.assert_not_called()
        s2.fetch.assert_called_once()

    def test_skip_unsupported_source(self):
        """supports()=False 的源被跳过。"""
        s1 = self._make_source(name="s1", supports=False)
        s2 = self._make_source(name="s2", frame=pd.DataFrame([{"open": 40.0}]))
        chain = SourceChain([s1, s2])
        df = chain.fetch(_make_symbol(), date(2024, 1, 1), date(2024, 1, 31), "qfq")
        assert df.iloc[0]["open"] == 40.0
        s1.fetch.assert_not_called()

    def test_empty_frame_is_success(self):
        """空 DataFrame 是有效结果,不触发降级(短路返回空)。"""
        s1 = self._make_source(name="s1", frame=pd.DataFrame())
        s2 = self._make_source(name="s2", frame=pd.DataFrame([{"open": 50.0}]))
        chain = SourceChain([s1, s2])
        df = chain.fetch(_make_symbol(), date(2024, 1, 1), date(2024, 1, 31), "qfq")
        assert df.empty
        s2.fetch.assert_not_called()  # 短路,未调用 s2

    def test_non_source_unavailable_propagates(self):
        """非 SourceUnavailable 异常(如 KeyError)直接向上抛,不降级。"""
        s1 = self._make_source(name="s1", raise_other=KeyError("missing column"))
        s2 = self._make_source(name="s2")
        chain = SourceChain([s1, s2])
        with pytest.raises(KeyError):
            chain.fetch(_make_symbol(), date(2024, 1, 1), date(2024, 1, 31), "qfq")
        s2.fetch.assert_not_called()  # 异常向上抛,未降级

    def test_no_source_tried_raises(self):
        """所有源都 unavailable/unsupported -> RuntimeError(0 tried)。"""
        s1 = self._make_source(name="s1", available=False)
        s2 = self._make_source(name="s2", supports=False)
        chain = SourceChain([s1, s2])
        with pytest.raises(RuntimeError, match="No source available"):
            chain.fetch(_make_symbol(), date(2024, 1, 1), date(2024, 1, 31), "qfq")
