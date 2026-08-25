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
from dataclasses import dataclass
from datetime import date

import pandas as pd

from app.services.akshare_utils import quiet_akshare_output
from app.services.market_data_sources.sources.base import HistorySource, SourceUnavailable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceFetchResult:
    """A successful fetch together with the source-selection lineage.

    ``fetch`` remains the compatibility API and returns only ``frame``. New
    callers should use this value object when the selected primary/backup
    source is part of the audit contract.
    """

    frame: pd.DataFrame
    source_name: str
    attempted_sources: tuple[str, ...]
    fallback_used: bool = False
    # Optional diagnostics retained for governance/audit consumers.  The
    # original four-field constructor remains fully compatible.
    failed_sources: tuple[str, ...] = ()
    unavailable_sources: tuple[str, ...] = ()
    considered_sources: tuple[str, ...] = ()

    @property
    def selected_source(self) -> str:
        """Explicit alias used by data-snapshot/report code."""
        return self.source_name

    @property
    def source_detail(self) -> str:
        """Gateway naming alias for the concrete provider."""
        return self.source_name

    @property
    def source_id(self) -> str:
        """Stable string source identifier (provider names are the IDs)."""
        return self.source_name

    def to_dict(self) -> dict[str, object]:
        return {
            "source_name": self.source_name,
            "selected_source": self.source_name,
            "attempted_sources": list(self.attempted_sources),
            "failed_sources": list(self.failed_sources),
            "unavailable_sources": list(self.unavailable_sources),
            "considered_sources": list(self.considered_sources or self.attempted_sources),
            "fallback_used": bool(self.fallback_used),
        }


class SourceChain:
    """多源降级链。

    按顺序尝试多个数据源,前源失败自动降级到下一源。

    Attributes:
        sources: 数据源列表(按优先级排序,前面的优先)
    """

    def __init__(
        self,
        sources: list[HistorySource],
        *,
        audit_db=None,
        audit_context: dict[str, object] | None = None,
        audit_callback=None,
    ) -> None:
        self.sources = list(sources)
        self.audit_db = audit_db
        self.audit_context = dict(audit_context or {})
        self.audit_callback = audit_callback

    def _emit_failover_audit(
        self,
        *,
        symbol,
        attempted_sources: list[str],
        failed_sources: list[str],
        unavailable_sources: list[str],
        considered_sources: list[str],
        selected_source: str,
    ) -> None:
        """Best-effort hook; business fetch success is never undone by audit I/O."""
        if not (self.audit_db is not None or self.audit_callback is not None):
            return
        event = {
            "interface_key": self.audit_context.get("interface_key", "market_data"),
            "dataset": self.audit_context.get("dataset", "daily_bars"),
            "symbol": getattr(symbol, "symbol", None),
            "attempted_sources": list(attempted_sources),
            "considered_sources": list(considered_sources),
            "failed_sources": list(failed_sources),
            "unavailable_sources": list(unavailable_sources),
            "selected_source": str(selected_source),
            "reason": "primary_source_unavailable",
            "source_snapshot_id": self.audit_context.get("source_snapshot_id"),
            "correlation_id": self.audit_context.get("correlation_id"),
        }
        try:
            if self.audit_callback is not None:
                self.audit_callback(event)
            if self.audit_db is not None:
                from app.services.data_governance_audit import audit_source_failover

                audit_source_failover(
                    self.audit_db,
                    interface_key=str(event["interface_key"]),
                    attempted_sources=list(considered_sources),
                    selected_source=str(selected_source),
                    reason=str(event["reason"]),
                    symbol=(str(event["symbol"]) if event["symbol"] is not None else None),
                    dataset=(str(event["dataset"]) if event["dataset"] is not None else None),
                    source_snapshot_id=(
                        str(event["source_snapshot_id"])
                        if event["source_snapshot_id"] is not None else None
                    ),
                    correlation_id=(
                        str(event["correlation_id"])
                        if event["correlation_id"] is not None else None
                    ),
                    operator_id=str(self.audit_context.get("operator_id", "system")),
                )
        except Exception as exc:  # pragma: no cover - audit must not break fetch
            logger.warning("source failover audit failed: %s", exc)

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
        return self.fetch_with_lineage(symbol, start_date, end_date, adjust).frame

    def fetch_with_lineage(
        self, symbol, start_date: date, end_date: date, adjust: str
    ) -> SourceFetchResult:
        """Fetch data and retain the concrete source that supplied it.

        A source that is unavailable or raises ``SourceUnavailable`` is part
        of the fallback trace. Unsupported sources are skipped by routing and
        do not count as an operational failover.
        """
        if not self.sources:
            raise RuntimeError(f"No sources in chain for {symbol.symbol}")

        tried: list[str] = []
        failed: list[str] = []
        unavailable: list[str] = []
        considered: list[str] = []
        last_error: Exception | None = None

        with quiet_akshare_output():
            for source in self.sources:
                source_name = getattr(source, "name", "unknown")
                considered.append(str(source_name))

                # 跳过不可用的源(如 TDX 路径未配置)
                try:
                    if not source.available():
                        logger.debug("source %s not available, skipping", source_name)
                        unavailable.append(str(source_name))
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
                    if failed or unavailable:
                        self._emit_failover_audit(
                            symbol=symbol,
                            attempted_sources=tried,
                            failed_sources=failed,
                            unavailable_sources=unavailable,
                            considered_sources=considered,
                            selected_source=str(source_name),
                        )
                    return SourceFetchResult(
                        frame=frame,
                        source_name=str(source_name),
                        attempted_sources=tuple(tried),
                        fallback_used=bool(failed or unavailable),
                        failed_sources=tuple(failed),
                        unavailable_sources=tuple(unavailable),
                        considered_sources=tuple(considered),
                    )
                except SourceUnavailable as exc:
                    logger.info("source %s unavailable for %s: %s, trying next", source_name, symbol.symbol, exc)
                    last_error = exc
                    failed.append(str(source_name))
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
