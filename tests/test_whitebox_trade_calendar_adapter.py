"""白盒测试 - 交易日历 Adapter 的 fail-closed 语义（PT-DEF-24 回归）。

覆盖 `app/services/bfg_trade_calendar_adapter.py`：
1. 裸表 `trade_calendar` 的 `date` 列在不同驱动下类型不同（MySQL 给 date、
   SQLite 裸 SQL 给字符串），两种都必须能读出来；
2. 读不懂的值必须整体放弃该数据源（不能只丢一行，那会让日历悄悄变短）；
3. 空表 ≠ 不可用：空表返回 []，由调用方报 INSUFFICIENT_TRADE_DAYS；
4. 两个数据源都没有时仍然抛 TradeCalendarUnavailableError（严禁 5/7 粗估兜底）。

修 PT-DEF-24 前，第 1 项在 SQLite 下必然失败：字符串被判成"未知类型"，
于是任何用 SQLite 的环境（含全部本地测试）回测预检永远 fail-closed。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.services.bfg_trade_calendar_adapter import (
    TradeCalendarUnavailableError,
    _coerce_calendar_value,
    _query_trade_calendar_table,
    get_trading_days,
)

pytestmark = pytest.mark.whitebox


START = date(2026, 1, 5)
END = date(2026, 1, 9)


def _create_calendar_table(db) -> None:
    db.execute(text(
        "CREATE TABLE IF NOT EXISTS trade_calendar ("
        "  date DATE NOT NULL,"
        "  is_trading_day INTEGER NOT NULL"
        ")"
    ))
    db.commit()


def _insert_days(db, values: list[tuple[object, int]]) -> None:
    db.execute(text("DELETE FROM trade_calendar"))
    db.execute(
        text("INSERT INTO trade_calendar (date, is_trading_day) VALUES (:d, :f)"),
        [{"d": v, "f": f} for v, f in values],
    )
    db.commit()


# ---------------------------------------------------------------------------
# 1. 值类型归一
# ---------------------------------------------------------------------------


def test_coerce_accepts_date_datetime_and_iso_strings():
    assert _coerce_calendar_value(date(2026, 1, 5)) == date(2026, 1, 5)
    assert _coerce_calendar_value(datetime(2026, 1, 5, 15, 30)) == date(2026, 1, 5)
    assert _coerce_calendar_value("2026-01-05") == date(2026, 1, 5)
    assert _coerce_calendar_value("2026-01-05 00:00:00") == date(2026, 1, 5)
    assert _coerce_calendar_value("2026-01-05T00:00:00.123456") == date(2026, 1, 5)
    assert _coerce_calendar_value(b"2026-01-05") == date(2026, 1, 5)


def test_coerce_rejects_garbage_and_empty():
    assert _coerce_calendar_value(None) is None
    assert _coerce_calendar_value("") is None
    assert _coerce_calendar_value("   ") is None
    assert _coerce_calendar_value("01/05/2026") is None
    assert _coerce_calendar_value("2026-13-40") is None
    assert _coerce_calendar_value(20260105) is None


# ---------------------------------------------------------------------------
# 2. 裸表读取（PT-DEF-24 主现场）
# ---------------------------------------------------------------------------


def test_sqlite_string_date_column_is_readable(db_session):
    """SQLite 下 DATE 列以字符串回来 —— 旧实现会误判成数据源不可用。"""
    _create_calendar_table(db_session)
    _insert_days(db_session, [
        (d.isoformat(), 1)
        for d in [START, START + timedelta(days=1), START + timedelta(days=2)]
    ])

    days = _query_trade_calendar_table(db_session, START, END)

    assert days is not None, (
        "adapter 把 SQLite 读出的字符串当成未知类型，直接放弃了 trade_calendar "
        "数据源（PT-DEF-24 的原始症状）"
    )
    assert days == [START, START + timedelta(days=1), START + timedelta(days=2)]
    assert all(isinstance(d, date) and not isinstance(d, datetime) for d in days)


def test_get_trading_days_uses_raw_table_on_sqlite(db_session):
    """端到端入口也得走通：get_trading_days 返回 date 列表而不是抛不可用。"""
    _create_calendar_table(db_session)
    _insert_days(db_session, [(START.isoformat(), 1), (END.isoformat(), 1)])

    days = get_trading_days(START, END, session=db_session)

    assert days == [START, END]


def test_non_trading_days_are_filtered_out(db_session):
    _create_calendar_table(db_session)
    _insert_days(db_session, [
        (START.isoformat(), 1),
        ((START + timedelta(days=1)).isoformat(), 0),  # 非交易日
    ])

    days = _query_trade_calendar_table(db_session, START, END)

    assert days == [START], f"is_trading_day=0 的行不应被算进交易日：{days}"


def test_window_bounds_are_respected(db_session):
    """窗口外的日期不得混进来：那会把 usable_trade_days 算虚高。"""
    _create_calendar_table(db_session)
    _insert_days(db_session, [
        ((START - timedelta(days=3)).isoformat(), 1),
        (START.isoformat(), 1),
        (END.isoformat(), 1),
        ((END + timedelta(days=3)).isoformat(), 1),
    ])

    days = _query_trade_calendar_table(db_session, START, END)

    assert days == [START, END], f"越界日期被返回：{days}"


def test_unparseable_value_abandons_whole_source(db_session):
    """只要有一行读不懂就整体放弃该数据源，不能只丢一行让日历变短。

    窗口取宽：SQLite 下 date 列是文本比较，字母序永远大于数字，垃圾串用小窗口
    会被范围条件先过滤掉，测不到“读出来了但认不出”这条分支。
    """
    wide_start, wide_end = date(2020, 1, 1), date(2030, 1, 1)
    _create_calendar_table(db_session)
    db_session.execute(text("DELETE FROM trade_calendar"))
    db_session.execute(text(
        "INSERT INTO trade_calendar (date, is_trading_day) VALUES (:d, 1)"
    ), [{"d": "2026-01-05"}, {"d": "2026-13-40"}])
    db_session.commit()

    assert _query_trade_calendar_table(db_session, wide_start, wide_end) is None


def test_empty_table_is_not_reported_unavailable(db_session):
    """空表是合法结果 []：语义是「没有交易日」，不是「日历服务坏了」。"""
    _create_calendar_table(db_session)
    db_session.execute(text("DELETE FROM trade_calendar"))
    db_session.commit()

    assert _query_trade_calendar_table(db_session, START, END) == []
    assert get_trading_days(START, END, session=db_session) == []


# ---------------------------------------------------------------------------
# 3. fail-closed 底线仍在
# ---------------------------------------------------------------------------


def test_raises_when_both_sources_missing(db_session, monkeypatch):
    """表不存在 + 无 calendar_utils 时必须抛错，绝不回退 5/7 自然日粗估。"""
    db_session.execute(text("DROP TABLE IF EXISTS trade_calendar"))
    db_session.commit()
    import sys

    monkeypatch.delitem(sys.modules, "calendar_utils", raising=False)

    with pytest.raises(TradeCalendarUnavailableError):
        get_trading_days(START, END, session=db_session)


def test_start_after_end_returns_empty_without_touching_sources(db_session):
    """反向窗口直接给空，不去探测数据源（旧契约保持）。"""
    assert get_trading_days(END, START, session=db_session) == []


def test_missing_table_does_not_leak_db_error(db_session):
    """表不存在时不能把 DB 异常抛给调用方，要安静地走 fallback。"""
    db_session.execute(text("DROP TABLE IF EXISTS trade_calendar"))
    db_session.commit()
    try:
        assert _query_trade_calendar_table(db_session, START, END) is None
    except SQLAlchemyError as exc:  # pragma: no cover - 守卫
        pytest.fail(f"表不存在时应返回 None 走 fallback，而不是抛 DB 错误：{exc}")
