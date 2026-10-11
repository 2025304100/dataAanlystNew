"""白盒 - 预检"无可回测标的"的边界（钉住 VIZ-0929-02）。

要钉的是这条判断：
- 0 只标的 → **阻断**（`NO_BACKTEST_SYMBOLS`），因为 run 阶段 `_filter_symbol_ids_by_rule`
  之后必然为空、必然 400，预检放行就是"预检过 → 执行被拒"的不一致；
- 1~4 只 → 只告警（`SYMBOL_POOL_TOO_SMALL`），不能阻断；
- ≥5 只 → 既不阻断也不告警。

为什么必须有测试：同类判断 `INSUFFICIENT_TRADE_DAYS` 在服务/适配器/前端测试/白盒里
散布 11 处，而这条新分支只出现在定义处 1 处 —— 没有任何用例钉住，边界随时可以被
无声改掉（比如有人把 `== 0` 改成 `< 5`，就直接把"少量标的"从可用变成被拒）。

断言同时检查"存在"和"不存在"两个方向，所以既防漏报也防误报：
补上判定前，用例 1 会因 blocking 码缺失而红；把 `elif` 写歪成 `<= 4` 之类，
用例 2/3 会因不该出现的码而红。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.models.portfolio import Portfolio
from app.models.portfolio_candidate import PortfolioCandidate
from app.models.symbol import Symbol
from app.schemas.bfg_precheck import BacktestPrecheckRequest
from app.services.bfg_precheck_service import run_backtest_precheck

pytestmark = pytest.mark.whitebox

# 与阈值 240 相容的近 1 年窗口：让日历这条判定不干扰标的数这条判定
WINDOW_START = date(2025, 9, 29)
WINDOW_END = date(2026, 9, 28)

ZERO_CODE = "NO_BACKTEST_SYMBOLS"
SMALL_CODE = "SYMBOL_POOL_TOO_SMALL"


def _seed_weekday_calendar(db, start: date, end: date) -> int:
    db.execute(text(
        "CREATE TABLE IF NOT EXISTS trade_calendar ("
        "  date DATE NOT NULL,"
        "  is_trading_day INTEGER NOT NULL"
        ")"
    ))
    db.execute(text("DELETE FROM trade_calendar"))
    rows = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            rows.append({"d": cursor.isoformat(), "f": 1})
        cursor += timedelta(days=1)
    db.execute(
        text("INSERT INTO trade_calendar (date, is_trading_day) VALUES (:d, :f)"),
        rows,
    )
    db.commit()
    return len(rows)


def _codes(items) -> set[str]:
    return {str(getattr(item, "code", "")) for item in items}


def _find(items, code: str):
    for item in items:
        if str(getattr(item, "code", "")) == code:
            return item
    return None


def _run(db, symbol_ids: list[int]):
    return run_backtest_precheck(db, BacktestPrecheckRequest(
        portfolio_id=1, symbol_ids=symbol_ids,
        start_date=WINDOW_START, end_date=WINDOW_END,
    ))


# ---------------------------------------------------------------------------


def test_zero_symbols_is_blocked_with_full_fix_guidance(db_session):
    """0 只标的必须阻断，且要把"下一步去哪"带全（前端按 fix_link 摊开指引）。"""
    seeded = _seed_weekday_calendar(db_session, WINDOW_START, WINDOW_END)
    assert seeded >= 240, f"测试前提不成立：交易日只有 {seeded} 天，会被日历门禁抢先"

    resp = _run(db_session, [])
    blocking = list(resp.blocking_reasons)
    assert ZERO_CODE in _codes(blocking), (
        f"0 只标的却没有阻断，预检会放行到一个必然 400 的 run：{[c for c in _codes(blocking)]}"
    )

    blocker = _find(blocking, ZERO_CODE)
    assert blocker.severity == "error"
    assert blocker.category == "universe", "归类错了前端会把它显示成数据/配置问题"
    assert int(blocker.evidence.get("symbol_count", -1)) == 0
    # 前端 PortfolioBacktestCenter 直接读 label_zh 渲染"下一步"，缺了就是一句空指引
    fix_link = getattr(blocker, "fix_link", None)
    assert fix_link, f"{ZERO_CODE} 必须带 fix_link 指引：{blocker}"
    assert str(getattr(fix_link, "label_zh", "") or (fix_link.get("label_zh") if isinstance(fix_link, dict) else "")), (
        "fix_link 没有 label_zh，界面会只剩一个看不懂的跳转"
    )
    assert SMALL_CODE not in _codes(resp.warnings), (
        "0 只已经阻断了，不该再叠一条『数量过少』告警（同一问题说两遍）"
    )


def test_one_to_four_symbols_warn_but_do_not_block(db_session):
    """1~4 只是统计显著性问题：只能告警，不能挡住用户跑回测。"""
    _seed_weekday_calendar(db_session, WINDOW_START, WINDOW_END)

    resp = _run(db_session, [11, 12, 13])
    assert ZERO_CODE not in _codes(resp.blocking_reasons), (
        f"3 只标的被阻断了：少量标的应是告警而不是拒绝执行 → {[str(b.code) for b in resp.blocking_reasons]}"
    )
    assert SMALL_CODE in _codes(resp.warnings), (
        f"3 只标的应给『数量过少』告警，实际 warnings={_codes(resp.warnings)}"
    )
    warn = _find(resp.warnings, SMALL_CODE)
    assert warn.severity == "warning"
    assert int(warn.evidence.get("symbol_count", -1)) == 3


def test_five_symbols_neither_blocks_nor_warns(db_session):
    """边界下界：5 只就算够数，别把告警范围悄悄扩大。"""
    _seed_weekday_calendar(db_session, WINDOW_START, WINDOW_END)

    resp = _run(db_session, [21, 22, 23, 24, 25])
    assert ZERO_CODE not in _codes(resp.blocking_reasons)
    assert SMALL_CODE not in _codes(resp.warnings), (
        f"5 只标的仍在告警，说明阈值被改大了：warnings={_codes(resp.warnings)}"
    )


def test_autofill_from_candidate_pool_does_not_claim_no_symbols(db_session):
    """不带 symbol_ids 时，预检必须从「成员 ∪ 候选池」auto-fill 出标的。

    钉住的缺陷：原先写 `union_all(...).distinct().subquery()`，而 SQLAlchemy 2.0 的
    CompoundSelect 没有 .distinct() → auto-fill **每次必抛** AttributeError，又被外层
    `except Exception` 吞成 [] → 直接报 NO_BACKTEST_SYMBOLS、提示“去同步基础数据”，
    而数据其实就在库里。等于“任何不带 symbol_ids 的回测请求都被误阻断”。

    这条分支此前零覆盖：上面的边界用例都显式传 symbol_ids，所以缺陷一直活着。
    如果把这个用例改回旧写法（加 .distinct()）它就立刻变红。
    """
    _seed_weekday_calendar(db_session, WINDOW_START, WINDOW_END)

    # _run() 里写的是 portfolio_id=1，候选池的外键要求这个组合必须存在，
    # 所以显式用 id=1 建一个（否则先撞 FK，报的就不是被测分支了）。
    db_session.add(
        Portfolio(
            id=1, name="auto-fill 回归组合", account_type="simulated",
            total_capital=200000.0, investable_ratio=0.9, cash_reserve_ratio=0.1,
            currency="CNY", is_default=0, auto_trade_enabled=1,
        )
    )
    db_session.commit()

    sym = Symbol(
        symbol="600999", name="auto-fill 回归标的", asset_type="stock",
        market="SSE", board="main",
    )
    db_session.add(sym)
    db_session.commit()

    db_session.add(
        PortfolioCandidate(
            portfolio_id=1, symbol_id=sym.id,
            effective_from=WINDOW_START, effective_to=None,
            auto_authorized_flag=1, removed_manually_flag=0,
            audit_version=1, source_type="manual",
            pool_memberships_json="[]", admission_snapshot_json="{}",
        )
    )
    db_session.commit()

    resp = _run(db_session, [])  # 空 symbol_ids → 走 auto-fill 分支

    blocked = _codes(resp.blocking_reasons)
    assert ZERO_CODE not in blocked, (
        f"候选池里有标的却报 NO_BACKTEST_SYMBOLS，auto-fill 又坏了：{blocked}；"
        f"evidence={[b.evidence for b in resp.blocking_reasons if str(b.code) == ZERO_CODE]}"
    )
    # 不能再有被吞掉的查询异常（修好后这个键应该不出现）
    item = _find(resp.blocking_reasons, ZERO_CODE)
    assert item is None or "universe_source_error" not in (item.evidence or {}), (
        "auto-fill 查询报错又被静默当成‘没标的’："
        f"{(item.evidence if item else {})}"
    )
