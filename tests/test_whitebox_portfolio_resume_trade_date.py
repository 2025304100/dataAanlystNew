"""`portfolio_resume_service` 交易日解析的回归守护。

背景：该模块从写下那天起就写着
`from app.models.market_data import MarketCalendar`，但**那个模块当时并不存在** ——
ImportError 被外层 `except Exception` 静默吞掉，于是 `_next_trade_date` /
`_is_trade_date` **从未真正查过数据库**，一直走「周一~周五」的弱近似：
国庆这类长假会被当成交易日排进「待补算交易日」，resume 时对着一堆没有数据的
假期执行决策。

这里钉住三条：
1. 跨假期必须跳到**真正的下一交易日**（不是下一个工作日）；
2. 假期与周末必须判为非交易日，表里没记录的日期也不能默认算开市；
3. 日历查询失败时要**留痕**，且 `_is_trade_date` 不得退回「工作日即交易日」。
"""
from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path

import pytest

from app.models.market_data import MarketCalendar
from app.services.portfolio_resume_service import _is_trade_date, _next_trade_date

# 这个文件得带 marker，否则 `pytest -m whitebox`（本地闸门与 CI 分片）会
# 静默跳过它——守护 test_every_test_file_declares_a_ci_marker 正是为这类盲区而存在。
pytestmark = pytest.mark.whitebox


@pytest.fixture
def tmp_alembic_db():
    """与 G1/G4 系列一致的 SQLite + alembic head fixture。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_resume_td_")
    os.close(fd)
    url = f"sqlite:///{path}"
    os.environ["ALEMBIC_DATABASE_URL"] = url
    from alembic import command as ac
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option(
        "script_location", str(Path(__file__).resolve().parent.parent / "alembic")
    )
    ac.upgrade(cfg, "head")

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(url)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()

# 2026 国庆：10-01 ~ 10-07 休市，10-08 开市（经交易所日历校正后的真实安排）
_NATIONAL_DAY_OFF = [date(2026, 10, d) for d in range(1, 8)]


def _seed(db, *, on=(), off=()) -> None:
    for d in on:
        db.add(MarketCalendar(trade_date=d, is_trading_day=1))
    for d in off:
        db.add(MarketCalendar(trade_date=d, is_trading_day=0))
    db.flush()


def test_next_trade_date_skips_national_holiday(tmp_alembic_db):
    """跨国庆要跳到 10-08 —— 修复前的 weekday 近似会给 10-01。"""
    db = tmp_alembic_db
    _seed(
        db,
        on=[date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30),
            date(2026, 10, 8), date(2026, 10, 9)],
        off=_NATIONAL_DAY_OFF,
    )
    assert _next_trade_date(db, date(2026, 9, 30)) == date(2026, 10, 8)


def test_next_trade_date_skips_weekend(tmp_alembic_db):
    """周五之后是周一。"""
    db = tmp_alembic_db
    _seed(
        db,
        on=[date(2026, 9, 25), date(2026, 9, 28)],
        off=[date(2026, 9, 26), date(2026, 9, 27)],
    )
    assert _next_trade_date(db, date(2026, 9, 25)) == date(2026, 9, 28)


def test_is_trade_date_false_on_holiday_and_unknown(tmp_alembic_db):
    """节假日、周末都判非交易日；表里没记录的日期同样不能算开市。"""
    db = tmp_alembic_db
    _seed(
        db,
        on=[date(2026, 9, 30), date(2026, 10, 8)],
        off=_NATIONAL_DAY_OFF + [date(2026, 10, 10), date(2026, 10, 11)],
    )
    assert _is_trade_date(db, date(2026, 9, 30)) is True
    assert _is_trade_date(db, date(2026, 10, 8)) is True
    assert _is_trade_date(db, date(2026, 10, 1)) is False
    assert _is_trade_date(db, date(2026, 10, 6)) is False
    assert _is_trade_date(db, date(2026, 10, 10)) is False
    assert _is_trade_date(db, date(2026, 12, 31)) is False


def test_calendar_breakdown_is_logged_and_never_fakes_a_trading_day(
    tmp_alembic_db, monkeypatch, caplog
):
    """日历查询失败时：留痕 + 不把工作日当交易日。

    用「模型属性缺失」来制造失败 —— 这正是线上真实发生过的形态
    （`app/models/market_data.py` 不存在导致的 AttributeError/ImportError）。
    旧的 `_is_trade_date` 在此分支 `return d.weekday() < 5`，
    会把 2026-10-01（周四、国庆）当成交易日。
    """
    import app.models.market_data as md
    import app.services.portfolio_resume_service as mod

    class _BrokenModel:
        """模拟「模型与真实表结构对不上」：访问列属性即炸。"""

    monkeypatch.setattr(md, "MarketCalendar", _BrokenModel)

    db = tmp_alembic_db
    with caplog.at_level("WARNING"):
        assert _is_trade_date(db, date(2026, 10, 1)) is False

    assert any("trade_calendar" in str(r.message) for r in caplog.records), (
        "日历查询失败必须留痕，不能再静默降级"
    )
    assert mod is not None
