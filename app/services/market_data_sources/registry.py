"""数据源降级链注册表。

按 region + asset_type 组装降级链,业务代码通过 get_chain(symbol) 获取链条。

降级链配置(_CHAIN_CONFIG):
    cn-stock: em_stock -> sina_stock -> tx_stock -> tdx  (4 源,含本地兜底)
    cn-etf:   em_etf -> sina_etf                        (2 源,无本地兜底)
    us-stock: us_em                                     (1 源)

设计要点:
- akshare 源每次新建(无状态,线程安全)
- TDX 源用单例(detect_tdx_path 开销大,且路径不变)
- 未配置/未检测到的源不进链(避免每次 available() 检查)
"""
from __future__ import annotations

import logging
from threading import Lock
from typing import Callable

from app.services.market_data_sources.chain import SourceChain
from app.services.market_data_sources.sources.akshare_source import (
    EMETFSource,
    EMStockSource,
    SinaETFSource,
    SinaStockSource,
    TxStockSource,
    USStockSource,
)
from app.services.market_data_sources.sources.base import HistorySource
from app.services.market_data_sources.sources.tdx_source import TDXHistorySource
from app.services.regions import region_from_market

logger = logging.getLogger(__name__)

# 降级链配置:按 region-asset_type 组装,前面的优先级高
_CHAIN_CONFIG: dict[str, list[str]] = {
    "cn-stock": ["em_stock", "sina_stock", "tx_stock", "tdx"],
    "cn-etf": ["em_etf", "sina_etf"],
    "us-stock": ["us_em"],
}

# 源工厂注册表:name -> factory() -> HistorySource | None
# akshare 源工厂每次返回新实例(无状态);TDX 用单例
_SOURCE_FACTORIES: dict[str, Callable[[], HistorySource | None]] = {}


def _register(name: str, factory: Callable[[], HistorySource | None]) -> None:
    _SOURCE_FACTORIES[name] = factory


# akshare 源:无状态,每次新建
_register("em_stock", lambda: EMStockSource())
_register("em_etf", lambda: EMETFSource())
_register("sina_stock", lambda: SinaStockSource())
_register("sina_etf", lambda: SinaETFSource())
_register("tx_stock", lambda: TxStockSource())
_register("us_em", lambda: USStockSource())


# TDX 源:用单例(detect_tdx_path 开销大,路径不变)
_tdx_source: TDXHistorySource | None = None
_tdx_lock = Lock()
_tdx_initialized = False


def _get_tdx_source() -> TDXHistorySource | None:
    """获取 TDX 源单例。未检测到通达信路径返回 None。"""
    global _tdx_source, _tdx_initialized
    if _tdx_initialized:
        return _tdx_source
    with _tdx_lock:
        if _tdx_initialized:
            return _tdx_source
        try:
            source = TDXHistorySource()
            if source.available():
                _tdx_source = source
                logger.info("TDX source enabled: %s", _tdx_source.tdx_path)
            else:
                logger.info("TDX source disabled: path not detected")
        except Exception as exc:
            logger.warning("TDX source init failed: %s", exc, exc_info=True)
        _tdx_initialized = True
        return _tdx_source


_register("tdx", _get_tdx_source)


def _chain_key(symbol) -> str:
    """根据 Symbol 推导降级链 key(region-asset_type)。"""
    region = region_from_market(symbol.market)
    return f"{region}-{symbol.asset_type}"


def get_chain(symbol) -> SourceChain:
    """根据 Symbol 获取对应的降级链。

    Args:
        symbol: Symbol 模型实例

    Returns:
        SourceChain 实例

    Raises:
        RuntimeError: 该标的无对应降级链配置 / 所有源工厂都不可用
    """
    key = _chain_key(symbol)
    source_names = _CHAIN_CONFIG.get(key)
    if not source_names:
        raise RuntimeError(f"No source chain configured for {key} (symbol={symbol.symbol})")

    sources: list[HistorySource] = []
    for name in source_names:
        factory = _SOURCE_FACTORIES.get(name)
        if factory is None:
            logger.warning("source factory %s not registered, skipping", name)
            continue
        try:
            source = factory()
        except Exception as exc:
            logger.warning("source factory %s raised: %s, skipping", name, exc, exc_info=True)
            continue
        if source is None:
            # 工厂返回 None 表示该源不可用(如 TDX 未检测到路径),不进链
            continue
        sources.append(source)

    if not sources:
        raise RuntimeError(f"No available sources for {key} (symbol={symbol.symbol})")

    return SourceChain(sources)
