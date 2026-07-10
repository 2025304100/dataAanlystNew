"""SourceChain 降级链编排器。

借鉴 EasyXT 的 SourceChain 设计:多个数据源按优先级组成降级链,
前一个失败(抛 SourceUnavailable)自动尝试下一个,直到成功或全部失败。

设计要点:
- 业务代码只调 chain.fetch(),不感知具体源
- 每个源失败时抛 SourceUnavailable,触发降级链 next
- 成功时短路,返回标准化 DataFrame(空 DataFrame 也是有效结果)
- 不支持(available/supports 返回 False)的源自动跳过,不计入失败
- 非 SourceUnavailable 异常向上抛出(可能是 bug,不应静默吞没)
"""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from app.services.akshare_utils import quiet_akshare_output
from app.services.market_data_sources.sources.base import HistorySource, SourceUnavailable

logger = logging.getLogger(__name__)


class SourceChain:
    """多源降级链。

    按顺序尝试多个数据源,前源失败自动降级到下一源。

    Attributes:
        sources: 数据源列表(按优先级排序,前面的优先)
    """

    def __init__(self, sources: list[HistorySource]) -> None:
        self.sources = list(sources)

    def fetch(self, symbol, start_date: date, end_date: date, adjust: str) -> pd.DataFrame:
        """按顺序尝试各数据源,返回第一个成功的结果。

        Args:
            symbol: Symbol 模型实例
            start_date: 起始日期(含)
            end_date: 结束日期(含)
            adjust: 复权方式("" / "qfq" / "hfq")

        Returns:
            标准化 DataFrame(空 DataFrame 也是有效结果,表示该标的确实无数据)

        Raises:
            RuntimeError: 所有源都不可用 / 不支持 / 抛 SourceUnavailable
            其他异常: 源抛出的非 SourceUnavailable 异常(如数据格式 bug)直接向上抛
        """
        if not self.sources:
            raise RuntimeError(f"No sources in chain for {symbol.symbol}")

        tried: list[str] = []
        last_error: Exception | None = None

        with quiet_akshare_output():
            for source in self.sources:
                source_name = getattr(source, "name", "unknown")

                # 跳过不可用的源(如 TDX 路径未配置)
                try:
                    if not source.available():
                        logger.debug("source %s not available, skipping", source_name)
                        continue
                except Exception as exc:
                    logger.warning("source %s.available() raised %s, skipping", source_name, exc, exc_info=True)
                    continue

                # 跳过不支持的标的(如 TDX 不支持 ETF)
                try:
                    if not source.supports(symbol):
                        logger.debug("source %s does not support %s, skipping", source_name, symbol.symbol)
                        continue
                except Exception as exc:
                    logger.warning("source %s.supports() raised %s, skipping", source_name, exc, exc_info=True)
                    continue

                tried.append(source_name)
                try:
                    frame = source.fetch(symbol, start_date, end_date, adjust)
                    # 成功,短路返回(空 DataFrame 也是有效结果)
                    logger.info("source %s succeeded for %s (%d rows)", source_name, symbol.symbol, len(frame))
                    return frame
                except SourceUnavailable as exc:
                    logger.info("source %s unavailable for %s: %s, trying next", source_name, symbol.symbol, exc)
                    last_error = exc
                    continue
                # 非 SourceUnavailable 异常(如 KeyError/ValueError)直接向上抛,可能是 bug

        if last_error is not None:
            raise RuntimeError(
                f"All {len(tried)} sources failed for {symbol.symbol} (tried: {', '.join(tried)}): {last_error}"
            ) from last_error
        raise RuntimeError(
            f"No source available/supports for {symbol.symbol} "
            f"(chain has {len(self.sources)} sources, 0 tried)"
        )
