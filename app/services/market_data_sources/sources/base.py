"""行情数据源统一接口定义。

借鉴 EasyXT 的 SourceChain 设计:每个数据源是独立 adapter,实现统一 Protocol;
降级链由 SourceChain 编排,业务代码零感知。

设计要点:
- 每个 source 失败时抛 SourceUnavailable,触发降级链 next
- 成功时短路,返回标准化 DataFrame
- 支持 region / asset_type 路由,不支持的场景自动跳过
"""
from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

import pandas as pd


class SourceUnavailable(Exception):
    """数据源不可用,触发降级链 next。

    不同于 RuntimeError/ValueError,这个异常专门用于"该源这次不可用,请尝试下一个源"的语义。
    例如:
    - akshare 接口风控触发 → SourceUnavailable
    - TDX 文件不存在 → SourceUnavailable
    - 网络瞬时错误 → SourceUnavailable
    """
    pass


@runtime_checkable
class HistorySource(Protocol):
    """行情数据源统一接口。

    每个具体数据源(东财/新浪/腾讯/TDX/Tushare 等)实现此接口。

    Attributes:
        name: 源标识(em / sina / tx / tdx / tushare),用于日志和配置
        region: 支持的区域(cn / us / hk)
        asset_types: 支持的资产类型(stock / etf)

    Methods:
        fetch: 拉取行情,失败抛 SourceUnavailable
        available: 该源是否可用(配置/环境检查)
        supports: 是否支持指定 symbol(region + asset_type 匹配)
    """

    name: str
    region: str
    asset_types: list[str]

    def available(self) -> bool:
        """该源是否可用(配置/环境检查)。

        Returns:
            True 表示该源已配置且可调用;False 表示未配置(如 TDX 路径不存在)
        """
        ...

    def supports(self, symbol) -> bool:
        """是否支持指定 symbol。

        Args:
            symbol: Symbol 模型实例,需检查 market 和 asset_type

        Returns:
            True 表示该源支持该标的;False 表示不支持(降级链会跳过)
        """
        ...

    def fetch(self, symbol, start_date: date, end_date: date, adjust: str) -> pd.DataFrame:
        """拉取行情数据。

        Args:
            symbol: Symbol 模型实例
            start_date: 起始日期(含)
            end_date: 结束日期(含)
            adjust: 复权方式("" 不复权 / "qfq" 前复权 / "hfq" 后复权)

        Returns:
            标准化 DataFrame,列:trade_date, open, high, low, close, volume, amount, turnover_rate

        Raises:
            SourceUnavailable: 该源本次不可用,触发降级链 next
        """
        ...
