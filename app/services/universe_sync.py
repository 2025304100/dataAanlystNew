"""基础数据层同步服务：全市场标的元数据 + K线 → universe_symbols / universe_daily_bars。

与业务表 symbols/daily_bars 物理隔离，挖掘任务只读此层。
复用 market_data._fetch_history 的多源 fallback 链（通过 SimpleNamespace 适配）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
import time
from types import SimpleNamespace
from typing import Any, Callable
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services.akshare_utils import call_akshare_with_retry, quiet_akshare_output

logger = logging.getLogger(__name__)

# 单标的 K线同步超时（秒），与挖掘任务一致
SYNC_ONE_SYMBOL_TIMEOUT_SECONDS = 90
# 连续失败熔断阈值：sync_failed >= 此值则跳过该标的
SYNC_FAILED_THRESHOLD = 5
# 默认同步历史天数（首次初始化用 1 年，满足评分需要的 80 根 K线；增量同步只拉 1 天）
DEFAULT_HISTORY_DAYS = 365


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _latest_completed_trading_date(
    as_of: date | datetime | None = None,
    region: str = "cn",
) -> date:
    """Return the latest completed weekday in the selected market timezone."""
    app_timezone = ZoneInfo("Asia/Shanghai")
    if as_of is None:
        app_now = datetime.now(app_timezone)
    elif isinstance(as_of, datetime):
        app_now = as_of.replace(tzinfo=app_timezone) if as_of.tzinfo is None else as_of
    else:
        app_now = datetime.combine(as_of, datetime.max.time(), tzinfo=app_timezone)

    market_timezone = app_timezone if region == "cn" else ZoneInfo("America/New_York")
    current = app_now.astimezone(market_timezone)

    candidate = current.date()
    if candidate.weekday() < 5 and current.hour < 16:
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


# ── 市场判断（与 discovery_tasks 保持一致）──────────────────────────

def _market_for_cn_stock(code: str) -> str:
    # 920xxx 为北交所新代码段（2024年起新增），优先判断，避免被 "9" → "sh" 误判
    if code.startswith("920"):
        return "bj"
    if code.startswith(("4", "8")):
        return "bj"
    if code.startswith(("5", "6", "9")):
        return "sh"
    return "sz"


def _market_for_cn_fund(code: str, prefixed_code: str | None = None) -> str:
    prefixed = (prefixed_code or "").lower()
    if prefixed.startswith("sh"):
        return "sh"
    if prefixed.startswith("sz"):
        return "sz"
    return "sh" if code.startswith("5") else "sz"


def _board_for_cn_stock(code: str) -> str:
    """推断 A股板块。"""
    if code.startswith("688"):
        return "star"
    if code.startswith(("300", "301")):
        return "gem"
    # 920xxx 为北交所新代码段，8xx/4xx 为北交所/老三板
    if code.startswith(("920", "8", "4")):
        return "bj"
    return "main"


# ── 标的列表拉取 ──────────────────────────────────────────────────

def _fetch_cn_stock_list(db: Session) -> pd.DataFrame:
    """拉取 A股股票列表（东财 → sina fallback）。"""
    sources = [
        ("stock_info_a_code_name", ak.stock_info_a_code_name),
        ("stock_zh_a_spot_em", ak.stock_zh_a_spot_em),
    ]
    errors: list[str] = []
    for api_key, func_ in sources:
        try:
            with quiet_akshare_output():
                return call_akshare_with_retry(func_, api_key=api_key, max_attempts=2, db=db)
        except Exception as exc:
            errors.append(f"{api_key}: {type(exc).__name__}: {exc}")
            logger.warning("universe cn-stock list source %s failed: %s", api_key, exc)
    raise RuntimeError("A股股票列表拉取失败；" + " | ".join(errors))


def _fetch_cn_etf_list(db: Session) -> pd.DataFrame:
    """拉取 A股ETF列表（东财 → sina fallback）。"""
    try:
        with quiet_akshare_output():
            return call_akshare_with_retry(
                ak.fund_etf_spot_em,
                api_key="fund_etf_spot_em",
                max_attempts=2,
                db=db,
            )
    except Exception as exc:
        logger.warning("universe cn-etf primary source failed, trying sina: %s", exc)
        with quiet_akshare_output():
            # 必须显式传 symbol="ETF基金"，否则默认返回 LOF
            return call_akshare_with_retry(
                ak.fund_etf_category_sina,
                symbol="ETF基金",
                api_key="fund_etf_category_sina",
                max_attempts=2,
                db=db,
            )


def _fetch_us_stock_list(db: Session) -> pd.DataFrame:
    """拉取美股股票列表（东财 stock_us_spot_em → stock_us_spot fallback）。

    返回列含"简称"（股票代码如 AAPL）和"名称"（中文名）。
    akshare 无参数版本返回纳斯达克+纽交所+AMEX 全部美股。
    """
    sources = [
        ("stock_us_spot_em", ak.stock_us_spot_em),
        ("stock_us_spot", ak.stock_us_spot),
    ]
    errors: list[str] = []
    for api_key, func_ in sources:
        try:
            with quiet_akshare_output():
                return call_akshare_with_retry(func_, api_key=api_key, max_attempts=2, db=db)
        except Exception as exc:
            errors.append(f"{api_key}: {type(exc).__name__}: {exc}")
            logger.warning("universe us-stock list source %s failed: %s", api_key, exc)
    raise RuntimeError("美股股票列表拉取失败；" + " | ".join(errors))


# 美股主流 ETF 列表（akshare 无美股 ETF 列表接口，用高流动性品种作为种子）
# 覆盖大盘/行业/商品/债券/海外等主流品种，K线通过 stock_us_daily 获取
_US_ETF_SEED_LIST: list[tuple[str, str]] = [
    # 大盘宽基
    ("SPY", "标普500 ETF"), ("IVV", "标普500 ETF iShares"), ("VOO", "标普500 ETF Vanguard"),
    ("QQQ", "纳斯达克100 ETF"), ("QQQE", "纳斯达克100等权ETF"), ("DIA", "道琼斯工业ETF"),
    ("VTI", "全美市场ETF"), ("VTV", "价值股ETF"), ("VUG", "成长股ETF"),
    ("VOE", "中盘价值ETF"), ("VO", "中盘ETF"), ("VB", "小盘ETF"),
    ("IJH", "中盘400 ETF"), ("IJR", "小盘600 ETF"), ("IWM", "罗素2000 ETF"),
    ("MDY", "中盘400 ETF SPDR"), ("SLY", "小盘600 ETF SPDR"),
    # 科技
    ("XLK", "科技板块ETF"), ("VGT", "信息技术ETF"), ("FDN", "互联网ETF"),
    ("SMH", "半导体ETF"), ("SOXX", "半导体ETF iShares"), ("IGV", "软件ETF"),
    ("KWEB", "中概互联网ETF"), ("CQQQ", "中国科技ETF"),
    # 金融/医疗/消费
    ("XLF", "金融板块ETF"), ("KBE", "银行ETF"), ("KRE", "区域银行ETF"),
    ("XLV", "医疗板块ETF"), ("IHI", "医疗器械ETF"), ("IBB", "生物科技ETF"),
    ("XLY", "可选消费ETF"), ("XLP", "必需消费ETF"), ("VDC", "必需消费ETF Vanguard"),
    # 能源/工业/材料
    ("XLE", "能源板块ETF"), ("XOP", "油气开采ETF"), ("OIH", "油田服务ETF"),
    ("XLI", "工业板块ETF"), ("XLB", "材料板块ETF"), ("XME", "金属采矿ETF"),
    # 不动产/公用事业
    ("XLRE", "不动产ETF"), ("VNQ", "不动产ETF Vanguard"), ("IYR", "不动产ETF iShares"),
    ("XLU", "公用事业ETF"), ("IDU", "公用事业ETF iShares"),
    # 商品
    ("GLD", "黄金ETF"), ("IAU", "黄金ETF iShares"), ("GDX", "金矿ETF"),
    ("SLV", "白银ETF"), ("USO", "原油ETF"), ("UNG", "天然气ETF"),
    # 债券
    ("AGG", "综合债券ETF"), ("BND", "全债市ETF Vanguard"), ("TLT", "20+年国债ETF"),
    ("IEF", "7-10年国债ETF"), ("SHY", "1-3年国债ETF"), ("LQD", "投资级公司债ETF"),
    ("HYG", "高收益债ETF"), ("EMB", "新兴市场债ETF"),
    # 海外/新兴市场
    ("VEA", "发达市场ETF"), ("IEFA", "发达市场ETF iShares"), ("EEM", "新兴市场ETF"),
    ("VWO", "新兴市场ETF Vanguard"), ("EWC", "加拿大ETF"), ("EWJ", "日本ETF"),
    ("EWG", "德国ETF"), ("EWQ", "法国ETF"), ("EWU", "英国ETF"),
    # 通胀保护/另类
    ("TIP", "通胀保护债ETF"), ("SCHP", "通胀保护债ETF Schwab"),
    ("ARKK", "创新ETF ARK"), ("ARKG", "基因组革命ETF"), ("ARKQ", "自动技术ETF"),
    ("ARKW", "下一代互联网ETF"), ("ARKF", "金融科技创新ETF"),
]


def _fetch_us_etf_list(db: Session) -> pd.DataFrame:
    """美股 ETF 列表（akshare 无接口，返回内置主流品种种子列表）。

    后续可通过 stock_us_daily 正常同步 K线。
    """
    return pd.DataFrame(_US_ETF_SEED_LIST, columns=["简称", "名称"])



def _first_row_value(row: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


# P0.5 优化：停牌/退市标的名称标记
# A股：停牌、退市、暂停上市、终止上市
# 美股：Suspended、Delisted、Withdrawn
_SUSPENDED_KEYWORDS_CN = ("停牌", "退市", "暂停上市", "终止上市")
_SUSPENDED_KEYWORDS_EN = ("Suspended", "Delisted", "Withdrawn")


def _is_suspended_or_delisted(name: str) -> bool:
    """判断标的名称是否为停牌/退市状态。

    Args:
        name: 标的名称（如 "ST长生退"、"某公司-Suspended"）
    Returns:
        True 表示应跳过该标的
    """
    if not name:
        return False
    # A股标记检查（中文）
    for kw in _SUSPENDED_KEYWORDS_CN:
        if kw in name:
            return True
    # 美股标记检查（英文，大小写不敏感）
    name_lower = name.lower()
    for kw in _SUSPENDED_KEYWORDS_EN:
        if kw.lower() in name_lower:
            return True
    return False


def _upsert_universe_symbol(
    db: Session,
    *,
    code: str,
    name: str,
    asset_type: str,
    market: str,
    region: str = "cn",
    board: str | None = None,
    industry: str | None = None,
) -> bool:
    """upsert universe_symbols，返回是否新建。"""
    symbol_code = code.strip().upper()
    if not symbol_code:
        return False
    existing = db.execute(
        select(UniverseSymbol).where(UniverseSymbol.symbol == symbol_code)
    ).scalars().first()
    created = existing is None
    if existing is None:
        db.add(UniverseSymbol(
            symbol=symbol_code,
            name=name.strip() or symbol_code,
            asset_type=asset_type,
            market=market,
            region=region,
            board=board,
            industry=industry,
        ))
    else:
        # 已存在则更新名称/市场/板块（不重置同步状态）
        # market/board 始终更新，修正旧数据（如 920xxx 旧记录 market='sh'）
        if name.strip():
            existing.name = name.strip()
        if market:
            existing.market = market
        if board:
            existing.board = board
        if industry:
            existing.industry = existing.industry or industry
    return created


def refresh_universe_symbols(scope: str, db: Session) -> dict:
    """拉取全市场标的写入 universe_symbols。

    Args:
        scope: "cn-stock" | "cn-etf" | "us-stock" | "us-etf"
        db: 数据库会话
    Returns:
        {"seen": int, "created": int, "scope": str}
    """
    logger.info("refresh universe symbols start: scope=%s", scope)
    # 网络请求前释放 DB 连接
    db.commit()

    if scope == "cn-stock":
        frame = _fetch_cn_stock_list(db)
        code_keys = ["code", "代码", "symbol"]
        name_keys = ["name", "名称"]
        asset_type = "stock"
        region = "cn"
    elif scope == "cn-etf":
        frame = _fetch_cn_etf_list(db)
        code_keys = ["代码", "基金代码", "symbol", "code"]
        name_keys = ["名称", "基金简称", "name"]
        asset_type = "etf"
        region = "cn"
    elif scope == "us-stock":
        frame = _fetch_us_stock_list(db)
        # stock_us_spot_em 返回"简称"（如 AAPL）和"名称"（中文名）
        code_keys = ["简称", "代码", "symbol", "code"]
        name_keys = ["名称", "name"]
        asset_type = "stock"
        region = "us"
    elif scope == "us-etf":
        frame = _fetch_us_etf_list(db)
        code_keys = ["简称", "代码", "symbol"]
        name_keys = ["名称", "name"]
        asset_type = "etf"
        region = "us"
    else:
        raise ValueError(f"Unsupported scope for universe refresh: {scope}")

    seen = 0
    created = 0
    skipped_suspended = 0
    for row in frame.to_dict("records"):
        raw_code = _first_row_value(row, code_keys)
        raw_name = _first_row_value(row, name_keys)
        if not raw_code or not raw_name:
            continue
        # P0.5 优化：跳过停牌/退市标的，减少无效同步
        # A股名称常见标记：停牌、退市、暂停上市、ST退、*ST退
        # 美股名称常见标记：Suspended、Delisted、Withdrawn
        name_str = str(raw_name).strip()
        if _is_suspended_or_delisted(name_str):
            skipped_suspended += 1
            continue
        if region == "cn" and asset_type == "stock":
            code = str(raw_code).strip().zfill(6)
            if not code.isdigit():
                continue
            market = _market_for_cn_stock(code)
            board = _board_for_cn_stock(code)
        elif region == "cn" and asset_type == "etf":
            prefixed = str(raw_code).strip().lower()
            code = prefixed.replace("sh", "").replace("sz", "").zfill(6)
            if not code.isdigit():
                continue
            market = _market_for_cn_fund(code, prefixed)
            board = None
        else:
            # 美股：code 为字母代码（如 AAPL），不 zfill，market 统一用 "us"
            code = str(raw_code).strip().upper()
            if not code or not code[0].isalpha():
                continue
            market = "us"
            board = None
        seen += 1
        if _upsert_universe_symbol(
            db, code=code, name=str(raw_name), asset_type=asset_type,
            market=market, region=region, board=board,
        ):
            created += 1
    db.commit()
    logger.info(
        "refresh universe symbols done: scope=%s seen=%d created=%d skipped_suspended=%d",
        scope, seen, created, skipped_suspended,
    )
    return {"seen": seen, "created": created, "scope": scope, "skipped_suspended": skipped_suspended}


# ── K线同步 ────────────────────────────────────────────────────────

def _fetch_universe_history(
    universe_symbol: UniverseSymbol,
    start_date: date,
    end_date: date,
    adjust: str = "qfq",
) -> pd.DataFrame:
    """拉取单个 universe_symbol 的历史 K线，复用 market_data._fetch_history 的多源 fallback。

    通过 SimpleNamespace 适配 _fetch_history 所需的 Symbol 接口（.symbol/.asset_type/.market）。
    """
    from app.services.market_data import _fetch_history
    fake_symbol = SimpleNamespace(
        symbol=universe_symbol.symbol,
        asset_type=universe_symbol.asset_type,
        market=universe_symbol.market,
    )
    return _fetch_history(fake_symbol, start_date, end_date, adjust)


def _normalize_trade_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return pd.to_datetime(value).date()


def _dedupe_history_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """按 trade_date 去重，保留同日最后一条记录。"""
    deduped: dict[date, dict[str, Any]] = {}
    for raw_row in frame.to_dict(orient="records"):
        trade_date = _normalize_trade_date(raw_row["trade_date"])
        row = dict(raw_row)
        row["trade_date"] = trade_date
        deduped[trade_date] = row
    return list(deduped.values())


def _to_float(value: Any) -> float | None:
    """安全转 float，None/NaN → None。"""
    if value is None or pd.isna(value):
        return None
    return float(value)


def _upsert_universe_bars(db: Session, universe_symbol_id: int, frame: pd.DataFrame) -> tuple[int, int]:
    """批量 upsert K线到 universe_daily_bars，返回 (inserted, updated)。

    MySQL 使用 INSERT ... ON DUPLICATE KEY UPDATE 批量语句（单条 SQL 完成全部插入/更新）；
    其他方言（如 SQLite 测试环境）回退到逐行 ORM upsert。
    """
    rows = _dedupe_history_rows(frame)
    if not rows:
        return 0, 0

    trade_dates = [r["trade_date"] for r in rows]

    # 前置查询已有记录的日期（用于精确计数 inserted/updated）
    existing_dates = set(
        db.execute(
            select(UniverseDailyBar.trade_date).where(
                UniverseDailyBar.universe_symbol_id == universe_symbol_id,
                UniverseDailyBar.trade_date.in_(trade_dates),
            )
        ).scalars().all()
    )
    inserted = sum(1 for r in rows if r["trade_date"] not in existing_dates)
    updated = len(rows) - inserted

    # 构造批量数据
    values = [
        {
            "universe_symbol_id": universe_symbol_id,
            "trade_date": row["trade_date"],
            "open": _to_float(row.get("open")),
            "high": _to_float(row.get("high")),
            "low": _to_float(row.get("low")),
            "close": _to_float(row.get("close")),
            "volume": _to_float(row.get("volume")),
            "amount": _to_float(row.get("amount")),
            "turnover_rate": _to_float(row.get("turnover_rate")),
            "source": "akshare",
        }
        for row in rows
    ]

    dialect_name = db.get_bind().dialect.name if db.get_bind() else ""
    if dialect_name == "mysql":
        # MySQL：单条 INSERT ... ON DUPLICATE KEY UPDATE 完成批量 upsert
        stmt = mysql_insert(UniverseDailyBar).values(values)
        stmt = stmt.on_duplicate_key_update(
            open=stmt.inserted.open,
            high=stmt.inserted.high,
            low=stmt.inserted.low,
            close=stmt.inserted.close,
            volume=stmt.inserted.volume,
            amount=stmt.inserted.amount,
            turnover_rate=stmt.inserted.turnover_rate,
            source=stmt.inserted.source,
        )
        db.execute(stmt)
    else:
        # SQLite/其他：逐行 ORM upsert（测试环境兼容）
        existing_bars = db.execute(
            select(UniverseDailyBar).where(
                UniverseDailyBar.universe_symbol_id == universe_symbol_id,
                UniverseDailyBar.trade_date.in_(trade_dates),
            )
        ).scalars().all()
        existing_map = {bar.trade_date: bar for bar in existing_bars}
        for row in values:
            trade_date = row["trade_date"]
            existing = existing_map.get(trade_date)
            if existing is None:
                existing = UniverseDailyBar(
                    universe_symbol_id=universe_symbol_id, trade_date=trade_date
                )
                db.add(existing)
                existing_map[trade_date] = existing
            existing.open = row["open"]
            existing.high = row["high"]
            existing.low = row["low"]
            existing.close = row["close"]
            existing.volume = row["volume"]
            existing.amount = row["amount"]
            existing.turnover_rate = row["turnover_rate"]
            existing.source = row["source"]

    return inserted, updated


def sync_one_universe_symbol(
    db: Session,
    universe_symbol: UniverseSymbol,
    history_days: int = DEFAULT_HISTORY_DAYS,
) -> dict:
    """同步单个 universe_symbol 的 K线到 universe_daily_bars。

    断点续传：已有 K线时从最后日期 +1 天增量拉取。
    熔断：sync_failed >= 5 直接跳过。
    """
    if universe_symbol.sync_failed >= SYNC_FAILED_THRESHOLD:
        return {"symbol": universe_symbol.symbol, "status": "skipped", "reason": "sync_failed threshold reached"}

    # 断点续传：查最后 K线日期
    latest_bar = db.execute(
        select(UniverseDailyBar)
        .where(UniverseDailyBar.universe_symbol_id == universe_symbol.id)
        .order_by(UniverseDailyBar.trade_date.desc())
    ).scalars().first()

    end_date = date.today()
    if latest_bar is not None:
        start_date = latest_bar.trade_date + timedelta(days=1)
        if start_date > end_date:
            # 已是最新
            return {"symbol": universe_symbol.symbol, "status": "uptodate", "inserted": 0, "updated": 0}
    else:
        start_date = end_date - timedelta(days=history_days)

    # 网络请求前释放 DB 连接，避免 akshare 长请求期间连接被 MySQL 关闭
    db.commit()

    try:
        frame = _fetch_universe_history(universe_symbol, start_date, end_date)
    except Exception as exc:
        logger.warning("fetch universe history failed: %s: %s", universe_symbol.symbol, exc)
        universe_symbol.sync_failed += 1
        db.commit()
        return {"symbol": universe_symbol.symbol, "status": "failed", "error": str(exc)}

    if frame.empty:
        # 空数据可能是退市/停牌，标记已同步避免反复重试
        universe_symbol.is_synced = 1
        universe_symbol.last_synced_at = _now()
        db.commit()
        return {"symbol": universe_symbol.symbol, "status": "empty", "inserted": 0, "updated": 0}

    inserted, updated = _upsert_universe_bars(db, universe_symbol.id, frame)
    # 更新同步状态
    universe_symbol.is_synced = 1
    universe_symbol.sync_failed = 0
    universe_symbol.last_synced_at = _now()
    universe_symbol.last_bar_date = _normalize_trade_date(frame["trade_date"].iloc[-1])
    universe_symbol.bar_count = db.execute(
        select(func.count(UniverseDailyBar.id)).where(
            UniverseDailyBar.universe_symbol_id == universe_symbol.id
        )
    ).scalar_one()
    db.commit()
    return {
        "symbol": universe_symbol.symbol,
        "status": "ok",
        "inserted": inserted,
        "updated": updated,
    }


def _sync_one_concurrent(universe_symbol_id: int, history_days: int) -> dict:
    """并发 worker：独立 Session 同步单个标的。"""
    from sqlalchemy.exc import OperationalError
    sub_db = SessionLocal()
    try:
        us = sub_db.get(UniverseSymbol, universe_symbol_id)
        if us is None:
            return {"symbol": str(universe_symbol_id), "status": "failed", "error": "not found"}
        return sync_one_universe_symbol(sub_db, us, history_days)
    except OperationalError as exc:
        # 连接断开重试一次
        logger.warning("concurrent universe sync %s OperationalError, retrying: %s", universe_symbol_id, exc)
        try:
            sub_db.rollback()
        except Exception:
            pass
        sub_db2 = SessionLocal()
        try:
            us = sub_db2.get(UniverseSymbol, universe_symbol_id)
            if us is None:
                return {"symbol": str(universe_symbol_id), "status": "failed", "error": "not found"}
            return sync_one_universe_symbol(sub_db2, us, history_days)
        finally:
            sub_db2.close()
    finally:
        sub_db.close()


def _incremental_timed_result(
    result: dict,
    started_at: float,
    *,
    fetch_seconds: float = 0.0,
    database_seconds: float = 0.0,
) -> dict:
    result['fetch_seconds'] = round(fetch_seconds, 4)
    result['database_seconds'] = round(database_seconds, 4)
    result['elapsed_seconds'] = round(time.perf_counter() - started_at, 4)
    return result


def sync_one_universe_symbol_incremental(
    db: Session,
    universe_symbol: UniverseSymbol,
    target_date: date,
) -> dict:
    """Sync only the missing tail represented by UniverseSymbol metadata."""
    started_at = time.perf_counter()
    database_seconds = 0.0
    if universe_symbol.sync_failed >= SYNC_FAILED_THRESHOLD:
        return _incremental_timed_result({
            'symbol': universe_symbol.symbol,
            'status': 'skipped',
            'reason': 'sync_failed threshold reached',
        }, started_at)

    if universe_symbol.last_bar_date is not None:
        start_date = universe_symbol.last_bar_date + timedelta(days=1)
        if start_date > target_date:
            return _incremental_timed_result({
                'symbol': universe_symbol.symbol,
                'status': 'uptodate',
                'inserted': 0,
                'updated': 0,
            }, started_at)
    else:
        # is_synced without a last bar usually means suspended/delisted.
        start_date = target_date

    database_started_at = time.perf_counter()
    db.commit()
    database_seconds += time.perf_counter() - database_started_at
    fetch_started_at = time.perf_counter()
    try:
        frame = _fetch_universe_history(universe_symbol, start_date, target_date)
    except Exception as exc:
        fetch_seconds = time.perf_counter() - fetch_started_at
        logger.warning(
            "fetch incremental universe history failed: %s: %s",
            universe_symbol.symbol,
            exc,
        )
        universe_symbol.sync_failed += 1
        universe_symbol.last_synced_at = _now()
        database_started_at = time.perf_counter()
        db.commit()
        database_seconds += time.perf_counter() - database_started_at
        return _incremental_timed_result(
            {'symbol': universe_symbol.symbol, 'status': 'failed', 'error': str(exc)},
            started_at,
            fetch_seconds=fetch_seconds,
            database_seconds=database_seconds,
        )

    fetch_seconds = time.perf_counter() - fetch_started_at

    universe_symbol.last_synced_at = _now()
    universe_symbol.is_synced = 1
    if frame.empty:
        database_started_at = time.perf_counter()
        db.commit()
        database_seconds += time.perf_counter() - database_started_at
        return _incremental_timed_result(
            {'symbol': universe_symbol.symbol, 'status': 'empty', 'inserted': 0, 'updated': 0},
            started_at,
            fetch_seconds=fetch_seconds,
            database_seconds=database_seconds,
        )

    database_started_at = time.perf_counter()
    inserted, updated = _upsert_universe_bars(db, universe_symbol.id, frame)
    universe_symbol.sync_failed = 0
    universe_symbol.last_bar_date = max(
        _normalize_trade_date(value) for value in frame["trade_date"]
    )
    universe_symbol.bar_count = int(universe_symbol.bar_count or 0) + inserted
    db.commit()
    database_seconds += time.perf_counter() - database_started_at
    return _incremental_timed_result(
        {
            'symbol': universe_symbol.symbol,
            'status': 'ok',
            'inserted': inserted,
            'updated': updated,
        },
        started_at,
        fetch_seconds=fetch_seconds,
        database_seconds=database_seconds,
    )


def _sync_one_incremental_concurrent(universe_symbol_id: int, target_date: date) -> dict:
    """Concurrent incremental worker with its own short-lived DB session."""
    from sqlalchemy.exc import OperationalError

    def _run(db: Session) -> dict:
        universe_symbol = db.get(UniverseSymbol, universe_symbol_id)
        if universe_symbol is None:
            return {"symbol": str(universe_symbol_id), "status": "failed", "error": "not found"}
        return sync_one_universe_symbol_incremental(db, universe_symbol, target_date)

    sub_db = SessionLocal()
    try:
        return _run(sub_db)
    except OperationalError as exc:
        logger.warning(
            "incremental universe sync %s OperationalError, retrying: %s",
            universe_symbol_id,
            exc,
        )
        try:
            sub_db.rollback()
        except Exception:
            pass
        retry_db = SessionLocal()
        try:
            return _run(retry_db)
        finally:
            retry_db.close()
    finally:
        sub_db.close()


def sync_universe_bars_batch(
    scope: str,
    max_workers: int = 5,
    history_days: int = DEFAULT_HISTORY_DAYS,
    progress_callback: Callable[[int, int, int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    sync_limit: int = 0,
) -> dict:
    """批量同步 K线到 universe_daily_bars（断点续传 + 并发 + 熔断 + 分批限速 + 失败重试）。

    Args:
        scope: "cn-stock" | "cn-etf" | "us-stock" | "us-etf"
        max_workers: 并发线程数（1-8，默认 5）
        history_days: 历史天数（默认 365）
        progress_callback: 进度回调 (processed, total, ok_count, failed_count)
        is_cancelled: 取消检查函数，返回 True 时停止
        sync_limit: 单次同步标的上限（0=不限，>0 只取前 N 个未同步标的），用于分段同步

    优化点：
    - 分批限速：每 BATCH_SIZE 个标的一批，批次间隔 BATCH_INTERVAL_SECONDS 秒，避免 akshare 风控
    - 失败重试：第一轮处理完后，失败标的重试 1 轮（可能是临时网络问题）
    - 进度按总 total 计，重试阶段进度回调只增不减（避免回退）

    Returns:
        {"total": int, "processed": int, "ok": int, "failed": int, "skipped": int}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

    # 分批限速配置：每 50 个标的一批，批次间隔 1 秒
    BATCH_SIZE = 50
    BATCH_INTERVAL_SECONDS = 1.0

    db = SessionLocal()
    try:
        # 确定要同步的标的（is_synced=0 且未熔断）
        config = _scope_config(scope)
        query = select(UniverseSymbol).where(
            UniverseSymbol.region == config["region"],
            UniverseSymbol.asset_type == config["asset_type"],
            UniverseSymbol.is_synced == 0,
            UniverseSymbol.sync_failed < SYNC_FAILED_THRESHOLD,
        ).order_by(UniverseSymbol.id.asc())
        # P3 分段同步：sync_limit > 0 时只取前 N 个未同步标的
        if sync_limit > 0:
            query = query.limit(sync_limit)
        pending = db.execute(query).scalars().all()
        pending_ids = [us.id for us in pending]
        total = len(pending_ids)
    finally:
        db.close()

    if total == 0:
        logger.info("universe bars sync: no pending symbols for scope=%s", scope)
        return {"total": 0, "processed": 0, "ok": 0, "failed": 0, "skipped": 0}

    logger.info("universe bars sync start: scope=%s total=%d workers=%d batch=%d", scope, total, max_workers, BATCH_SIZE)
    processed = 0
    ok_count = 0
    failed_count = 0
    skipped_count = 0
    failed_ids: list[int] = []  # 第一轮失败的标的，用于重试
    consecutive_failures = 0  # P2.2：连续失败计数（熔断降级）
    circuit_broken = False  # P2.2：熔断标志

    # P2.2：整体熔断阈值——连续 CIRCUIT_BREAKER_CONSECUTIVE_FAILS 个标的都失败时整体停止
    CIRCUIT_BREAKER_CONSECUTIVE_FAILS = 50

    def _process_batch(batch_ids: list[int], is_retry: bool = False) -> None:
        """处理一批标的（串行或并发），更新 processed/ok_count/failed_count/skipped_count。

        分批限速：每批处理完后（非最后一批）sleep BATCH_INTERVAL_SECONDS 秒。
        P2.2：连续 CIRCUIT_BREAKER_CONSECUTIVE_FAILS 个失败则提前 return（熔断）。
        """
        nonlocal processed, ok_count, failed_count, skipped_count, consecutive_failures, circuit_broken
        if not batch_ids:
            return

        if max_workers <= 1:
            for uid in batch_ids:
                if is_cancelled and is_cancelled():
                    return
                # P2.2：连续失败熔断
                if consecutive_failures >= CIRCUIT_BREAKER_CONSECUTIVE_FAILS:
                    logger.error(
                        "universe bars sync CIRCUIT BREAK: scope=%s %d consecutive failures, abort",
                        scope, consecutive_failures,
                    )
                    circuit_broken = True
                    return
                result = _sync_one_concurrent(uid, history_days)
                processed += 1
                status = result.get("status")
                if status in ("ok", "empty", "uptodate"):
                    ok_count += 1
                    consecutive_failures = 0
                elif status == "skipped":
                    skipped_count += 1
                    consecutive_failures = 0
                else:
                    failed_count += 1
                    consecutive_failures += 1
                    if not is_retry:
                        failed_ids.append(uid)
                if progress_callback:
                    progress_callback(processed, total, ok_count, failed_count)
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_sync_one_concurrent, uid, history_days): uid for uid in batch_ids}
                for future in as_completed(futures):
                    if is_cancelled and is_cancelled():
                        for f in futures:
                            f.cancel()
                        return
                    uid = futures[future]
                    try:
                        result = future.result(timeout=SYNC_ONE_SYMBOL_TIMEOUT_SECONDS)
                        status = result.get("status")
                        if status in ("ok", "empty", "uptodate"):
                            ok_count += 1
                            consecutive_failures = 0
                        elif status == "skipped":
                            skipped_count += 1
                            consecutive_failures = 0
                        else:
                            failed_count += 1
                            consecutive_failures += 1
                            if not is_retry:
                                failed_ids.append(uid)
                    except FuturesTimeoutError:
                        logger.warning("universe sync timeout: symbol_id=%s", uid)
                        failed_count += 1
                        consecutive_failures += 1
                        if not is_retry:
                            failed_ids.append(uid)
                    except Exception as exc:
                        logger.warning("universe sync error: symbol_id=%s: %s", uid, exc)
                        failed_count += 1
                        consecutive_failures += 1
                        if not is_retry:
                            failed_ids.append(uid)
                    processed += 1
                    if progress_callback:
                        progress_callback(processed, total, ok_count, failed_count)
                    # P2.2：连续失败熔断
                    if consecutive_failures >= CIRCUIT_BREAKER_CONSECUTIVE_FAILS:
                        logger.error(
                            "universe bars sync CIRCUIT BREAK: scope=%s %d consecutive failures, abort",
                            scope, consecutive_failures,
                        )
                        circuit_broken = True
                        for f in futures:
                            f.cancel()
                        return

    # 第一轮：按 BATCH_SIZE 分批处理所有 pending_ids
    for batch_start in range(0, len(pending_ids), BATCH_SIZE):
        if is_cancelled and is_cancelled() or circuit_broken:
            break
        batch = pending_ids[batch_start:batch_start + BATCH_SIZE]
        is_last_batch = batch_start + BATCH_SIZE >= len(pending_ids)
        _process_batch(batch, is_retry=False)
        # 批次间隔（最后一批或已取消或已熔断则不 sleep）
        if not is_last_batch and not (is_cancelled and is_cancelled()) and not circuit_broken:
            time.sleep(BATCH_INTERVAL_SECONDS)

    # 第二轮：失败标的重试 1 次（可能是临时网络问题）
    # 失败的标的 sync_failed 已 +1，重试时仍 < SYNC_FAILED_THRESHOLD 才会再试
    # P2.2：熔断触发时跳过重试（数据源异常，重试无意义）
    if failed_ids and not (is_cancelled and is_cancelled()) and not circuit_broken:
        # 重新查询仍可重试的失败标的（sync_failed < 阈值）
        db = SessionLocal()
        try:
            retryable_rows = db.execute(
                select(UniverseSymbol.id).where(
                    UniverseSymbol.id.in_(failed_ids),
                    UniverseSymbol.is_synced == 0,
                    UniverseSymbol.sync_failed < SYNC_FAILED_THRESHOLD,
                )
            ).all()
            retryable_ids = [r[0] for r in retryable_rows]
        finally:
            db.close()

        if retryable_ids:
            logger.info(
                "universe bars sync retry: scope=%s retrying %d/%d failed symbols",
                scope, len(retryable_ids), len(failed_ids),
            )
            # 重试阶段：同样分批限速
            # 重试不重复计数 processed（已计入第一轮），但 ok/failed 会调整
            # 注意：重试成功的标的从 failed_count 移到 ok_count
            for batch_start in range(0, len(retryable_ids), BATCH_SIZE):
                if is_cancelled and is_cancelled():
                    break
                batch = retryable_ids[batch_start:batch_start + BATCH_SIZE]

                # 重试时直接用串行或并发，但不增加 processed（已在第一轮计数）
                if max_workers <= 1:
                    for uid in batch:
                        if is_cancelled and is_cancelled():
                            break
                        result = _sync_one_concurrent(uid, history_days)
                        status = result.get("status")
                        if status in ("ok", "empty", "uptodate"):
                            ok_count += 1
                            failed_count -= 1  # 从失败移到成功
                        elif status == "skipped":
                            skipped_count += 1
                            failed_count -= 1
                        # 仍失败则 failed_count 不变（已计入第一轮）
                        if progress_callback:
                            progress_callback(processed, total, ok_count, failed_count)
                else:
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = {executor.submit(_sync_one_concurrent, uid, history_days): uid for uid in batch}
                        for future in as_completed(futures):
                            if is_cancelled and is_cancelled():
                                for f in futures:
                                    f.cancel()
                                break
                            uid = futures[future]
                            try:
                                result = future.result(timeout=SYNC_ONE_SYMBOL_TIMEOUT_SECONDS)
                                status = result.get("status")
                                if status in ("ok", "empty", "uptodate"):
                                    ok_count += 1
                                    failed_count -= 1
                                elif status == "skipped":
                                    skipped_count += 1
                                    failed_count -= 1
                            except (FuturesTimeoutError, Exception):
                                # 仍失败，failed_count 不变
                                pass
                            if progress_callback:
                                progress_callback(processed, total, ok_count, failed_count)

                # 批次间隔（最后一批或已取消则不 sleep）
                is_last_retry_batch = batch_start + BATCH_SIZE >= len(retryable_ids)
                if not is_last_retry_batch and not (is_cancelled and is_cancelled()):
                    time.sleep(BATCH_INTERVAL_SECONDS)

    logger.info(
        "universe bars sync done: scope=%s total=%d processed=%d ok=%d failed=%d skipped=%d",
        scope, total, processed, ok_count, failed_count, skipped_count,
    )
    return {
        "total": total,
        "processed": processed,
        "ok": ok_count,
        "failed": failed_count,
        "skipped": skipped_count,
    }


# ── 历史回补 ──────────────────────────────────────────────

def backfill_one_universe_symbol(
    db: Session,
    universe_symbol: UniverseSymbol,
    history_days: int,
) -> dict:
    """历史回补单个标的：只向前扩展历史，已有范围内的数据不重拉。

    优化逻辑（历史K线值大概率不变，已有数据直接跳过）：
    1. 查现有最早K线日期 earliest_bar_date
    2. 若 target_start(today - history_days) < earliest_bar_date：
       → 只拉 target_start ~ earliest_bar_date-1（缺失的历史段）
    3. 若 target_start >= earliest_bar_date：
       → 目标范围已在现有数据内，直接跳过（最近几天的补充由增量同步负责）

    场景示例：
    - 已有5年数据，回补10年 → 只拉前5年缺失段，不重拉后5年（速度提升50%+）
    - 已有5年数据，回补3年 → 目标已在现有范围内，直接跳过（秒返回）
    - 无数据 → 全量拉取
    """
    if universe_symbol.sync_failed >= SYNC_FAILED_THRESHOLD:
        return {"symbol": universe_symbol.symbol, "status": "skipped", "reason": "sync_failed threshold reached"}

    today = date.today()
    target_start = today - timedelta(days=history_days)

    # 查现有最早K线日期（用于判断是否需要向前扩展历史）
    earliest_bar = db.execute(
        select(UniverseDailyBar)
        .where(UniverseDailyBar.universe_symbol_id == universe_symbol.id)
        .order_by(UniverseDailyBar.trade_date.asc())
        .limit(1)
    ).scalars().first()

    if earliest_bar is not None:
        existing_start = earliest_bar.trade_date
        if target_start < existing_start:
            # 需要向前扩展历史：只拉缺失的历史段（target_start ~ existing_start-1）
            start_date = target_start
            end_date = existing_start - timedelta(days=1)
            backfill_mode = "extend"
        else:
            # 目标范围已在现有数据内，历史K线值不变，直接跳过
            # 最近几天的补充由增量同步负责，历史回补不重复拉取
            return {
                "symbol": universe_symbol.symbol,
                "status": "skipped",
                "reason": "target range within existing data",
                "mode": "skip",
            }
    else:
        # 无数据，全量拉取
        start_date = target_start
        end_date = today
        backfill_mode = "full"

    # 网络请求前释放 DB 连接
    db.commit()

    try:
        frame = _fetch_universe_history(universe_symbol, start_date, end_date)
    except Exception as exc:
        logger.warning("backfill history failed: %s: %s", universe_symbol.symbol, exc)
        universe_symbol.sync_failed += 1
        db.commit()
        return {"symbol": universe_symbol.symbol, "status": "failed", "error": str(exc)}

    if frame.empty:
        # 空数据可能是退市/停牌，保持已同步状态
        universe_symbol.last_synced_at = _now()
        db.commit()
        return {"symbol": universe_symbol.symbol, "status": "empty", "inserted": 0, "updated": 0}

    inserted, updated = _upsert_universe_bars(db, universe_symbol.id, frame)
    # 更新同步状态
    universe_symbol.is_synced = 1
    universe_symbol.sync_failed = 0
    universe_symbol.last_synced_at = _now()
    # last_bar_date 取该标的所有K线的最新日期（而非本次拉取的最后日期）
    latest = db.execute(
        select(UniverseDailyBar.trade_date)
        .where(UniverseDailyBar.universe_symbol_id == universe_symbol.id)
        .order_by(UniverseDailyBar.trade_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is not None:
        universe_symbol.last_bar_date = latest
    universe_symbol.bar_count = db.execute(
        select(func.count(UniverseDailyBar.id)).where(
            UniverseDailyBar.universe_symbol_id == universe_symbol.id
        )
    ).scalar_one()
    db.commit()
    return {
        "symbol": universe_symbol.symbol,
        "status": "ok",
        "inserted": inserted,
        "updated": updated,
        "mode": backfill_mode,
    }


def _backfill_one_concurrent(universe_symbol_id: int, history_days: int) -> dict:
    """并发 worker：独立 Session 历史回补单个标的。"""
    from sqlalchemy.exc import OperationalError
    sub_db = SessionLocal()
    try:
        us = sub_db.get(UniverseSymbol, universe_symbol_id)
        if us is None:
            return {"symbol": str(universe_symbol_id), "status": "failed", "error": "not found"}
        return backfill_one_universe_symbol(sub_db, us, history_days)
    except OperationalError as exc:
        logger.warning("concurrent backfill %s OperationalError, retrying: %s", universe_symbol_id, exc)
        try:
            sub_db.rollback()
            us = sub_db.get(UniverseSymbol, universe_symbol_id)
            if us is None:
                return {"symbol": str(universe_symbol_id), "status": "failed", "error": "not found"}
            return backfill_one_universe_symbol(sub_db, us, history_days)
        except Exception as exc2:
            logger.error("concurrent backfill %s retry failed: %s", universe_symbol_id, exc2)
            return {"symbol": str(universe_symbol_id), "status": "failed", "error": str(exc2)}
    except Exception as exc:
        logger.error("concurrent backfill %s error: %s", universe_symbol_id, exc)
        return {"symbol": str(universe_symbol_id), "status": "failed", "error": str(exc)}
    finally:
        sub_db.close()


def backfill_universe_bars_batch(
    scope: str,
    max_workers: int = 5,
    history_days: int = DEFAULT_HISTORY_DAYS,
    progress_callback: Callable[[int, int, int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    sync_limit: int = 0,
) -> dict:
    """批量历史回补：对已同步标的（is_synced=1）强制按新 history_days 重新拉取K线。

    与 sync_universe_bars_batch 的区别：
    - sync：查询 is_synced=0（从未同步）
    - backfill：查询 is_synced=1（已同步），强制扩展历史范围

    复用熔断、分批限速、失败重试机制。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

    BATCH_SIZE = 50
    BATCH_INTERVAL_SECONDS = 1.0

    db = SessionLocal()
    try:
        config = _scope_config(scope)
        target_start = date.today() - timedelta(days=history_days)
        earliest_bar_subquery = (
            select(
                UniverseDailyBar.universe_symbol_id.label("universe_symbol_id"),
                func.min(UniverseDailyBar.trade_date).label("earliest_trade_date"),
            )
            .group_by(UniverseDailyBar.universe_symbol_id)
            .subquery()
        )
        query = (
            select(UniverseSymbol.id)
            .outerjoin(
                earliest_bar_subquery,
                earliest_bar_subquery.c.universe_symbol_id == UniverseSymbol.id,
            )
            .where(
                UniverseSymbol.region == config["region"],
                UniverseSymbol.asset_type == config["asset_type"],
                UniverseSymbol.is_synced == 1,
                UniverseSymbol.sync_failed < SYNC_FAILED_THRESHOLD,
                or_(
                    earliest_bar_subquery.c.earliest_trade_date.is_(None),
                    earliest_bar_subquery.c.earliest_trade_date > target_start,
                ),
            )
            .order_by(UniverseSymbol.id.asc())
        )
        if sync_limit > 0:
            query = query.limit(sync_limit)
        logger.info("backfill query: scope=%s sync_limit=%d", scope, sync_limit)
        pending_ids = db.execute(query).scalars().all()
        total = len(pending_ids)
    finally:
        db.close()

    if total == 0:
        logger.info("universe backfill: no synced symbols for scope=%s", scope)
        # 即使 total=0 也通知前端，避免进度卡在上一个 scope 的 total 上
        if progress_callback:
            progress_callback(0, 0, 0, 0)
        return {"total": 0, "processed": 0, "ok": 0, "failed": 0, "skipped": 0}

    logger.info("universe backfill start: scope=%s total=%d workers=%d history_days=%d",
                scope, total, max_workers, history_days)
    # 立即通知前端 total，避免大范围回补时首个标的处理耗时长导致进度长时间为 0
    if progress_callback:
        progress_callback(0, total, 0, 0)
    processed = 0
    ok_count = 0
    failed_count = 0
    skipped_count = 0
    failed_ids: list[int] = []
    consecutive_failures = 0
    circuit_broken = False
    CIRCUIT_BREAKER_CONSECUTIVE_FAILS = 50

    def _process_batch(batch_ids: list[int]) -> None:
        nonlocal processed, ok_count, failed_count, skipped_count, consecutive_failures, circuit_broken
        if not batch_ids:
            return

        if max_workers <= 1:
            for uid in batch_ids:
                if is_cancelled and is_cancelled():
                    return
                if consecutive_failures >= CIRCUIT_BREAKER_CONSECUTIVE_FAILS:
                    logger.error("universe backfill CIRCUIT BREAK: scope=%s %d consecutive failures", scope, consecutive_failures)
                    circuit_broken = True
                    return
                result = _backfill_one_concurrent(uid, history_days)
                processed += 1
                status = result.get("status")
                if status in ("ok", "empty", "uptodate"):
                    ok_count += 1
                    consecutive_failures = 0
                elif status == "skipped":
                    skipped_count += 1
                    consecutive_failures = 0
                else:
                    failed_count += 1
                    consecutive_failures += 1
                    failed_ids.append(uid)
                if progress_callback:
                    progress_callback(processed, total, ok_count, failed_count)
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_backfill_one_concurrent, uid, history_days): uid for uid in batch_ids}
                for future in as_completed(futures):
                    if is_cancelled and is_cancelled():
                        for f in futures:
                            f.cancel()
                        return
                    uid = futures[future]
                    try:
                        result = future.result(timeout=SYNC_ONE_SYMBOL_TIMEOUT_SECONDS)
                        status = result.get("status")
                        if status in ("ok", "empty", "uptodate"):
                            ok_count += 1
                            consecutive_failures = 0
                        elif status == "skipped":
                            skipped_count += 1
                            consecutive_failures = 0
                        else:
                            failed_count += 1
                            consecutive_failures += 1
                            failed_ids.append(uid)
                    except FuturesTimeoutError:
                        logger.warning("universe backfill timeout: symbol_id=%s", uid)
                        failed_count += 1
                        consecutive_failures += 1
                        failed_ids.append(uid)
                    except Exception as exc:
                        logger.warning("universe backfill error: symbol_id=%s: %s", uid, exc)
                        failed_count += 1
                        consecutive_failures += 1
                        failed_ids.append(uid)
                    processed += 1
                    if progress_callback:
                        progress_callback(processed, total, ok_count, failed_count)
                    if consecutive_failures >= CIRCUIT_BREAKER_CONSECUTIVE_FAILS:
                        logger.error("universe backfill CIRCUIT BREAK: scope=%s %d consecutive failures", scope, consecutive_failures)
                        circuit_broken = True
                        for f in futures:
                            f.cancel()
                        return

    # 第一轮
    for batch_start in range(0, len(pending_ids), BATCH_SIZE):
        if (is_cancelled and is_cancelled()) or circuit_broken:
            break
        batch = pending_ids[batch_start:batch_start + BATCH_SIZE]
        is_last_batch = batch_start + BATCH_SIZE >= len(pending_ids)
        _process_batch(batch)
        if not is_last_batch and not (is_cancelled and is_cancelled()) and not circuit_broken:
            time.sleep(BATCH_INTERVAL_SECONDS)

    # 失败重试 1 轮
    if failed_ids and not (is_cancelled and is_cancelled()) and not circuit_broken:
        db = SessionLocal()
        try:
            retryable_rows = db.execute(
                select(UniverseSymbol.id).where(
                    UniverseSymbol.id.in_(failed_ids),
                    UniverseSymbol.sync_failed < SYNC_FAILED_THRESHOLD,
                )
            ).all()
            retryable_ids = [r[0] for r in retryable_rows]
        finally:
            db.close()

        if retryable_ids:
            logger.info("universe backfill retry: scope=%s retrying %d/%d failed symbols", scope, len(retryable_ids), len(failed_ids))
            for batch_start in range(0, len(retryable_ids), BATCH_SIZE):
                if is_cancelled and is_cancelled():
                    break
                batch = retryable_ids[batch_start:batch_start + BATCH_SIZE]
                if max_workers <= 1:
                    for uid in batch:
                        if is_cancelled and is_cancelled():
                            break
                        result = _backfill_one_concurrent(uid, history_days)
                        status = result.get("status")
                        if status in ("ok", "empty", "uptodate"):
                            ok_count += 1
                            failed_count -= 1
                        elif status == "skipped":
                            skipped_count += 1
                            failed_count -= 1
                        if progress_callback:
                            progress_callback(processed, total, ok_count, failed_count)
                else:
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = {executor.submit(_backfill_one_concurrent, uid, history_days): uid for uid in batch}
                        for future in as_completed(futures):
                            if is_cancelled and is_cancelled():
                                for f in futures:
                                    f.cancel()
                                break
                            uid = futures[future]
                            try:
                                result = future.result(timeout=SYNC_ONE_SYMBOL_TIMEOUT_SECONDS)
                                status = result.get("status")
                                if status in ("ok", "empty", "uptodate"):
                                    ok_count += 1
                                    failed_count -= 1
                                elif status == "skipped":
                                    skipped_count += 1
                                    failed_count -= 1
                            except (FuturesTimeoutError, Exception):
                                pass
                            if progress_callback:
                                progress_callback(processed, total, ok_count, failed_count)
                is_last_retry_batch = batch_start + BATCH_SIZE >= len(retryable_ids)
                if not is_last_retry_batch and not (is_cancelled and is_cancelled()):
                    time.sleep(BATCH_INTERVAL_SECONDS)

    logger.info(
        "universe backfill done: scope=%s total=%d processed=%d ok=%d failed=%d skipped=%d",
        scope, total, processed, ok_count, failed_count, skipped_count,
    )
    return {
        "total": total,
        "processed": processed,
        "ok": ok_count,
        "failed": failed_count,
        "skipped": skipped_count,
    }


def _scope_config(scope: str) -> dict:
    configs = {
        "cn-stock": {"region": "cn", "asset_type": "stock"},
        "cn-etf": {"region": "cn", "asset_type": "etf"},
        "us-stock": {"region": "us", "asset_type": "stock"},
        "us-etf": {"region": "us", "asset_type": "etf"},
    }
    if scope not in configs:
        raise ValueError(f"Unsupported scope: {scope}")
    return configs[scope]


# ── 健康度统计 ─────────────────────────────────────────────────────


def repair_one_universe_symbol(
    db: Session,
    universe_symbol: UniverseSymbol,
    start_date: date,
    end_date: date,
    chunk_days: int = 90,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Repair a recent history range in chunks for already-synced symbols."""
    if universe_symbol.sync_failed >= SYNC_FAILED_THRESHOLD:
        return {"symbol": universe_symbol.symbol, "status": "skipped", "reason": "sync_failed threshold reached"}
    if end_date < start_date:
        return {"symbol": universe_symbol.symbol, "status": "skipped", "reason": "invalid range"}

    existing_start = db.execute(
        select(func.min(UniverseDailyBar.trade_date)).where(
            UniverseDailyBar.universe_symbol_id == universe_symbol.id
        )
    ).scalar_one_or_none()
    existing_end = db.execute(
        select(func.max(UniverseDailyBar.trade_date)).where(
            UniverseDailyBar.universe_symbol_id == universe_symbol.id
        )
    ).scalar_one_or_none()

    if existing_start is None or existing_end is None:
        return {"symbol": universe_symbol.symbol, "status": "skipped", "reason": "no existing history"}

    repair_start = max(start_date, existing_start)
    if universe_symbol.listed_at is not None:
        repair_start = max(repair_start, universe_symbol.listed_at)
    repair_end = min(end_date, existing_end)
    if repair_start > repair_end:
        return {"symbol": universe_symbol.symbol, "status": "skipped", "reason": "no overlap in requested range"}

    chunk_span = max(chunk_days, 1)
    inserted_total = 0
    updated_total = 0
    chunk_count = 0
    empty_chunks = 0

    current_start = repair_start
    while current_start <= repair_end:
        if is_cancelled and is_cancelled():
            return {
                "symbol": universe_symbol.symbol,
                "status": "skipped",
                "reason": "cancelled",
                "inserted": inserted_total,
                "updated": updated_total,
                "chunks": chunk_count,
                "empty_chunks": empty_chunks,
            }
        current_end = min(current_start + timedelta(days=chunk_span - 1), repair_end)

        db.commit()
        try:
            frame = _fetch_universe_history(universe_symbol, current_start, current_end)
        except Exception as exc:
            logger.warning("range repair fetch failed: %s %s~%s: %s", universe_symbol.symbol, current_start, current_end, exc)
            universe_symbol.sync_failed += 1
            db.commit()
            return {
                "symbol": universe_symbol.symbol,
                "status": "failed",
                "error": str(exc),
                "inserted": inserted_total,
                "updated": updated_total,
                "chunks": chunk_count,
                "empty_chunks": empty_chunks,
            }

        chunk_count += 1
        if frame.empty:
            empty_chunks += 1
        else:
            inserted, updated = _upsert_universe_bars(db, universe_symbol.id, frame)
            inserted_total += inserted
            updated_total += updated
            db.commit()

        current_start = current_end + timedelta(days=1)

    universe_symbol.is_synced = 1
    universe_symbol.sync_failed = 0
    universe_symbol.last_synced_at = _now()
    latest_trade_date = db.execute(
        select(func.max(UniverseDailyBar.trade_date)).where(
            UniverseDailyBar.universe_symbol_id == universe_symbol.id
        )
    ).scalar_one_or_none()
    if latest_trade_date is not None:
        universe_symbol.last_bar_date = latest_trade_date
    universe_symbol.bar_count = db.execute(
        select(func.count(UniverseDailyBar.id)).where(
            UniverseDailyBar.universe_symbol_id == universe_symbol.id
        )
    ).scalar_one()
    db.commit()
    return {
        "symbol": universe_symbol.symbol,
        "status": "ok",
        "inserted": inserted_total,
        "updated": updated_total,
        "chunks": chunk_count,
        "empty_chunks": empty_chunks,
        "range_start": repair_start.isoformat(),
        "range_end": repair_end.isoformat(),
    }



def _repair_one_concurrent(
    universe_symbol_id: int,
    start_date: date,
    end_date: date,
    chunk_days: int,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Concurrent worker for chunked range repair."""
    from sqlalchemy.exc import OperationalError

    sub_db = SessionLocal()
    try:
        us = sub_db.get(UniverseSymbol, universe_symbol_id)
        if us is None:
            return {"symbol": str(universe_symbol_id), "status": "failed", "error": "not found"}
        return repair_one_universe_symbol(
            sub_db,
            us,
            start_date=start_date,
            end_date=end_date,
            chunk_days=chunk_days,
            is_cancelled=is_cancelled,
        )
    except OperationalError as exc:
        logger.warning("concurrent range repair %s OperationalError, retrying: %s", universe_symbol_id, exc)
        try:
            sub_db.rollback()
            us = sub_db.get(UniverseSymbol, universe_symbol_id)
            if us is None:
                return {"symbol": str(universe_symbol_id), "status": "failed", "error": "not found"}
            return repair_one_universe_symbol(
                sub_db,
                us,
                start_date=start_date,
                end_date=end_date,
                chunk_days=chunk_days,
                is_cancelled=is_cancelled,
            )
        except Exception as exc2:
            logger.error("concurrent range repair %s retry failed: %s", universe_symbol_id, exc2)
            return {"symbol": str(universe_symbol_id), "status": "failed", "error": str(exc2)}
    except Exception as exc:
        logger.error("concurrent range repair %s error: %s", universe_symbol_id, exc)
        return {"symbol": str(universe_symbol_id), "status": "failed", "error": str(exc)}
    finally:
        sub_db.close()



def repair_universe_bars_batch(
    scope: str,
    max_workers: int = 5,
    history_days: int = DEFAULT_HISTORY_DAYS,
    chunk_days: int = 90,
    progress_callback: Callable[[int, int, int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    sync_limit: int = 0,
) -> dict:
    """Repair a recent date range by chunked re-fetch and upsert."""
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

    BATCH_SIZE = 25
    BATCH_INTERVAL_SECONDS = 1.0

    target_end = date.today()
    target_start = target_end - timedelta(days=history_days)

    db = SessionLocal()
    try:
        config = _scope_config(scope)
        existing_range_subquery = (
            select(
                UniverseDailyBar.universe_symbol_id.label("universe_symbol_id"),
                func.min(UniverseDailyBar.trade_date).label("earliest_trade_date"),
                func.max(UniverseDailyBar.trade_date).label("latest_trade_date"),
            )
            .group_by(UniverseDailyBar.universe_symbol_id)
            .subquery()
        )
        query = (
            select(UniverseSymbol.id)
            .join(
                existing_range_subquery,
                existing_range_subquery.c.universe_symbol_id == UniverseSymbol.id,
            )
            .where(
                UniverseSymbol.region == config["region"],
                UniverseSymbol.asset_type == config["asset_type"],
                UniverseSymbol.is_synced == 1,
                UniverseSymbol.sync_failed < SYNC_FAILED_THRESHOLD,
                existing_range_subquery.c.earliest_trade_date <= target_end,
                existing_range_subquery.c.latest_trade_date >= target_start,
            )
            .order_by(UniverseSymbol.id.asc())
        )
        if sync_limit > 0:
            query = query.limit(sync_limit)
        pending_ids = db.execute(query).scalars().all()
        total = len(pending_ids)
    finally:
        db.close()

    if total == 0:
        logger.info("universe range repair: no overlapping symbols for scope=%s", scope)
        if progress_callback:
            progress_callback(0, 0, 0, 0)
        return {
            "total": 0,
            "processed": 0,
            "ok": 0,
            "failed": 0,
            "skipped": 0,
            "inserted": 0,
            "updated": 0,
            "empty_chunks": 0,
        }

    logger.info(
        "universe range repair start: scope=%s total=%d workers=%d history_days=%d chunk_days=%d",
        scope,
        total,
        max_workers,
        history_days,
        chunk_days,
    )
    if progress_callback:
        progress_callback(0, total, 0, 0)

    processed = 0
    ok_count = 0
    failed_count = 0
    skipped_count = 0
    inserted_total = 0
    updated_total = 0
    empty_chunks_total = 0
    failed_ids: list[int] = []
    consecutive_failures = 0
    circuit_broken = False
    CIRCUIT_BREAKER_CONSECUTIVE_FAILS = 50

    def _handle_result(result: dict, uid: int) -> None:
        nonlocal processed, ok_count, failed_count, skipped_count
        nonlocal inserted_total, updated_total, empty_chunks_total, consecutive_failures
        processed += 1
        status = result.get("status")
        if status == "ok":
            ok_count += 1
            consecutive_failures = 0
            inserted_total += int(result.get("inserted", 0) or 0)
            updated_total += int(result.get("updated", 0) or 0)
            empty_chunks_total += int(result.get("empty_chunks", 0) or 0)
        elif status == "skipped":
            skipped_count += 1
            consecutive_failures = 0
            inserted_total += int(result.get("inserted", 0) or 0)
            updated_total += int(result.get("updated", 0) or 0)
            empty_chunks_total += int(result.get("empty_chunks", 0) or 0)
        else:
            failed_count += 1
            consecutive_failures += 1
            failed_ids.append(uid)
        if progress_callback:
            progress_callback(processed, total, ok_count, failed_count)

    def _process_batch(batch_ids: list[int]) -> None:
        nonlocal failed_count, consecutive_failures, circuit_broken
        if not batch_ids:
            return

        if max_workers <= 1:
            for uid in batch_ids:
                if is_cancelled and is_cancelled():
                    return
                if consecutive_failures >= CIRCUIT_BREAKER_CONSECUTIVE_FAILS:
                    logger.error("universe range repair CIRCUIT BREAK: scope=%s %d consecutive failures", scope, consecutive_failures)
                    circuit_broken = True
                    return
                result = _repair_one_concurrent(uid, target_start, target_end, chunk_days, is_cancelled)
                _handle_result(result, uid)
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(_repair_one_concurrent, uid, target_start, target_end, chunk_days, is_cancelled): uid
                    for uid in batch_ids
                }
                for future in as_completed(futures):
                    if is_cancelled and is_cancelled():
                        for f in futures:
                            f.cancel()
                        return
                    uid = futures[future]
                    try:
                        result = future.result(timeout=SYNC_ONE_SYMBOL_TIMEOUT_SECONDS)
                        _handle_result(result, uid)
                    except FuturesTimeoutError:
                        logger.warning("universe range repair timeout: symbol_id=%s", uid)
                        _handle_result({"status": "failed", "error": "timeout"}, uid)
                    except Exception as exc:
                        logger.warning("universe range repair error: symbol_id=%s: %s", uid, exc)
                        _handle_result({"status": "failed", "error": str(exc)}, uid)
                    if consecutive_failures >= CIRCUIT_BREAKER_CONSECUTIVE_FAILS:
                        logger.error("universe range repair CIRCUIT BREAK: scope=%s %d consecutive failures", scope, consecutive_failures)
                        circuit_broken = True
                        for f in futures:
                            f.cancel()
                        return

    for batch_start in range(0, len(pending_ids), BATCH_SIZE):
        if (is_cancelled and is_cancelled()) or circuit_broken:
            break
        batch = pending_ids[batch_start:batch_start + BATCH_SIZE]
        is_last_batch = batch_start + BATCH_SIZE >= len(pending_ids)
        _process_batch(batch)
        if not is_last_batch and not (is_cancelled and is_cancelled()) and not circuit_broken:
            time.sleep(BATCH_INTERVAL_SECONDS)

    logger.info(
        "universe range repair done: scope=%s total=%d processed=%d ok=%d failed=%d skipped=%d inserted=%d updated=%d",
        scope,
        total,
        processed,
        ok_count,
        failed_count,
        skipped_count,
        inserted_total,
        updated_total,
    )
    return {
        "total": total,
        "processed": processed,
        "ok": ok_count,
        "failed": failed_count,
        "skipped": skipped_count,
        "inserted": inserted_total,
        "updated": updated_total,
        "empty_chunks": empty_chunks_total,
    }

def get_universe_stats(db: Session) -> dict:
    """基础数据健康度统计。

    P2.3：增强健康度报告，新增：
    - circuit_broken_symbols：熔断标的数（sync_failed >= 阈值）
    - freshness：数据新鲜度分布（今日最新/1-3天/3-7天/7天以上）
    - by_scope：按 region+asset_type 维度的覆盖率
    """
    total_symbols = db.execute(select(func.count(UniverseSymbol.id))).scalar_one()
    synced_symbols = db.execute(
        select(func.count(UniverseSymbol.id)).where(UniverseSymbol.is_synced == 1)
    ).scalar_one()
    failed_symbols = db.execute(
        select(func.count(UniverseSymbol.id)).where(
            UniverseSymbol.sync_failed >= SYNC_FAILED_THRESHOLD
        )
    ).scalar_one()
    total_bars = db.execute(select(func.count(UniverseDailyBar.id))).scalar_one()

    # 按 asset_type 分组统计
    by_type = {}
    rows = db.execute(
        select(
            UniverseSymbol.asset_type,
            func.count(UniverseSymbol.id),
            func.sum(UniverseSymbol.is_synced),
        ).group_by(UniverseSymbol.asset_type)
    ).all()
    for asset_type, cnt, synced_cnt in rows:
        by_type[asset_type] = {"total": int(cnt), "synced": int(synced_cnt or 0)}

    # 最新同步时间
    latest_synced = db.execute(
        select(func.max(UniverseSymbol.last_synced_at))
    ).scalar_one_or_none()

    # P2.3：数据新鲜度分布（按 last_bar_date 距今天数）
    today = date.today()
    freshness = {"today": 0, "1_3_days": 0, "3_7_days": 0, "over_7_days": 0, "no_data": 0}
    synced_rows = db.execute(
        select(UniverseSymbol.last_bar_date).where(UniverseSymbol.is_synced == 1)
    ).all()
    for (last_bar_date,) in synced_rows:
        if last_bar_date is None:
            freshness["no_data"] += 1
        else:
            delta_days = (today - last_bar_date).days
            if delta_days <= 0:
                freshness["today"] += 1
            elif delta_days <= 3:
                freshness["1_3_days"] += 1
            elif delta_days <= 7:
                freshness["3_7_days"] += 1
            else:
                freshness["over_7_days"] += 1

    # P2.3：按 scope（region+asset_type）维度统计覆盖率
    by_scope = {}
    scope_rows = db.execute(
        select(
            UniverseSymbol.region,
            UniverseSymbol.asset_type,
            func.count(UniverseSymbol.id),
            func.sum(UniverseSymbol.is_synced),
        ).group_by(UniverseSymbol.region, UniverseSymbol.asset_type)
    ).all()
    for region, asset_type, cnt, synced_cnt in scope_rows:
        scope_key = f"{region}-{asset_type}"
        by_scope[scope_key] = {
            "total": int(cnt),
            "synced": int(synced_cnt or 0),
            "coverage": round(int(synced_cnt or 0) / int(cnt) * 100, 1) if cnt else 0,
        }

    # 按板块（board）维度统计覆盖率（仅 A股股票有 board 字段）
    by_board = {}
    board_rows = db.execute(
        select(
            UniverseSymbol.board,
            func.count(UniverseSymbol.id),
            func.sum(UniverseSymbol.is_synced),
        )
        .where(UniverseSymbol.board.is_not(None))
        .group_by(UniverseSymbol.board)
    ).all()
    for board, cnt, synced_cnt in board_rows:
        by_board[board] = {
            "total": int(cnt),
            "synced": int(synced_cnt or 0),
            "coverage": round(int(synced_cnt or 0) / int(cnt) * 100, 1) if cnt else 0,
        }

    return {
        "total_symbols": int(total_symbols),
        "synced_symbols": int(synced_symbols),
        "failed_symbols": int(failed_symbols),
        "circuit_broken_symbols": int(failed_symbols),  # P2.3：熔断标的数（别名，语义更清晰）
        "total_bars": int(total_bars),
        "by_type": by_type,
        "by_scope": by_scope,  # P2.3：按 scope 维度
        "by_board": by_board,  # 按 board 维度
        "freshness": freshness,  # P2.3：新鲜度分布
        "latest_synced_at": latest_synced.isoformat() if latest_synced else None,
        "is_empty": total_symbols == 0,
    }


# ── P2：定时增量同步 ─────────────────────────────────────────────

def incremental_sync(
    max_workers: int = 5,
    progress_callback: Callable[[int, int, int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    scopes: list[str] | None = None,
    as_of: date | datetime | None = None,
) -> dict:
    """增量同步：只同步早于最近已完成交易日且当天未尝试的标的。

    与全量初始化 sync_universe_bars_batch 的区别：
    - 全量初始化：is_synced=0 的标的（从未同步过）
    - 增量同步：is_synced=1 但 last_bar_date < today 的标的（已有数据但不最新）

    使用元数据中的 last_bar_date 断点续传，避免逐标的重复查询最新 K 线。

    Args:
        max_workers: 并发线程数（1-8，默认 5）
        progress_callback: 进度回调 (processed, total, ok_count, failed_count)
        is_cancelled: 取消检查函数，返回 True 时停止
        scopes: 可选 scope 过滤，仅同步指定范围
        as_of: 可选运行日期/时间，用于确定最近已完成交易日

    优化点：
    - 分批限速：每 BATCH_SIZE 个标的一批，批次间隔 BATCH_INTERVAL_SECONDS 秒（与初始化同步一致）

    Returns:
        {"total": int, "processed": int, "ok": int, "failed": int, "skipped": int, "uptodate": int}
    """
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
    run_started_at = time.perf_counter()

    # 分批限速配置：与初始化同步一致
    BATCH_SIZE = 50
    BATCH_INTERVAL_SECONDS = 1.0

    run_date = as_of.date() if isinstance(as_of, datetime) else (as_of or date.today())
    attempted_since = datetime.combine(run_date, datetime.min.time())
    selected_scopes = scopes or ["cn-stock", "cn-etf", "us-stock", "us-etf"]
    selected_regions = {_scope_config(scope)["region"] for scope in selected_scopes}
    target_dates = {
        region: _latest_completed_trading_date(as_of, region)
        for region in selected_regions
    }
    db = SessionLocal()
    try:
        # SQL 层直接筛出目标范围，避免将全市场记录加载后再用 Python 过滤。
        staleness_filters = [
            (UniverseSymbol.region == region)
            & or_(
                UniverseSymbol.last_bar_date.is_(None),
                UniverseSymbol.last_bar_date < target_date,
            )
            for region, target_date in target_dates.items()
        ]
        query = select(UniverseSymbol).where(
            UniverseSymbol.is_synced == 1,
            UniverseSymbol.sync_failed < SYNC_FAILED_THRESHOLD,
            or_(*staleness_filters),
            or_(
                UniverseSymbol.last_synced_at.is_(None),
                UniverseSymbol.last_synced_at < attempted_since,
            ),
        )
        scope_filters = []
        for scope in selected_scopes:
            config = _scope_config(scope)
            scope_filters.append(
                (UniverseSymbol.region == config["region"])
                & (UniverseSymbol.asset_type == config["asset_type"])
            )
        query = query.where(or_(*scope_filters))
        pending_rows = db.execute(
            query.with_only_columns(
                UniverseSymbol.id,
                UniverseSymbol.region,
            )
        ).all()
        pending_items = [
            (universe_symbol_id, target_dates[region])
            for universe_symbol_id, region in pending_rows
        ]
        target_date_by_id = dict(pending_items)
        total = len(pending_items)
    finally:
        db.close()

    if total == 0:
        logger.info(
            "incremental_sync: no stale symbols (all up-to-date as of %s, scopes=%s)",
            target_dates,
            scopes or "all",
        )
        return {
            'total': 0,
            'processed': 0,
            'ok': 0,
            'failed': 0,
            'skipped': 0,
            'uptodate': 0,
            'attempts': 0,
            'fetch_seconds_total': 0.0,
            'database_seconds_total': 0.0,
            'average_fetch_seconds': 0.0,
            'average_database_seconds': 0.0,
            'elapsed_seconds': round(time.perf_counter() - run_started_at, 4),
            'throughput_per_second': 0.0,
        }

    logger.info(
        "incremental_sync start: total=%d workers=%d scopes=%s (target_dates=%s)",
        total,
        max_workers,
        scopes or "all",
        target_dates,
    )
    processed = 0
    ok_count = 0
    failed_count = 0
    skipped_count = 0
    uptodate_count = 0  # 已是最新无需同步的标的
    failed_ids: list[int] = []  # P2.1：第一轮失败的标的，用于重试
    consecutive_failures = 0  # P2.2：连续失败计数（熔断降级）
    attempt_count = 0
    fetch_seconds_total = 0.0
    database_seconds_total = 0.0

    # P2.2：整体熔断阈值——连续 CIRCUIT_BREAKER_CONSECUTIVE_FAILS 个标的都失败时整体停止
    # 避免数据源异常时持续无效请求
    CIRCUIT_BREAKER_CONSECUTIVE_FAILS = 50

    def _record_timings(result: dict) -> None:
        nonlocal attempt_count, fetch_seconds_total, database_seconds_total
        attempt_count += 1
        fetch_seconds_total += float(result.get('fetch_seconds') or 0.0)
        database_seconds_total += float(result.get('database_seconds') or 0.0)

    def _handle_result(result: dict, uid: int) -> None:
        """统一处理单标的同步结果，更新计数器。"""
        nonlocal processed, ok_count, failed_count, skipped_count, uptodate_count, consecutive_failures
        _record_timings(result)
        processed += 1
        status = result.get("status")
        if status == "ok":
            ok_count += 1
            consecutive_failures = 0
        elif status in ("empty", "uptodate"):
            uptodate_count += 1
            consecutive_failures = 0
        elif status == "skipped":
            skipped_count += 1
            consecutive_failures = 0
        else:
            failed_count += 1
            failed_ids.append(uid)
            consecutive_failures += 1
        if progress_callback:
            progress_callback(processed, total, ok_count, failed_count)

    if max_workers <= 1:
        # 串行模式
        for idx, (uid, target_date) in enumerate(pending_items):
            if is_cancelled and is_cancelled():
                logger.info("incremental_sync cancelled at %d/%d", processed, total)
                break
            # P2.2：连续失败熔断——连续 50 个失败则整体停止
            if consecutive_failures >= CIRCUIT_BREAKER_CONSECUTIVE_FAILS:
                logger.error(
                    "incremental_sync CIRCUIT BREAK: %d consecutive failures, abort (data source may be down)",
                    consecutive_failures,
                )
                break
            result = _sync_one_incremental_concurrent(uid, target_date)
            _handle_result(result, uid)
            # 分批限速：每 BATCH_SIZE 个标的后 sleep
            if (idx + 1) % BATCH_SIZE == 0 and (idx + 1) < total and not (is_cancelled and is_cancelled()):
                time.sleep(BATCH_INTERVAL_SECONDS)
    else:
        # Keep every worker occupied: replenish one slot as soon as any request
        # completes instead of waiting for the slowest request in a fixed batch.
        executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="universe-incr")
        future_to_uid: dict = {}
        pending_iter = iter(pending_items)
        next_pause_at = BATCH_SIZE

        def _submit_one() -> bool:
            try:
                uid, target_date = next(pending_iter)
            except StopIteration:
                return False
            future = executor.submit(_sync_one_incremental_concurrent, uid, target_date)
            future_to_uid[future] = uid
            return True

        try:
            for _ in range(max_workers):
                if not _submit_one():
                    break

            while future_to_uid:
                completed, _ = wait(tuple(future_to_uid), return_when=FIRST_COMPLETED)
                for future in completed:
                    uid = future_to_uid.pop(future)
                    try:
                        _handle_result(future.result(), uid)
                    except Exception as exc:
                        logger.warning("incremental_sync %s failed: %s", uid, exc)
                        _handle_result({"status": "failed", "error": str(exc)}, uid)

                cancelled = bool(is_cancelled and is_cancelled())
                circuit_broken = consecutive_failures >= CIRCUIT_BREAKER_CONSECUTIVE_FAILS
                if cancelled or circuit_broken:
                    if circuit_broken:
                        logger.error(
                            "incremental_sync CIRCUIT BREAK: %d consecutive failures, abort",
                            consecutive_failures,
                        )
                    for future in future_to_uid:
                        future.cancel()
                    break

                if processed >= next_pause_at and processed < total:
                    time.sleep(BATCH_INTERVAL_SECONDS)
                    next_pause_at += BATCH_SIZE

                while len(future_to_uid) < max_workers and _submit_one():
                    pass
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    # P2.1：失败标的重试 1 轮（与初始化同步 sync_universe_bars_batch 对称）
    # 重新查询仍可重试的失败标的（sync_failed < 阈值），避免临时网络问题导致的数据缺失
    if failed_ids and not (is_cancelled and is_cancelled()) and consecutive_failures < CIRCUIT_BREAKER_CONSECUTIVE_FAILS:
        db = SessionLocal()
        try:
            retryable_rows = db.execute(
                select(UniverseSymbol.id).where(
                    UniverseSymbol.id.in_(failed_ids),
                    UniverseSymbol.is_synced == 1,
                    UniverseSymbol.sync_failed < SYNC_FAILED_THRESHOLD,
                )
            ).all()
            retryable_ids = [r[0] for r in retryable_rows]
        finally:
            db.close()

        if retryable_ids:
            logger.info(
                "incremental_sync retry: retrying %d/%d failed symbols",
                len(retryable_ids), len(failed_ids),
            )
            # 重试阶段：不增加 processed（已在第一轮计数），但 ok/failed 会调整
            if max_workers <= 1:
                for uid in retryable_ids:
                    if is_cancelled and is_cancelled():
                        break
                    result = _sync_one_incremental_concurrent(uid, target_date_by_id[uid])
                    _record_timings(result)
                    status = result.get("status")
                    if status == "ok":
                        ok_count += 1
                        failed_count -= 1  # 从失败移到成功
                    elif status in ("empty", "uptodate"):
                        uptodate_count += 1
                        failed_count -= 1
                    elif status == "skipped":
                        skipped_count += 1
                        failed_count -= 1
                    # 仍失败则 failed_count 不变（已计入第一轮）
                    if progress_callback:
                        progress_callback(processed, total, ok_count, failed_count)
            else:
                with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="universe-incr-retry") as executor:
                    futures = {
                        executor.submit(
                            _sync_one_incremental_concurrent,
                            uid,
                            target_date_by_id[uid],
                        ): uid
                        for uid in retryable_ids
                    }
                    for future in as_completed(futures):
                        if is_cancelled and is_cancelled():
                            for f in futures:
                                f.cancel()
                            break
                        try:
                            result = future.result()
                            _record_timings(result)
                            status = result.get("status")
                            if status == "ok":
                                ok_count += 1
                                failed_count -= 1
                            elif status in ("empty", "uptodate"):
                                uptodate_count += 1
                                failed_count -= 1
                            elif status == "skipped":
                                skipped_count += 1
                                failed_count -= 1
                        except Exception as exc:
                            logger.warning("incremental_sync retry %s failed: %s", futures[future], exc)
                            # 仍失败，failed_count 不变
                        if progress_callback:
                            progress_callback(processed, total, ok_count, failed_count)

    elapsed_seconds = max(time.perf_counter() - run_started_at, 0.0001)
    throughput = processed / elapsed_seconds
    average_fetch_seconds = fetch_seconds_total / attempt_count if attempt_count else 0.0
    average_database_seconds = database_seconds_total / attempt_count if attempt_count else 0.0
    logger.info(
        "incremental_sync done: total=%d processed=%d ok=%d uptodate=%d failed=%d skipped=%d "
        "elapsed=%.2fs rate=%.2f/s avg_fetch=%.3fs avg_db=%.3fs attempts=%d",
        total, processed, ok_count, uptodate_count, failed_count, skipped_count,
        elapsed_seconds, throughput, average_fetch_seconds, average_database_seconds, attempt_count,
    )
    return {
        "total": total,
        "processed": processed,
        "ok": ok_count,
        "failed": failed_count,
        "skipped": skipped_count,
        "uptodate": uptodate_count,
        "attempts": attempt_count,
        "fetch_seconds_total": round(fetch_seconds_total, 4),
        "database_seconds_total": round(database_seconds_total, 4),
        "average_fetch_seconds": round(average_fetch_seconds, 4),
        "average_database_seconds": round(average_database_seconds, 4),
        "elapsed_seconds": round(elapsed_seconds, 4),
        "throughput_per_second": round(throughput, 4),
    }
