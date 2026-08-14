"""白盒测试 - WP7.1 组合回测成员化（按有效日期读取成员）+ WP7.3 兼容与切换。

守护 ``app/services/portfolio_backtest.py`` 中 WP7.1 新增接口的关键行为，
防止重构/优化时退化：

1. ``get_effective_members_for_date`` 基本读取 / 未来成员排除 / 归档成员排除 /
   paused 排除 / only_auto 过滤 / exclude_member_ids 过滤
2. ``get_member_symbols_for_date`` 返回 symbol_id 去重列表
3. ``validate_member_eligibility`` 全 auto 通过 / 含 manual 返回排除清单 /
   缺失 entry_rule_version_id 返回排除清单

WP7.3 兼容与切换新增测试：
4. ``run_portfolio_backtest`` 在 member 来源下填充 WP7.2 全部快照字段
5. ``run_portfolio_backtest`` 在 legacy 来源下仅填充最小快照集
6. 存在 manual/confirm 成员时默认阻止完整回测
7. ``only_auto=True`` 跳过 manual 成员并记录 excluded_members
8. ``compare_new_old_engine`` 返回新旧引擎差异
9. 切换功能开关后历史 BacktestRun 仍可读（legacy / member 来源）

测试用 SQLite 内存库（``db_session`` fixture），每个用例独立 session。

参照 spec WP7 line 261-264：
- 未来才加入的成员（effective_from > trade_date）绝不出现
- 中途归档的成员（effective_to < trade_date）不参与后续回测
- 同一 trade_date 重复读取结果一致（除非数据被修改）

注：本文件为 WP7.5 的预备工作，先写最小骨架覆盖 WP7.1 新增接口的纯函数行为，
不涉及与 ``run_backtest`` 引擎的端到端集成（那部分由现有
``test_whitebox_portfolio_backtest.py`` 守护，且功能开关默认关闭时行为不变）。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.models.backtest import BacktestRun, BacktestTrade
from app.models.daily_bar import DailyBar
from app.models.portfolio import Portfolio, PortfolioRule, Position
from app.models.portfolio_candidate import PortfolioCandidate
from app.models.portfolio_member import (
    EXECUTION_AUTO,
    EXECUTION_CONFIRM,
    EXECUTION_MANUAL,
    PortfolioMember,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_PAUSED,
)
from app.models.scan import ScanResult, ScanRun
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.portfolio_backtest import (
    _derive_current_universe_symbol_ids,
    _derive_symbol_ids,
    compare_new_old_engine,
    get_effective_members_for_date,
    get_member_symbols_for_date,
    run_portfolio_backtest,
    validate_member_eligibility,
)


def test_portfolio_candidate_is_available_to_legacy_backtest_source(db_session):
    """组合专属候选池应被旧来源识别，不能误报“无候选标的”。"""
    portfolio = _make_portfolio(db_session, "QA-candidate-legacy")
    symbol = _make_symbol(db_session, "600901")
    db_session.add(PortfolioCandidate(portfolio_id=portfolio.id, symbol_id=symbol.id))
    db_session.commit()

    assert _derive_symbol_ids(db_session, portfolio.id) == [symbol.id]


def test_current_universe_ignores_historical_membership_date_and_keeps_candidates(db_session):
    """当前组合配置回测不应按历史区间排除今天才加入的候选或成员。"""
    portfolio = _make_portfolio(db_session, "QA-current-universe")
    candidate_symbol = _make_symbol(db_session, "600902")
    member_symbol = _make_symbol(db_session, "600903")
    manual_symbol = _make_symbol(db_session, "600904")
    db_session.add(PortfolioCandidate(portfolio_id=portfolio.id, symbol_id=candidate_symbol.id))
    _make_member(
        db_session,
        portfolio_id=portfolio.id,
        symbol_id=member_symbol.id,
        execution_mode=EXECUTION_AUTO,
        effective_from=datetime(2026, 8, 7),
    )
    _make_member(
        db_session,
        portfolio_id=portfolio.id,
        symbol_id=manual_symbol.id,
        execution_mode=EXECUTION_MANUAL,
        effective_from=datetime(2026, 8, 7),
    )
    db_session.commit()

    assert _derive_current_universe_symbol_ids(
        db_session, portfolio.id, only_auto=True
    ) == sorted([candidate_symbol.id, member_symbol.id])


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _utc(dt: datetime) -> datetime:
    """将 datetime 规范化为 naive UTC（与项目其他模型保持一致）。"""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _make_portfolio(db_session, name: str = "QA-WP7-PF") -> Portfolio:
    pf = Portfolio(
        name=name,
        account_type="simulated",
        total_capital=100000.0,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
        currency="CNY",
        auto_trade_enabled=1,
    )
    db_session.add(pf)
    db_session.commit()
    db_session.refresh(pf)
    return pf


def _make_symbol(db_session, symbol: str = "600000") -> Symbol:
    sym = Symbol(
        symbol=symbol,
        name=f"测试-{symbol}",
        asset_type="stock",
        market="SH",
    )
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_member(
    db_session,
    *,
    portfolio_id: int,
    symbol_id: int,
    status: str = STATUS_ACTIVE,
    execution_mode: str = EXECUTION_AUTO,
    entry_rule_version_id: int | None = 1,
    exit_rule_version_id: int | None = 2,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
    source_type: str = "manual",
) -> PortfolioMember:
    """直接构造 PortfolioMember 记录，用于精确控制 effective_from/effective_to。

    注意：模型有部分唯一索引（portfolio_id, symbol_id where effective_to IS NULL），
    同一组合同一标的同时只能有一条 effective_to IS NULL 的记录。
    """
    now = _utc(datetime.now(timezone.utc))
    member = PortfolioMember(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        status=status,
        execution_mode=execution_mode,
        source_type=source_type,
        entry_rule_version_id=entry_rule_version_id,
        exit_rule_version_id=exit_rule_version_id,
        effective_from=_utc(effective_from) if effective_from else now,
        effective_to=_utc(effective_to) if effective_to else None,
        manual_lock=False,
        priority=0,
        created_at=now,
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    return member


# ----------------------------------------------------------------------------
# 1. get_effective_members_for_date 基本读取
# ----------------------------------------------------------------------------


def test_get_effective_members_for_date_basic(db_session):
    """【WP7.1】基本读取：trade_date 落在 effective 区间内的 active 成员被返回。

    场景：
    - 成员 A：effective_from = T-10, effective_to = None（持续有效）
    - 成员 B：effective_from = T-5, effective_to = T+5（窗口内有效）
    - trade_date = T
    - 期望返回 A 和 B
    """
    pf = _make_portfolio(db_session, name="QA-WP7-Basic")
    sym_a = _make_symbol(db_session, symbol="600100")
    sym_b = _make_symbol(db_session, symbol="600101")

    base = datetime(2026, 1, 10, 12, 0, 0)
    _make_member(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym_a.id,
        effective_from=base - timedelta(days=10),
        effective_to=None,
    )
    _make_member(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym_b.id,
        effective_from=base - timedelta(days=5),
        effective_to=base + timedelta(days=5),
    )

    result = get_effective_members_for_date(
        db_session, portfolio_id=pf.id, trade_date=base
    )

    assert len(result) == 2
    returned_ids = {m.id for m in result}
    # 按 id 排序，方便断言
    expected_symbols = {sym_a.id, sym_b.id}
    returned_symbols = {m.symbol_id for m in result}
    assert returned_symbols == expected_symbols
    # 全部 active
    assert all(m.status == STATUS_ACTIVE for m in result)
    # 返回顺序按 id 升序
    assert [m.id for m in result] == sorted(returned_ids)


# ----------------------------------------------------------------------------
# 2. 未来成员不返回
# ----------------------------------------------------------------------------


def test_future_members_excluded(db_session):
    """【WP7.1】effective_from > trade_date 的成员不返回（未来成员不污染过去）。

    参照 spec line 261-263："未来才加入的成员（effective_from > trade_date）绝不出现"。
    """
    pf = _make_portfolio(db_session, name="QA-WP7-Future")
    sym_past = _make_symbol(db_session, symbol="600200")
    sym_future = _make_symbol(db_session, symbol="600201")

    trade_date = datetime(2026, 1, 10, 12, 0, 0)

    # 过去已生效成员（应被返回）
    _make_member(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym_past.id,
        effective_from=trade_date - timedelta(days=5),
        effective_to=None,
    )
    # 未来才生效成员（应被排除）
    _make_member(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym_future.id,
        effective_from=trade_date + timedelta(days=5),
        effective_to=None,
    )

    result = get_effective_members_for_date(
        db_session, portfolio_id=pf.id, trade_date=trade_date
    )

    assert len(result) == 1
    assert result[0].symbol_id == sym_past.id
    # 未来成员绝不出现
    assert all(m.symbol_id != sym_future.id for m in result)


# ----------------------------------------------------------------------------
# 3. 归档成员（effective_to < trade_date）不返回
# ----------------------------------------------------------------------------


def test_archived_members_excluded_after_effective_to(db_session):
    """【WP7.1】effective_to < trade_date 的成员不返回（中途归档不参与后续回测）。

    参照 spec line 264："中途归档成员只参与有效期内回测"。

    场景：
    - 成员 A：effective_from = T-10, effective_to = T-2（已归档，trade_date 之后不应返回）
    - 成员 B：effective_from = T-5, effective_to = None（持续有效，应返回）
    - trade_date = T
    - 期望只返回 B
    """
    pf = _make_portfolio(db_session, name="QA-WP7-Archived")
    sym_archived = _make_symbol(db_session, symbol="600300")
    sym_active = _make_symbol(db_session, symbol="600301")

    trade_date = datetime(2026, 1, 10, 12, 0, 0)

    # 已归档成员：effective_to 在 trade_date 之前
    _make_member(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym_archived.id,
        effective_from=trade_date - timedelta(days=10),
        effective_to=trade_date - timedelta(days=2),
    )
    # 持续有效成员
    _make_member(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym_active.id,
        effective_from=trade_date - timedelta(days=5),
        effective_to=None,
    )

    result = get_effective_members_for_date(
        db_session, portfolio_id=pf.id, trade_date=trade_date
    )

    assert len(result) == 1
    assert result[0].symbol_id == sym_active.id
    # 归档成员不出现
    assert all(m.symbol_id != sym_archived.id for m in result)


# ----------------------------------------------------------------------------
# 4. paused 成员不返回
# ----------------------------------------------------------------------------


def test_paused_members_excluded(db_session):
    """【WP7.1】status='paused' 的成员不返回（只返回 status='active'）。

    参照 SQL 条件 `status = 'active'`。
    pause 不归档（effective_to 仍为 None），但 status='paused' 应被过滤。
    """
    pf = _make_portfolio(db_session, name="QA-WP7-Paused")
    sym_active = _make_symbol(db_session, symbol="600400")
    sym_paused = _make_symbol(db_session, symbol="600401")

    trade_date = datetime(2026, 1, 10, 12, 0, 0)

    # active 成员
    _make_member(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym_active.id,
        status=STATUS_ACTIVE,
        effective_from=trade_date - timedelta(days=5),
    )
    # paused 成员（effective_to 仍为 None，但 status='paused'）
    _make_member(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym_paused.id,
        status=STATUS_PAUSED,
        effective_from=trade_date - timedelta(days=5),
    )

    result = get_effective_members_for_date(
        db_session, portfolio_id=pf.id, trade_date=trade_date
    )

    assert len(result) == 1
    assert result[0].symbol_id == sym_active.id
    assert result[0].status == STATUS_ACTIVE


# ----------------------------------------------------------------------------
# 5. only_auto=True 过滤
# ----------------------------------------------------------------------------


def test_only_auto_filter(db_session):
    """【WP7.1】only_auto=True 时只返回 execution_mode='auto' 的成员。"""
    pf = _make_portfolio(db_session, name="QA-WP7-OnlyAuto")
    sym_auto = _make_symbol(db_session, symbol="600500")
    sym_manual = _make_symbol(db_session, symbol="600501")
    sym_confirm = _make_symbol(db_session, symbol="600502")

    trade_date = datetime(2026, 1, 10, 12, 0, 0)
    _make_member(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym_auto.id,
        execution_mode=EXECUTION_AUTO,
        effective_from=trade_date - timedelta(days=5),
    )
    _make_member(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym_manual.id,
        execution_mode=EXECUTION_MANUAL,
        effective_from=trade_date - timedelta(days=5),
    )
    _make_member(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym_confirm.id,
        execution_mode=EXECUTION_CONFIRM,
        effective_from=trade_date - timedelta(days=5),
    )

    # only_auto=True：只返回 auto
    result_auto = get_effective_members_for_date(
        db_session, portfolio_id=pf.id, trade_date=trade_date, only_auto=True
    )
    assert len(result_auto) == 1
    assert result_auto[0].symbol_id == sym_auto.id
    assert result_auto[0].execution_mode == EXECUTION_AUTO

    # only_auto=False（默认）：返回全部 3 个
    result_all = get_effective_members_for_date(
        db_session, portfolio_id=pf.id, trade_date=trade_date, only_auto=False
    )
    assert len(result_all) == 3
    modes = {m.execution_mode for m in result_all}
    assert modes == {EXECUTION_AUTO, EXECUTION_MANUAL, EXECUTION_CONFIRM}


# ----------------------------------------------------------------------------
# 6. exclude_member_ids 过滤
# ----------------------------------------------------------------------------


def test_exclude_member_ids(db_session):
    """【WP7.1】exclude_member_ids 中的成员被排除。"""
    pf = _make_portfolio(db_session, name="QA-WP7-Exclude")
    sym_a = _make_symbol(db_session, symbol="600600")
    sym_b = _make_symbol(db_session, symbol="600601")
    sym_c = _make_symbol(db_session, symbol="600602")

    trade_date = datetime(2026, 1, 10, 12, 0, 0)
    m_a = _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_a.id,
        effective_from=trade_date - timedelta(days=5),
    )
    m_b = _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_b.id,
        effective_from=trade_date - timedelta(days=5),
    )
    m_c = _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_c.id,
        effective_from=trade_date - timedelta(days=5),
    )

    # 排除 m_a 和 m_c
    result = get_effective_members_for_date(
        db_session,
        portfolio_id=pf.id,
        trade_date=trade_date,
        exclude_member_ids=[m_a.id, m_c.id],
    )

    assert len(result) == 1
    assert result[0].id == m_b.id
    assert result[0].symbol_id == sym_b.id

    # 排除不存在的 id 不影响结果
    result_full = get_effective_members_for_date(
        db_session,
        portfolio_id=pf.id,
        trade_date=trade_date,
        exclude_member_ids=[999999],
    )
    assert len(result_full) == 3


# ----------------------------------------------------------------------------
# 7. 同一 trade_date 重复读取结果一致
# ----------------------------------------------------------------------------


def test_repeat_read_consistent(db_session):
    """【WP7.1】同一 trade_date 重复读取结果一致（除非数据被修改）。

    参照 spec line 264："同一 trade_date 重复读取结果一致（除非数据被修改）"。
    """
    pf = _make_portfolio(db_session, name="QA-WP7-Consistent")
    sym_a = _make_symbol(db_session, symbol="600700")
    sym_b = _make_symbol(db_session, symbol="600701")

    trade_date = datetime(2026, 1, 10, 12, 0, 0)
    _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_a.id,
        effective_from=trade_date - timedelta(days=5),
    )
    _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_b.id,
        effective_from=trade_date - timedelta(days=5),
    )

    r1 = get_effective_members_for_date(
        db_session, portfolio_id=pf.id, trade_date=trade_date
    )
    r2 = get_effective_members_for_date(
        db_session, portfolio_id=pf.id, trade_date=trade_date
    )

    assert [m.id for m in r1] == [m.id for m in r2]
    assert len(r1) == 2


# ----------------------------------------------------------------------------
# 8. get_member_symbols_for_date 返回去重 symbol_id 列表
# ----------------------------------------------------------------------------


def test_get_member_symbols_for_date(db_session):
    """【WP7.1】get_member_symbols_for_date 返回去重、排序的 symbol_id 列表。

    场景：同一标的有两条历史成员记录（一条已归档 + 一条当前有效），
    应只返回一次该 symbol_id。
    """
    pf = _make_portfolio(db_session, name="QA-WP7-Symbols")
    sym_a = _make_symbol(db_session, symbol="600800")
    sym_b = _make_symbol(db_session, symbol="600801")

    trade_date = datetime(2026, 1, 10, 12, 0, 0)

    # sym_a 有一条已归档（effective_to < trade_date，不应返回）+ 一条当前有效
    # 注意：partial unique index 仅约束 effective_to IS NULL 的记录，
    # 所以可以同时存在一条 effective_to 已设置 + 一条 effective_to IS NULL
    _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_a.id,
        effective_from=trade_date - timedelta(days=30),
        effective_to=trade_date - timedelta(days=20),  # 已归档
    )
    _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_a.id,
        effective_from=trade_date - timedelta(days=5),
        effective_to=None,  # 当前有效
    )
    _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_b.id,
        effective_from=trade_date - timedelta(days=5),
        effective_to=None,
    )

    result = get_member_symbols_for_date(
        db_session, portfolio_id=pf.id, trade_date=trade_date
    )

    # sym_a 当前有效成员 + sym_b 当前有效成员 → 2 个 symbol_id（去重）
    assert result == sorted([sym_a.id, sym_b.id])
    assert len(result) == 2
    # sym_a 不重复
    assert result.count(sym_a.id) == 1


# ----------------------------------------------------------------------------
# 9. validate_member_eligibility 全 auto 通过
# ----------------------------------------------------------------------------


def test_validate_member_eligibility_all_auto(db_session):
    """【WP7.1】全部 execution_mode='auto' 且 entry_rule_version_id 非空 → 通过。"""
    pf = _make_portfolio(db_session, name="QA-WP7-EligAllAuto")
    sym_a = _make_symbol(db_session, symbol="600900")
    sym_b = _make_symbol(db_session, symbol="600901")

    trade_date = datetime(2026, 1, 10, 12, 0, 0)
    m_a = _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_a.id,
        execution_mode=EXECUTION_AUTO,
        entry_rule_version_id=1001,
        effective_from=trade_date - timedelta(days=5),
    )
    m_b = _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_b.id,
        execution_mode=EXECUTION_AUTO,
        entry_rule_version_id=1002,
        effective_from=trade_date - timedelta(days=5),
    )

    is_valid, excluded = validate_member_eligibility([m_a, m_b])

    assert is_valid is True
    assert excluded == []


# ----------------------------------------------------------------------------
# 10. validate_member_eligibility 含 manual 返回排除清单
# ----------------------------------------------------------------------------


def test_validate_member_eligibility_with_manual(db_session):
    """【WP7.1】含 execution_mode='manual' 的成员 → 返回排除清单。

    排除清单每项包含 member_id / symbol_id / reason。
    """
    pf = _make_portfolio(db_session, name="QA-WP7-EligManual")
    sym_auto = _make_symbol(db_session, symbol="601000")
    sym_manual = _make_symbol(db_session, symbol="601001")

    trade_date = datetime(2026, 1, 10, 12, 0, 0)
    m_auto = _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_auto.id,
        execution_mode=EXECUTION_AUTO,
        entry_rule_version_id=1001,
        effective_from=trade_date - timedelta(days=5),
    )
    m_manual = _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_manual.id,
        execution_mode=EXECUTION_MANUAL,
        entry_rule_version_id=1002,
        effective_from=trade_date - timedelta(days=5),
    )

    is_valid, excluded = validate_member_eligibility([m_auto, m_manual])

    assert is_valid is False
    assert len(excluded) == 1
    assert excluded[0]["member_id"] == m_manual.id
    assert excluded[0]["symbol_id"] == sym_manual.id
    assert "execution_mode" in excluded[0]["reason"]
    assert EXECUTION_MANUAL in excluded[0]["reason"]


# ----------------------------------------------------------------------------
# 11. validate_member_eligibility 缺失 entry_rule_version_id 返回排除清单
# ----------------------------------------------------------------------------


def test_validate_member_eligibility_with_missing_rule(db_session):
    """【WP7.1】execution_mode='auto' 但 entry_rule_version_id=None → 排除。

    场景：auto 成员未配置入场规则版本，应被识别为不合格。
    """
    pf = _make_portfolio(db_session, name="QA-WP7-EligNoRule")
    sym_ok = _make_symbol(db_session, symbol="601100")
    sym_no_rule = _make_symbol(db_session, symbol="601101")

    trade_date = datetime(2026, 1, 10, 12, 0, 0)
    m_ok = _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_ok.id,
        execution_mode=EXECUTION_AUTO,
        entry_rule_version_id=2001,
        effective_from=trade_date - timedelta(days=5),
    )
    m_no_rule = _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_no_rule.id,
        execution_mode=EXECUTION_AUTO,
        entry_rule_version_id=None,  # 缺失入场规则版本
        effective_from=trade_date - timedelta(days=5),
    )

    is_valid, excluded = validate_member_eligibility([m_ok, m_no_rule])

    assert is_valid is False
    assert len(excluded) == 1
    assert excluded[0]["member_id"] == m_no_rule.id
    assert excluded[0]["symbol_id"] == sym_no_rule.id
    assert "entry_rule_version_id" in excluded[0]["reason"]


# ----------------------------------------------------------------------------
# 12. 边界：trade_date 正好等于 effective_from / effective_to
# ----------------------------------------------------------------------------


def test_boundary_dates_inclusive(db_session):
    """【WP7.1】effective_from <= trade_date AND effective_to >= trade_date 边界含等号。

    - 成员 A：effective_from = T（恰好等于 trade_date）→ 应返回
    - 成员 B：effective_to = T（恰好等于 trade_date）→ 应返回
    """
    pf = _make_portfolio(db_session, name="QA-WP7-Boundary")
    sym_a = _make_symbol(db_session, symbol="601200")
    sym_b = _make_symbol(db_session, symbol="601201")

    trade_date = datetime(2026, 1, 10, 12, 0, 0)

    # effective_from 恰好等于 trade_date
    _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_a.id,
        effective_from=trade_date,
        effective_to=None,
    )
    # effective_to 恰好等于 trade_date
    _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_b.id,
        effective_from=trade_date - timedelta(days=5),
        effective_to=trade_date,
    )

    result = get_effective_members_for_date(
        db_session, portfolio_id=pf.id, trade_date=trade_date
    )

    assert len(result) == 2
    returned_symbols = {m.symbol_id for m in result}
    assert returned_symbols == {sym_a.id, sym_b.id}


# ----------------------------------------------------------------------------
# 13. portfolio_id 隔离：不返回其他组合的成员
# ----------------------------------------------------------------------------


def test_portfolio_isolation(db_session):
    """【WP7.1】只返回指定 portfolio_id 的成员，不返回其他组合的成员。"""
    pf_a = _make_portfolio(db_session, name="QA-WP7-IsoA")
    pf_b = _make_portfolio(db_session, name="QA-WP7-IsoB")
    sym_shared = _make_symbol(db_session, symbol="601300")

    trade_date = datetime(2026, 1, 10, 12, 0, 0)
    # 同一标的在两个组合中都有效
    _make_member(
        db_session, portfolio_id=pf_a.id, symbol_id=sym_shared.id,
        effective_from=trade_date - timedelta(days=5),
    )
    _make_member(
        db_session, portfolio_id=pf_b.id, symbol_id=sym_shared.id,
        effective_from=trade_date - timedelta(days=5),
    )

    result_a = get_effective_members_for_date(
        db_session, portfolio_id=pf_a.id, trade_date=trade_date
    )
    result_b = get_effective_members_for_date(
        db_session, portfolio_id=pf_b.id, trade_date=trade_date
    )

    assert len(result_a) == 1
    assert result_a[0].portfolio_id == pf_a.id
    assert len(result_b) == 1
    assert result_b[0].portfolio_id == pf_b.id
    assert result_a[0].id != result_b[0].id


# ----------------------------------------------------------------------------
# 14. archived status 不返回（与 effective_to 无关）
# ----------------------------------------------------------------------------


def test_archived_status_excluded(db_session):
    """【WP7.1】status='archived' 的成员不返回，即使 effective_to 仍在窗口内。

    验证 status='active' 过滤的独立性。
    """
    pf = _make_portfolio(db_session, name="QA-WP7-StatusArchived")
    sym_archived = _make_symbol(db_session, symbol="601400")
    sym_active = _make_symbol(db_session, symbol="601401")

    trade_date = datetime(2026, 1, 10, 12, 0, 0)

    # status='archived' 但 effective_to 仍在窗口内（模拟数据不一致场景）
    _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_archived.id,
        status=STATUS_ARCHIVED,
        effective_from=trade_date - timedelta(days=10),
        effective_to=trade_date + timedelta(days=5),
    )
    _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_active.id,
        status=STATUS_ACTIVE,
        effective_from=trade_date - timedelta(days=5),
        effective_to=None,
    )

    result = get_effective_members_for_date(
        db_session, portfolio_id=pf.id, trade_date=trade_date
    )

    assert len(result) == 1
    assert result[0].symbol_id == sym_active.id
    assert result[0].status == STATUS_ACTIVE


# ============================================================================
# WP7.3 兼容与切换测试
# ============================================================================


# ----------------------------------------------------------------------------
# WP7.3 helpers
# ----------------------------------------------------------------------------


def _make_daily_bar(db_session, symbol_id: int, trade_date: date, close: float = 10.0):
    bar = DailyBar(
        symbol_id=symbol_id,
        trade_date=trade_date,
        open=close,
        high=close * 1.02,
        low=close * 0.98,
        close=close,
        volume=100000.0,
    )
    db_session.add(bar)
    db_session.commit()
    return bar


def _make_score(
    db_session,
    symbol_id: int,
    *,
    action: str = "open",
    stage: str = "start",
    trade_date: date = date(2026, 1, 5),
) -> Score:
    score = Score(
        symbol_id=symbol_id,
        trade_date=trade_date,
        quality_score=70.0,
        quality_grade="B",
        timing_score=65.0,
        stage=stage,
        action=action,
        priority_score=75.0,
        weight_mode="manual",
    )
    db_session.add(score)
    db_session.commit()
    db_session.refresh(score)
    return score


def _make_active_rule(db_session, portfolio_id: int) -> PortfolioRule:
    rule = PortfolioRule(
        portfolio_id=portfolio_id,
        rule_name="QA-WP73-Rule",
        max_single_position_pct=0.2,
        max_sector_position_pct=0.4,
        max_stock_position_pct=0.6,
        max_etf_position_pct=0.3,
        max_loss_per_trade_pct=0.02,
        max_open_positions=10,
        stage_limits_json=json.dumps({
            "stock": {"start": 0.2, "pullback": 0.15, "breakout": 0.1},
            "etf": {"start": 0.2, "pullback": 0.15, "breakout": 0.1},
        }),
        is_active=1,
    )
    db_session.add(rule)
    db_session.commit()
    db_session.refresh(rule)
    return rule


def _make_position(
    db_session,
    portfolio_id: int,
    symbol_id: int,
    *,
    quantity: float = 100,
    avg_cost: float = 10.0,
    latest_price: float = 10.0,
) -> Position:
    pos = Position(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        quantity=quantity,
        avg_cost=avg_cost,
        latest_price=latest_price,
        market_value=quantity * latest_price,
        position_pct=round(quantity * latest_price / 100000.0, 4),
        asset_type="stock",
    )
    db_session.add(pos)
    db_session.commit()
    db_session.refresh(pos)
    return pos


def _make_scan_run_with_candidate(
    db_session, portfolio_id: int, symbol_id: int,
) -> tuple[ScanRun, ScanResult]:
    run = ScanRun(
        portfolio_id=portfolio_id,
        run_name=f"QA-WP73-Scan-{datetime.now().strftime('%H%M%S%f')}",
        scope_snapshot="cn-stock",
        status="done",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    result = ScanResult(
        scan_run_id=run.id,
        symbol_id=symbol_id,
        result_type="executable",
        rank_no=1,
        quality_score=70.0,
        timing_score=65.0,
        priority_score=80.0,
        stage="start",
        action="open",
        recommended_position_pct=0.2,
    )
    db_session.add(result)
    db_session.commit()
    db_session.refresh(result)
    return run, result


@pytest.fixture
def member_source_enabled():
    """临时开启 PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED，测试结束后恢复原值。

    settings 是单例，必须确保恢复，避免污染同进程后续测试。
    """
    original = settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED
    settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = True
    try:
        yield
    finally:
        settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = original


@pytest.fixture
def member_source_disabled():
    """临时关闭 PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED，测试结束后恢复原值。"""
    original = settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED
    settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = False
    try:
        yield
    finally:
        settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = original


# ----------------------------------------------------------------------------
# 15. run_portfolio_backtest 在 member 来源下填充 WP7.2 全部快照字段
# ----------------------------------------------------------------------------


def test_run_backtest_fills_snapshot_fields_when_member_source(db_session, member_source_enabled):
    """【WP7.3】member 来源回测时填充 WP7.2 全部 9 个快照字段。

    场景：
    - portfolio 开启 auto_trade，有 active rule
    - 2 个 auto 成员，规则版本齐全
    - 成员窗口覆盖回测窗口
    - 期望：BacktestRun 的 9 个快照字段全部非 None
    """
    pf = _make_portfolio(db_session, name="QA-WP73-Snapshot-Member")
    sym_a = _make_symbol(db_session, symbol="700010")
    sym_b = _make_symbol(db_session, symbol="700011")
    rule = _make_active_rule(db_session, pf.id)

    trade_date = datetime(2026, 1, 10, 12, 0, 0)
    m_a = _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_a.id,
        execution_mode=EXECUTION_AUTO,
        entry_rule_version_id=1001, exit_rule_version_id=2001,
        effective_from=trade_date - timedelta(days=30),
    )
    m_b = _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_b.id,
        execution_mode=EXECUTION_AUTO,
        entry_rule_version_id=1002, exit_rule_version_id=2002,
        effective_from=trade_date - timedelta(days=30),
    )

    # 造 3 天行情，确保回测能跑通
    for i, d in enumerate([date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]):
        _make_daily_bar(db_session, sym_a.id, d, close=10.0 + i * 0.2)
        _make_daily_bar(db_session, sym_b.id, d, close=20.0 + i * 0.2)
    _make_score(db_session, sym_a.id, action="open", trade_date=date(2026, 1, 5))
    _make_score(db_session, sym_b.id, action="open", trade_date=date(2026, 1, 5))

    result = run_portfolio_backtest(
        db_session, portfolio_id=pf.id,
        start_date=date(2026, 1, 5), end_date=date(2026, 1, 7),
    )

    assert result["symbol_source"] == "members"
    assert result["source_type"] == "member"

    run = db_session.get(BacktestRun, result["run_id"])
    assert run is not None

    # 9 个快照字段应全部非 None
    assert run.engine_name == "event_driven"
    assert run.engine_version == "1.0.0"
    assert run.source_type == "member"
    assert run.member_snapshot_json is not None
    assert run.symbol_ids_json is not None
    assert run.excluded_members_json is not None
    assert run.portfolio_rule_version_id == rule.id
    assert run.score_mode is not None
    assert run.data_cutoff_at is not None

    # 验证快照内容
    member_snapshot = json.loads(run.member_snapshot_json)
    assert len(member_snapshot) == 2
    member_ids = {item["member_id"] for item in member_snapshot}
    assert member_ids == {m_a.id, m_b.id}

    symbol_ids = json.loads(run.symbol_ids_json)
    assert sorted(symbol_ids) == sorted([sym_a.id, sym_b.id])

    excluded = json.loads(run.excluded_members_json)
    assert excluded == []  # 全 auto，无排除

    # data_cutoff_at 应为回测窗口内最新数据日期
    assert run.data_cutoff_at.date() == date(2026, 1, 7)


# ----------------------------------------------------------------------------
# 16. run_portfolio_backtest 在 legacy 来源下仅填充最小快照集
# ----------------------------------------------------------------------------


def test_run_backtest_legacy_source_fills_minimal_snapshot(db_session, member_source_disabled):
    """【WP7.3】legacy 来源回测仅填充 engine_name/version/source_type，其它快照字段为 None。

    场景：
    - PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED=False（默认）
    - 通过持仓 + scan 候选推导 symbol_ids
    - 期望：engine_name/version/source_type 填充，member_snapshot 等为 None
    """
    pf = _make_portfolio(db_session, name="QA-WP73-Snapshot-Legacy")
    sym = _make_symbol(db_session, symbol="700020")
    _make_active_rule(db_session, pf.id)
    _make_position(db_session, pf.id, sym.id, quantity=100)

    for i, d in enumerate([date(2026, 1, 5), date(2026, 1, 6)]):
        _make_daily_bar(db_session, sym.id, d, close=10.0 + i * 0.2)

    result = run_portfolio_backtest(
        db_session, portfolio_id=pf.id,
        start_date=date(2026, 1, 5), end_date=date(2026, 1, 6),
    )

    assert result["symbol_source"] == "legacy"
    assert result["source_type"] == "legacy_scan"

    run = db_session.get(BacktestRun, result["run_id"])
    assert run is not None

    # 最小快照集：仅 engine_name/version/source_type
    assert run.engine_name == "event_driven"
    assert run.engine_version == "1.0.0"
    assert run.source_type == "legacy_scan"

    # 其它快照字段应为 None（历史回测行为不变）
    assert run.member_snapshot_json is None
    assert run.symbol_ids_json is None
    assert run.excluded_members_json is None
    assert run.portfolio_rule_version_id is None
    assert run.score_mode is None
    assert run.data_cutoff_at is None


# ----------------------------------------------------------------------------
# 17. 存在 manual/confirm 成员时默认阻止完整回测
# ----------------------------------------------------------------------------


def test_run_backtest_blocks_when_manual_members_exist(db_session, member_source_enabled):
    """【WP7.3】member 来源 + only_auto=False + 存在 manual 成员 → 抛 ValueError。

    错误消息应明确提示用户选择 only_auto 或调整执行模式。
    """
    pf = _make_portfolio(db_session, name="QA-WP73-Block-Manual")
    sym_auto = _make_symbol(db_session, symbol="700030")
    sym_manual = _make_symbol(db_session, symbol="700031")
    _make_active_rule(db_session, pf.id)

    trade_date = datetime(2026, 1, 10, 12, 0, 0)
    _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_auto.id,
        execution_mode=EXECUTION_AUTO,
        entry_rule_version_id=1001,
        effective_from=trade_date - timedelta(days=30),
    )
    _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_manual.id,
        execution_mode=EXECUTION_MANUAL,
        entry_rule_version_id=1002,
        effective_from=trade_date - timedelta(days=30),
    )

    # 造行情确保不是因无数据报错
    for d in [date(2026, 1, 5), date(2026, 1, 6)]:
        _make_daily_bar(db_session, sym_auto.id, d, close=10.0)
        _make_daily_bar(db_session, sym_manual.id, d, close=10.0)

    # 默认 only_auto=False → 应阻止
    with pytest.raises(ValueError, match="manual/confirm 成员"):
        run_portfolio_backtest(
            db_session, portfolio_id=pf.id,
            start_date=date(2026, 1, 5), end_date=date(2026, 1, 6),
        )

    # 不应创建任何 BacktestRun
    runs = db_session.query(BacktestRun).filter_by(portfolio_id=pf.id).all()
    assert len(runs) == 0


# ----------------------------------------------------------------------------
# 18. only_auto=True 跳过 manual 成员并记录 excluded_members
# ----------------------------------------------------------------------------


def test_run_backtest_only_auto_option_skips_manual_members(db_session, member_source_enabled):
    """【WP7.3】member 来源 + only_auto=True → 跳过 manual 成员，excluded_members_json 记录原因。

    场景：
    - 1 个 auto 成员 + 1 个 manual 成员
    - only_auto=True
    - 期望：回测完成，symbol_ids 仅含 auto 成员标的，
      excluded_members_json 含 manual 成员及 reason
    """
    pf = _make_portfolio(db_session, name="QA-WP73-OnlyAuto-Skip")
    sym_auto = _make_symbol(db_session, symbol="700040")
    sym_manual = _make_symbol(db_session, symbol="700041")
    _make_active_rule(db_session, pf.id)

    trade_date = datetime(2026, 1, 10, 12, 0, 0)
    m_auto = _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_auto.id,
        execution_mode=EXECUTION_AUTO,
        entry_rule_version_id=1001,
        effective_from=trade_date - timedelta(days=30),
    )
    m_manual = _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_manual.id,
        execution_mode=EXECUTION_MANUAL,
        entry_rule_version_id=1002,
        effective_from=trade_date - timedelta(days=30),
    )

    # 造行情
    for i, d in enumerate([date(2026, 1, 5), date(2026, 1, 6)]):
        _make_daily_bar(db_session, sym_auto.id, d, close=10.0 + i * 0.2)
        _make_daily_bar(db_session, sym_manual.id, d, close=20.0 + i * 0.2)
    _make_score(db_session, sym_auto.id, action="open", trade_date=date(2026, 1, 5))

    result = run_portfolio_backtest(
        db_session, portfolio_id=pf.id,
        start_date=date(2026, 1, 5), end_date=date(2026, 1, 6),
        only_auto=True,
    )

    assert result["status"] == "completed"
    assert result["symbol_source"] == "members"
    assert result["symbol_ids"] == [sym_auto.id]  # 仅 auto 成员标的
    assert result["excluded_member_count"] == 1

    run = db_session.get(BacktestRun, result["run_id"])
    assert run is not None

    # excluded_members_json 应记录 manual 成员
    excluded = json.loads(run.excluded_members_json)
    assert len(excluded) == 1
    assert excluded[0]["member_id"] == m_manual.id
    assert excluded[0]["symbol_id"] == sym_manual.id
    assert "execution_mode" in excluded[0]["reason"]

    # member_snapshot_json 应仅含 auto 成员
    member_snapshot = json.loads(run.member_snapshot_json)
    assert len(member_snapshot) == 1
    assert member_snapshot[0]["member_id"] == m_auto.id
    assert member_snapshot[0]["execution_mode"] == EXECUTION_AUTO


# ----------------------------------------------------------------------------
# 19. compare_new_old_engine 返回新旧引擎差异
# ----------------------------------------------------------------------------


def test_compare_new_old_engine_returns_diff(db_session, member_source_disabled):
    """【WP7.3】compare_new_old_engine 同时跑新旧两套来源并返回 diff。

    场景：
    - legacy 来源：通过持仓推导 sym_a
    - member 来源：通过成员推导 sym_a + sym_b
    - 期望：diff.symbol_ids_added == [sym_b.id]，explanation 含"多了 1 个标的"
    """
    pf = _make_portfolio(db_session, name="QA-WP73-Compare")
    sym_a = _make_symbol(db_session, symbol="700050")
    sym_b = _make_symbol(db_session, symbol="700051")
    _make_active_rule(db_session, pf.id)

    # legacy 来源：持仓 sym_a
    _make_position(db_session, pf.id, sym_a.id, quantity=100)

    # member 来源：成员 sym_a + sym_b（都 auto）
    trade_date = datetime(2026, 1, 10, 12, 0, 0)
    _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_a.id,
        execution_mode=EXECUTION_AUTO,
        entry_rule_version_id=1001,
        effective_from=trade_date - timedelta(days=30),
    )
    _make_member(
        db_session, portfolio_id=pf.id, symbol_id=sym_b.id,
        execution_mode=EXECUTION_AUTO,
        entry_rule_version_id=1002,
        effective_from=trade_date - timedelta(days=30),
    )

    # 造行情（两个标的都要有，否则 member 来源回测会因 sym_b 无数据失败）
    for i, d in enumerate([date(2026, 1, 5), date(2026, 1, 6)]):
        _make_daily_bar(db_session, sym_a.id, d, close=10.0 + i * 0.2)
        _make_daily_bar(db_session, sym_b.id, d, close=20.0 + i * 0.2)
    _make_score(db_session, sym_a.id, action="open", trade_date=date(2026, 1, 5))
    _make_score(db_session, sym_b.id, action="open", trade_date=date(2026, 1, 5))

    result = compare_new_old_engine(
        db_session, portfolio_id=pf.id,
        start_date=date(2026, 1, 5), end_date=date(2026, 1, 6),
    )

    # 结构验证
    assert set(result.keys()) == {"old", "new", "diff"}
    assert "symbol_ids" in result["old"]
    assert "metrics" in result["old"]
    assert "trades" in result["old"]
    assert "symbol_ids" in result["new"]
    assert "metrics" in result["new"]
    assert "trades" in result["new"]

    # 来源标签验证
    assert result["old"]["source_type"] == "legacy_scan"
    assert result["new"]["source_type"] == "member"

    # 标的集差异：new 应比 old 多 sym_b
    assert sym_a.id in result["old"]["symbol_ids"]
    assert sym_b.id not in result["old"]["symbol_ids"]
    assert sym_a.id in result["new"]["symbol_ids"]
    assert sym_b.id in result["new"]["symbol_ids"]

    assert result["diff"]["symbol_ids_added"] == [sym_b.id]
    assert result["diff"]["symbol_ids_removed"] == []

    # 指标 diff 应包含 6 个指标
    metrics_diff = result["diff"]["metrics_diff"]
    for key in ("total_return", "total_return_pct", "max_drawdown",
                "max_drawdown_pct", "sharpe_ratio", "trade_count"):
        assert key in metrics_diff
        assert "old" in metrics_diff[key]
        assert "new" in metrics_diff[key]
        assert "delta" in metrics_diff[key]

    # 文本解释应提及标的集差异
    explanation = result["diff"]["explanation"]
    assert "多了 1 个标的" in explanation

    # 开关应已恢复（finally 块）
    assert settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED is False


# ----------------------------------------------------------------------------
# 20. 切换功能开关后历史 legacy 回测仍可读
# ----------------------------------------------------------------------------


def test_legacy_backtest_still_readable_after_switch_off(db_session, member_source_disabled):
    """【WP7.3】切换开关后，历史 legacy BacktestRun（无快照字段）仍可通过 DB 查询读取。

    场景：
    - 创建一个无快照字段的 BacktestRun（模拟切换前的历史回测）
    - 切换开关到任意状态
    - 验证 db_session.get 仍能读出原始记录，快照字段全为 None
    """
    pf = _make_portfolio(db_session, name="QA-WP73-LegacyReadable")

    run = BacktestRun(
        portfolio_id=pf.id,
        run_name="legacy-history-run",
        symbols_json=json.dumps([1, 2, 3]),
        rule_config_json=json.dumps({"buy_conditions": {}}),
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
        initial_capital=100000.0,
        status="completed",
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    original_id = run.id

    # 切换开关到开启状态（模拟切换后场景）
    settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = True
    try:
        reloaded = db_session.get(BacktestRun, original_id)
        assert reloaded is not None
        assert reloaded.run_name == "legacy-history-run"
        assert reloaded.status == "completed"
        # 历史回测的快照字段应保持 None（未被回填）
        assert reloaded.member_snapshot_json is None
        assert reloaded.symbol_ids_json is None
        assert reloaded.excluded_members_json is None
        assert reloaded.portfolio_rule_version_id is None
        assert reloaded.score_mode is None
        assert reloaded.data_cutoff_at is None
        assert reloaded.engine_name is None
        assert reloaded.engine_version is None
        assert reloaded.source_type is None
    finally:
        settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = False


# ----------------------------------------------------------------------------
# 21. 切换功能开关后历史 member 来源回测仍可读（快照已落盘）
# ----------------------------------------------------------------------------


def test_historical_backtest_with_member_source_still_readable(db_session, member_source_disabled):
    """【WP7.3】切换开关关闭后，历史 member 来源 BacktestRun（含快照）仍可读。

    场景：
    - 创建一个 source_type='member' 的 BacktestRun（模拟切换前的 member 来源回测）
    - 快照字段已落盘（member_snapshot_json / symbol_ids_json 等）
    - 切换开关到关闭状态
    - 验证 db_session.get 仍能读出原始记录，快照字段保持原值
    """
    pf = _make_portfolio(db_session, name="QA-WP73-MemberHistoryReadable")
    sym = _make_symbol(db_session, symbol="700060")

    # 模拟切换前已落盘的 member 来源回测
    member_snapshot_data = [
        {
            "member_id": 999,
            "symbol_id": sym.id,
            "effective_from": "2026-01-01T00:00:00",
            "effective_to": None,
            "execution_mode": "auto",
            "entry_rule_version_id": 1001,
            "exit_rule_version_id": 2001,
        }
    ]
    excluded_data = []
    cost_data = {
        "commission_rate": None,
        "stamp_duty_rate": None,
        "slippage": None,
        "min_commission": None,
    }

    run = BacktestRun(
        portfolio_id=pf.id,
        run_name="member-history-run",
        symbols_json=json.dumps([sym.id]),
        rule_config_json=json.dumps({"buy_conditions": {}}),
        cost_config_json=json.dumps(cost_data),
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
        initial_capital=100000.0,
        status="completed",
        # WP7.2/WP7.3 快照字段
        member_snapshot_json=json.dumps(member_snapshot_data, ensure_ascii=False),
        symbol_ids_json=json.dumps([sym.id]),
        excluded_members_json=json.dumps(excluded_data),
        portfolio_rule_version_id=42,
        score_mode="auto_trade_signal",
        data_cutoff_at=datetime(2026, 1, 31),
        engine_name="event_driven",
        engine_version="1.0.0",
        source_type="member",
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    original_id = run.id

    # 验证开关已关闭（默认状态）
    assert settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED is False

    # 重新查询：历史 member 来源回测应仍可读，快照字段保持原值
    reloaded = db_session.get(BacktestRun, original_id)
    assert reloaded is not None
    assert reloaded.run_name == "member-history-run"
    assert reloaded.source_type == "member"
    assert reloaded.engine_name == "event_driven"
    assert reloaded.engine_version == "1.0.0"
    assert reloaded.portfolio_rule_version_id == 42
    assert reloaded.score_mode == "auto_trade_signal"

    # JSON 字段按原值回读
    assert json.loads(reloaded.symbol_ids_json) == [sym.id]
    assert json.loads(reloaded.member_snapshot_json) == member_snapshot_data
    assert json.loads(reloaded.excluded_members_json) == excluded_data
    assert json.loads(reloaded.cost_config_json) == cost_data

    # 即使当前开关关闭，历史 member 来源记录的 source_type 仍为 'member'，
    # UI 可据此正确展示"使用历史成员集"标签
