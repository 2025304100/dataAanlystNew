"""市场指数日线同步服务（P3+：Benchmark 对比曲线基础设施）。

职责：
1. 从 akshare 拉取指数日线，Upsert 入 index_prices 表
2. 供 portfolio_performance 查询 benchmark 对比曲线

调用方：
- scheduled_tasks 每日 16:05 触发（在组合净值快照 16:00 之后）
- 手动触发：一次性回补历史数据（如沪深300 过去 5 年）

设计要点：
- 指数日线历史可回溯（与组合 snapshot 不同）
- Upsert：同 symbol + trade_date 重复则更新
- 复用 call_akshare_with_retry：自动重试 + 防风控延时
- 每日只调 1 次（单标的），限流风险极低

数据源 fallback 链（与 A 股日 K 三源容灾模式一致）：
1. 东财 index_zh_a_hist（主源，列名中文）
2. 新浪 stock_zh_index_daily（备源 1，列名英文，返回全量历史需本地过滤）
3. 腾讯 stock_zh_index_daily_tx（备源 2，列名英文，返回全量历史需本地过滤）

任一 source 成功即停止 fallback；全部失败返回空 DataFrame。
新浪/腾讯接口返回全量历史（不分页），需在 Python 端按日期过滤。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import akshare as ak
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.index_price import IndexPrice
from app.services.akshare_utils import (
    call_akshare_with_retry,
    quiet_akshare_output,
)
from app.services.market_data import _proxy_bypass


logger = logging.getLogger(__name__)

# 数据源标识（与 IndexPrice.source 字段值一致）
_SOURCE_EASTMONEY = "akshare"  # 主源保持向后兼容
_SOURCE_SINA = "sina"
_SOURCE_TENCENT = "tencent"

# akshare registry 中的 api_key
_API_KEY_EASTMONEY = "index_zh_a_hist"
_API_KEY_SINA = "index_zh_a_daily"
_API_KEY_TENCENT = "stock_zh_index_daily_tx"


@dataclass(frozen=True)
class IndexSyncResult:
    """指数日线同步结果。"""
    symbol: str
    received: int
    written: int
    skipped: int
    date_range: tuple[date | None, date | None]


def _format_ak_date(d: date) -> str:
    """akshare index_zh_a_hist 要求 YYYYMMDD 格式。"""
    return d.strftime("%Y%m%d")


def _to_prefixed_symbol(symbol: str) -> str:
    """转换为新浪/腾讯接口要求的带交易所前缀格式。

    - 深证指数（399 开头）→ sz 前缀，如 399001 → sz399001
    - 其他（沪深300/上证等）→ sh 前缀，如 000300 → sh000300
    """
    if not symbol:
        raise ValueError("symbol is required")
    return f"sz{symbol}" if symbol.startswith("399") else f"sh{symbol}"


def _empty_frame() -> pd.DataFrame:
    """返回统一列结构的空 DataFrame。"""
    return pd.DataFrame(columns=["trade_date", "open", "high", "low", "close", "volume", "amount"])


def _filter_by_date(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """按日期范围过滤 DataFrame（新浪/腾讯返回全量历史需本地过滤）。

    要求 df 已有 trade_date 列（date 类型）。
    """
    if df.empty:
        return df
    mask = (df["trade_date"] >= start) & (df["trade_date"] <= end)
    return df[mask].sort_values("trade_date").reset_index(drop=True)


def _normalize_eastmoney_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """统一东财返回的中文列名为英文。

    ak.index_zh_a_hist 返回中文列：
    日期 / 开盘 / 收盘 / 最高 / 最低 / 成交量 / 成交额 / 振幅 / 涨跌幅 / 涨跌额 / 换手率
    """
    if frame is None or frame.empty:
        return _empty_frame()
    col_map = {
        "日期": "trade_date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
    }
    renamed = frame.rename(columns={k: v for k, v in col_map.items() if k in frame.columns})
    keep = [c for c in ["trade_date", "open", "high", "low", "close", "volume", "amount"] if c in renamed.columns]
    result = renamed[keep].copy()
    # 统一 trade_date 为 date 类型
    if "trade_date" in result.columns:
        result["trade_date"] = pd.to_datetime(result["trade_date"]).dt.date
    return result


def _normalize_sina_frame(frame: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """统一新浪返回的英文列名并按日期过滤。

    ak.stock_zh_index_daily 返回列：date / open / high / low / close / volume（无 amount）
    返回全量历史，需本地按日期过滤。
    """
    if frame is None or frame.empty:
        return _empty_frame()
    renamed = frame.rename(columns={"date": "trade_date"})
    keep = [c for c in ["trade_date", "open", "high", "low", "close", "volume", "amount"] if c in renamed.columns]
    result = renamed[keep].copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"]).dt.date
    return _filter_by_date(result, start, end)


def _normalize_tencent_frame(frame: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """统一腾讯返回的英文列名并按日期过滤。

    ak.stock_zh_index_daily_tx 返回列：date / open / close / high / low / amount（无 volume）
    返回全量历史，需本地按日期过滤。
    """
    if frame is None or frame.empty:
        return _empty_frame()
    renamed = frame.rename(columns={"date": "trade_date"})
    keep = [c for c in ["trade_date", "open", "high", "low", "close", "volume", "amount"] if c in renamed.columns]
    result = renamed[keep].copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"]).dt.date
    return _filter_by_date(result, start, end)


def _fetch_from_eastmoney(
    db: Session, symbol: str, start_date: date, end_date: date
) -> pd.DataFrame:
    """数据源 1：东财 index_zh_a_hist（主源）。

    优点：按日期范围精确拉取，无需本地过滤
    缺点：易被东财 WAF 风控
    """
    with _proxy_bypass(), quiet_akshare_output():
        frame = call_akshare_with_retry(
            ak.index_zh_a_hist,
            symbol=symbol,
            period="daily",
            start_date=_format_ak_date(start_date),
            end_date=_format_ak_date(end_date),
            api_key=_API_KEY_EASTMONEY,
            db=db,
        )
    if frame is None or frame.empty:
        return _empty_frame()
    return _normalize_eastmoney_frame(frame)


def _fetch_from_sina(
    symbol: str, start_date: date, end_date: date
) -> pd.DataFrame:
    """数据源 2：新浪 stock_zh_index_daily（备源 1）。

    优点：稳定性高，不易风控
    缺点：返回全量历史需本地过滤；无 amount 列
    """
    sina_symbol = _to_prefixed_symbol(symbol)
    with _proxy_bypass(), quiet_akshare_output():
        frame = call_akshare_with_retry(
            ak.stock_zh_index_daily,
            symbol=sina_symbol,
            api_key=_API_KEY_SINA,
        )
    if frame is None or frame.empty:
        return _empty_frame()
    return _normalize_sina_frame(frame, start_date, end_date)


def _fetch_from_tencent(
    symbol: str, start_date: date, end_date: date
) -> pd.DataFrame:
    """数据源 3：腾讯 stock_zh_index_daily_tx（备源 2）。

    优点：稳定性高，不易风控
    缺点：返回全量历史需本地过滤；无 volume 列，有 amount
    """
    tx_symbol = _to_prefixed_symbol(symbol)
    with _proxy_bypass(), quiet_akshare_output():
        frame = call_akshare_with_retry(
            ak.stock_zh_index_daily_tx,
            symbol=tx_symbol,
            api_key=_API_KEY_TENCENT,
        )
    if frame is None or frame.empty:
        return _empty_frame()
    return _normalize_tencent_frame(frame, start_date, end_date)


def _fetch_index_daily(
    db: Session, symbol: str, start_date: date, end_date: date
) -> tuple[pd.DataFrame, str]:
    """按 fallback 链拉取指数日线：东财 → 新浪 → 腾讯。

    任一 source 返回非空数据即停止 fallback；
    全部失败时返回空 DataFrame（让上层 sync_index_daily 处理）。

    Args:
        db: SQLAlchemy Session（仅主源需要记录调用结果到 DB）
        symbol: 指数代码，如 "000300"（沪深300）
        start_date: 起始日期（含）
        end_date: 结束日期（含）

    Returns:
        (frame, source) 元组：
        - frame: 统一列结构的 DataFrame（trade_date / open / high / low / close / volume / amount）
        - source: 实际命中的数据源标识（akshare/sina/tencent）；全失败时为 "akshare"（向后兼容默认值）
    """
    # 数据源 1：东财（主源）
    try:
        frame = _fetch_from_eastmoney(db, symbol, start_date, end_date)
        if not frame.empty:
            logger.info("Index %s: fetched from eastmoney (%d rows)", symbol, len(frame))
            return frame, _SOURCE_EASTMONEY
        logger.info("Index %s: eastmoney returned empty, trying fallback", symbol)
    except Exception as exc:
        logger.warning("Index %s: eastmoney failed: %s, trying fallback", symbol, exc)

    # 数据源 2：新浪
    try:
        frame = _fetch_from_sina(symbol, start_date, end_date)
        if not frame.empty:
            logger.info("Index %s: fetched from sina (%d rows)", symbol, len(frame))
            return frame, _SOURCE_SINA
        logger.info("Index %s: sina returned empty, trying fallback", symbol)
    except Exception as exc:
        logger.warning("Index %s: sina failed: %s, trying fallback", symbol, exc)

    # 数据源 3：腾讯
    try:
        frame = _fetch_from_tencent(symbol, start_date, end_date)
        if not frame.empty:
            logger.info("Index %s: fetched from tencent (%d rows)", symbol, len(frame))
            return frame, _SOURCE_TENCENT
        logger.info("Index %s: tencent returned empty", symbol)
    except Exception as exc:
        logger.warning("Index %s: tencent failed: %s", symbol, exc)

    logger.warning("Index %s: all sources failed for %s ~ %s", symbol, start_date, end_date)
    return _empty_frame(), _SOURCE_EASTMONEY


def sync_index_daily(
    db: Session,
    symbol: str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> IndexSyncResult:
    """同步指数日线到 index_prices 表（Upsert 语义）。

    Args:
        symbol: 指数代码，如 "000300"
        start_date: 起始日期（含）。None 表示从 5 年前（覆盖回补场景）
        end_date: 结束日期（含）。None 表示今天

    Returns:
        IndexSyncResult：含 received/written/skipped 统计
    """
    if not symbol:
        raise ValueError("symbol is required")

    end = end_date or date.today()
    # 默认回补 5 年（指数日线历史可回溯，首次同步会拉一次大量数据）
    start = start_date or date(end.year - 5, end.month, end.day)

    frame, source = _fetch_index_daily(db, symbol, start, end)
    if frame.empty:
        logger.warning("Index %s: no data returned for %s ~ %s", symbol, start, end)
        return IndexSyncResult(
            symbol=symbol, received=0, written=0, skipped=0, date_range=(None, None)
        )

    written = 0
    skipped = 0
    first_date: date | None = None
    last_date: date | None = None

    for row in frame.itertuples(index=False):
        try:
            raw_date = getattr(row, "trade_date", None)
            if raw_date is None or raw_date == "" or (isinstance(raw_date, float) and pd.isna(raw_date)):
                skipped += 1
                continue
            # normalize 后 trade_date 已统一为 date 类型，但保留对 str/Timestamp 的兼容
            if isinstance(raw_date, str):
                trade_date = date.fromisoformat(raw_date[:10])
            elif isinstance(raw_date, date):
                trade_date = raw_date
            else:
                # pandas Timestamp or datetime
                trade_date = pd.Timestamp(raw_date).date()

            if first_date is None or trade_date < first_date:
                first_date = trade_date
            if last_date is None or trade_date > last_date:
                last_date = trade_date

            open_ = float(getattr(row, "open", 0) or 0)
            high = float(getattr(row, "high", 0) or 0)
            low = float(getattr(row, "low", 0) or 0)
            close = float(getattr(row, "close", 0) or 0)
            volume = getattr(row, "volume", None)
            amount = getattr(row, "amount", None)

            existing = db.execute(
                select(IndexPrice).where(
                    IndexPrice.symbol == symbol,
                    IndexPrice.trade_date == trade_date,
                )
            ).scalars().first()

            if existing is not None:
                existing.open = open_
                existing.high = high
                existing.low = low
                existing.close = close
                if volume is not None and not (isinstance(volume, float) and pd.isna(volume)):
                    existing.volume = float(volume)
                if amount is not None and not (isinstance(amount, float) and pd.isna(amount)):
                    existing.amount = float(amount)
                existing.source = source
            else:
                db.add(IndexPrice(
                    symbol=symbol,
                    trade_date=trade_date,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=float(volume) if volume is not None and not (isinstance(volume, float) and pd.isna(volume)) else None,
                    amount=float(amount) if amount is not None and not (isinstance(amount, float) and pd.isna(amount)) else None,
                    source=source,
                ))
            written += 1
        except Exception as exc:
            logger.warning(
                "Index %s: failed to upsert row %s: %s", symbol, row, exc
            )
            skipped += 1
            continue

    db.commit()
    logger.info(
        "Index %s sync: received=%d written=%d skipped=%d range=%s~%s",
        symbol, len(frame), written, skipped, first_date, last_date,
    )
    return IndexSyncResult(
        symbol=symbol,
        received=len(frame),
        written=written,
        skipped=skipped,
        date_range=(first_date, last_date),
    )


def list_index_prices(
    db: Session,
    symbol: str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 2000,
) -> list[IndexPrice]:
    """按时间升序返回指数日线（供 portfolio_performance 查询 benchmark 使用）。"""
    stmt = (
        select(IndexPrice)
        .where(IndexPrice.symbol == symbol)
        .order_by(IndexPrice.trade_date.asc())
    )
    if start_date is not None:
        stmt = stmt.where(IndexPrice.trade_date >= start_date)
    if end_date is not None:
        stmt = stmt.where(IndexPrice.trade_date <= end_date)
    if limit and limit > 0:
        stmt = stmt.limit(int(limit))
    return list(db.execute(stmt).scalars().all())


__all__ = [
    "IndexSyncResult",
    "sync_index_daily",
    "list_index_prices",
    # 暴露给白盒测试的辅助函数
    "_to_prefixed_symbol",
    "_normalize_eastmoney_frame",
    "_normalize_sina_frame",
    "_normalize_tencent_frame",
    "_filter_by_date",
    "_fetch_index_daily",
    "_fetch_from_eastmoney",
    "_fetch_from_sina",
    "_fetch_from_tencent",
]
