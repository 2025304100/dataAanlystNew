"""TDX(通达信)本地文件数据源 adapter。

借鉴 EasyXT 利用通达信本地文件的思路:通达信客户端免费,行情数据下载到本地 .day 二进制文件,
读本地文件零网络、零频率限制、零成本,适合作为 akshare 风控触发时的兜底源。

限制:
- 仅支持 SH/SZ A 股(不支持 ETF / 可转债 / 北交所 / 美股)
- 不提供复权因子(返回不复权价)
- 不提供 turnover_rate(该列留空)
- 数据新鲜度依赖用户手动下载或通达信自动下载
"""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from app.services.market_data_sources.sources._normalize import standard_history_frame
from app.services.market_data_sources.sources.base import HistorySource, SourceUnavailable
from app.services.market_data_sources.tdx_parser import (
    detect_tdx_path,
    parse_day_file,
    tdx_file_path,
)

logger = logging.getLogger(__name__)


class TDXHistorySource:
    """通达信本地 .day 文件数据源。

    实现 HistorySource Protocol。
    """

    name: str = "tdx"
    region: str = "cn"
    asset_types: list[str] = ["stock"]

    def __init__(self) -> None:
        self.tdx_path: str | None = detect_tdx_path()

    def available(self) -> bool:
        """该源是否可用(通达信路径已检测到)。"""
        return self.tdx_path is not None

    def supports(self, symbol) -> bool:
        """是否支持指定 symbol。

        支持:cn 区域 + stock 类型 + SH/SZ 市场
        不支持:ETF / 北交所 / 美股 / 港股
        """
        if symbol.asset_type != "stock":
            return False
        market = (symbol.market or "").lower()
        # 仅支持 SH/SZ,不支持北交所(BJ)/可转债/ETF
        if market not in {"sh", "sz"}:
            return False
        return True

    def fetch(self, symbol, start_date: date, end_date: date, adjust: str) -> pd.DataFrame:
        """从通达信 .day 文件读取行情。

        Args:
            symbol: Symbol 模型实例
            start_date: 起始日期(含)
            end_date: 结束日期(含)
            adjust: 复权方式(通达信不支持复权,忽略此参数,返回不复权价)

        Returns:
            标准化 DataFrame

        Raises:
            SourceUnavailable: 文件不存在 / 解析失败
        """
        if not self.available():
            raise SourceUnavailable(f"TDX path not detected for {symbol.symbol}")
        try:
            filepath = tdx_file_path(self.tdx_path, symbol)  # type: ignore[arg-type]
            frame = parse_day_file(filepath, start=start_date, end=end_date)
            if frame.empty:
                # 文件不存在或无数据,抛 SourceUnavailable 触发降级
                raise SourceUnavailable(f"TDX file empty or not found: {filepath}")

            # 补齐 standard_history_frame 需要的列
            frame["turnover_rate"] = None
            # parse_day_file 返回列:trade_date, open, high, low, close, amount, volume
            # standard_history_frame 期望:trade_date, open, high, low, close, volume, amount, turnover_rate
            frame = frame[["trade_date", "open", "high", "low", "close", "volume", "amount", "turnover_rate"]]
            normalized = standard_history_frame(frame)
            logger.info(
                "TDX fetched %d rows for %s (%s ~ %s)",
                len(normalized), symbol.symbol, start_date, end_date,
            )
            return normalized
        except SourceUnavailable:
            raise
        except Exception as e:
            raise SourceUnavailable(f"TDX parse failed for {symbol.symbol}: {e}") from e
