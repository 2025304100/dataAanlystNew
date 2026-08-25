"""T-A3: Q5.3 关键成员缺 Score 直接阻断。

Checklist:
- T-A3-C1: 关键成员阻断优先于整体覆盖率（整体 ≥95% 但 key_members 中任一缺任意交易日 → REJECTED）
- T-A3-C2: key_members_json 空/NULL → 默认全组合关键，回落 estimate_score_coverage_window level
- T-A3-C3: JSON 序列化/反序列化（Snapshot 创建 key_members_json=[1..5]，DB round-trip 后 list[int] 完全相等）
"""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest


def _make_symbol(db, sym_code: str, name: str = "测试股") -> int:
    from app.models.symbol import Symbol
    s = Symbol(symbol=sym_code, name=name, market="SH", asset_type="stock")
    db.add(s)
    db.flush()
    return s.id


def _make_portfolio(db, **kw) -> int:
    from app.models.portfolio import Portfolio
    p = Portfolio(
        name=kw.pop("name", "T_A3 组合"),
        account_type=kw.pop("account_type", "sim"),
        asset_scope=kw.pop("asset_scope", "stock"),
        total_capital=kw.pop("total_capital", 1_000_000.0),
        investable_ratio=kw.pop("investable_ratio", 1.0),
        cash_reserve_ratio=kw.pop("cash_reserve_ratio", 0.05),
        currency=kw.pop("currency", "CNY"),
        is_default=kw.pop("is_default", 0),
        buy_fee_pct=kw.pop("buy_fee_pct", 0.00025),
        sell_fee_pct=kw.pop("sell_fee_pct", 0.00025),
        benchmark_code=kw.pop("benchmark_code", "000300"),
        default_single_position_pct=kw.pop("default_single_position_pct", 0.3),
        auto_trade_enabled=kw.pop("auto_trade_enabled", 0),
    )
    for k, v in kw.items():
        setattr(p, k, v)
    db.add(p)
    db.flush()
    return p.id


def _insert_index_trade_dates(db, dates: list[date]) -> None:
    from app.models.index_price import IndexPrice
    for d in dates:
        ip = IndexPrice(
            symbol="000300",
            trade_date=d,
            open=4000.0,
            high=4100.0,
            low=3900.0,
            close=4050.0,
            volume=1_000_000_000.0,
            amount=500_000_000_000.0,
        )
        db.add(ip)
    db.flush()


def _insert_score(
    db,
    *,
    fmr_id: str,
    symbol_id: int,
    trade_date: date,
    cutoff_utc: datetime | None = None,
    score_value: float = 0.5,
) -> None:
    from app.models.score import Score
    s = Score(
        factor_model_run_id=fmr_id,
        symbol_id=symbol_id,
        trade_date=trade_date,
        factor_data_cutoff_at=cutoff_utc,
        factor_set_id=None,
        weight_mode="ridge",
        priority_score=score_value,
        quality_score=score_value,
        timing_score=score_value,
        quality_grade="A",
        stage="growth",
        action="HOLD",
        model_alpha_score=score_value,
        calc_batch_id="batch_T_A3",
        published_at=cutoff_utc,
        pit_flag="PIT_SAFE" if cutoff_utc else "NOT_CHECKED",
    )
    db.add(s)


def _insert_snapshot(
    db,
    *,
    snapshot_id: str,
    portfolio_id: int,
    symbol_ids: list[int],
    created_at: datetime,
    key_members_json: list[int] | None = None,
) -> None:
    from app.models.decision_engine import StrategyExecutionSnapshot
    members_json = json.dumps([{"symbol_id": sid} for sid in symbol_ids])
    km_json_str = json.dumps(key_members_json) if key_members_json is not None else None
    s = StrategyExecutionSnapshot(
        id=snapshot_id,
        portfolio_id=portfolio_id,
        decision_clock_json="{}",
        member_snapshot_json=members_json,
        key_members_json=km_json_str,
        universe_type="portfolio_members",
        snapshot_type="save_and_apply",
        snapshot_hash="a" * 64,
        idempotency_key=f"idemp_{snapshot_id}",
        effective_from=created_at,
        created_at=created_at,
    )
    db.add(s)
    db.flush()


# ───────────────────────────────────── fixtures ─────────────────────────────────────


@pytest.fixture()
def portfolio_100_members_2days(db_session):
    """100 只成员组合 + 2 个交易日。

    Score 覆盖率：98% (196/200)，但 key_members=[101,102] 中 102 缺 2 日 Score →
    整体覆盖 ≥95% 但关键成员阻断。
    """
    db = db_session
    pid = _make_portfolio(db, name="T_A3_C1_Block_Over_Coverage")
    # 前 100 只 symbol_id 作为组合成员
    symbol_ids = [_make_symbol(db, f"T{i:06d}", f"测试股{i}") for i in range(100)]
    # symbol_id 101, 102 单独创建（不在组合内），作为指定的 key_members
    km_101 = _make_symbol(db, "KM00101", "关键成员101")
    km_102 = _make_symbol(db, "KM00102", "关键成员102")
    created_at = datetime(2025, 1, 2, 12, 0, 0)
    # 注意：为了 key_members 有效，101/102 必须在 member_snapshot 内？
    # 需求：key_members 是 symbol_id 列表，可来自组合外，但其 Score 仍应被查。
    # 为测试方便，把 km_101/km_102 也放进 snapshot 成员里（保证缺的仅是 Score 不是成员）
    all_symbol_ids = symbol_ids + [km_101, km_102]
    _insert_snapshot(
        db,
        snapshot_id="snap_T_A3_C1",
        portfolio_id=pid,
        symbol_ids=all_symbol_ids,
        created_at=created_at,
    )
    trade_dates = [date(2025, 1, 3), date(2025, 1, 6)]
    _insert_index_trade_dates(db, trade_dates)
    fmr = "fmr_T_A3_C1"
    cutoff = datetime(2025, 1, 6, 7, 0, 0)
    # ── 插 Score：98% 覆盖率（前 98 只成员两天都有，后 2 只没） ──
    for sid in symbol_ids[:98]:
        for td in trade_dates:
            _insert_score(db, fmr_id=fmr, symbol_id=sid, trade_date=td, cutoff_utc=cutoff)
    # 关键成员 101：2 天都有 Score
    for td in trade_dates:
        _insert_score(db, fmr_id=fmr, symbol_id=km_101, trade_date=td, cutoff_utc=cutoff)
    # 关键成员 102：2 天都缺 Score（不插）
    db.commit()
    return {
        "portfolio_id": pid,
        "symbol_ids_100": symbol_ids,
        "km_101": km_101,
        "km_102": km_102,
        "trade_dates": trade_dates,
        "fmr_id": fmr,
        "cutoff": cutoff,
        "all_symbol_ids": all_symbol_ids,
    }


@pytest.fixture()
def portfolio_default_fallback(db_session):
    """key_members_json 全空场景（snapshot+portfolio 都是 NULL），应回落全组合。"""
    db = db_session
    pid = _make_portfolio(db, name="T_A3_C2_DefaultFallback")
    symbol_ids = [_make_symbol(db, f"D{i:06d}", f"回落股{i}") for i in range(10)]
    created_at = datetime(2025, 1, 2, 12, 0, 0)
    _insert_snapshot(
        db,
        snapshot_id="snap_T_A3_C2",
        portfolio_id=pid,
        symbol_ids=symbol_ids,
        created_at=created_at,
        key_members_json=None,
    )
    trade_dates = [date(2025, 1, 3)]
    _insert_index_trade_dates(db, trade_dates)
    fmr = "fmr_T_A3_C2"
    cutoff = datetime(2025, 1, 3, 7, 0, 0)
    # 8/10 只有 → 覆盖 80% → 关键成员全缺 20% → FAIL
    for sid in symbol_ids[:8]:
        _insert_score(db, fmr_id=fmr, symbol_id=sid, trade_date=trade_dates[0], cutoff_utc=cutoff)
    db.commit()
    return {
        "portfolio_id": pid,
        "symbol_ids": symbol_ids,
        "trade_dates": trade_dates,
        "fmr_id": fmr,
        "cutoff": cutoff,
    }


@pytest.fixture()
def portfolio_for_roundtrip(db_session):
    """C3: JSON 序列化/反序列化 round-trip。"""
    db = db_session
    pid = _make_portfolio(db, name="T_A3_C3_RoundTrip")
    symbol_ids = [_make_symbol(db, f"R{i:06d}", f"RT股{i}") for i in range(5)]
    return {
        "db": db,
        "portfolio_id": pid,
        "symbol_ids": symbol_ids,
    }


# ───────────────────────────────────── tests ─────────────────────────────────────


class TestT_A3_KeyMembers:

    def test_c1_block_over_coverage(self, db_session, portfolio_100_members_2days):
        """T-A3-C1: 关键成员阻断优先于整体覆盖率。

        整体覆盖率 (98*2 + 2) / (102*2) = 198/204 ≈ 97% ≥95% 应 PASS；
        但 key_members=[101,102] 中 102 缺 2 日 Score → REJECTED + KEY_MEMBER_SCORE_MISSING。
        """
        from app.services.score_query_service import (
            check_key_member_score_coverage,
            estimate_score_coverage_window,
        )
        data = portfolio_100_members_2days
        pid = data["portfolio_id"]
        tds = data["trade_dates"]
        fmr = data["fmr_id"]
        km101 = data["km_101"]
        km102 = data["km_102"]
        all_sids = data["all_symbol_ids"]

        # ── 基线 A：确认整体覆盖率 ≥95%（estimate_score_coverage_window PASS） ──
        cov = estimate_score_coverage_window(
            db_session,
            portfolio_id=pid,
            start_date=tds[0],
            end_date=tds[-1],
            bound_factor_model_run_id=fmr,
        )
        # 102 成员 × 2 交易日 = 204；实际 = 98*2 + 101 的 2 条 = 198
        assert cov["member_count_day_count_expected_denominator"] == len(all_sids) * len(tds)
        # 整体覆盖 ≥ 95%
        assert cov["coverage_rate"] >= 0.95, (
            f"测试前提：整体覆盖率应 ≥95% 才能验证“阻断优先”，实际 {cov['coverage_rate']}"
        )
        assert cov["coverage_level"] == "PASS"

        # ── 基线 B：关键成员指定（显式传）──
        r = check_key_member_score_coverage(
            db_session,
            portfolio_id=pid,
            start_date=tds[0],
            end_date=tds[-1],
            bound_factor_model_run_id=fmr,
            strategy_snapshot_key_members_json=[km101, km102],
        )
        assert r["level"] == "REJECTED", (
            f"key_members 中 {km102} 缺 2 日 Score，应 REJECTED，实际 {r['level']}"
        )
        assert r["block_code"] == "KEY_MEMBER_SCORE_MISSING"
        # block_reason 可读，含关键信息
        assert r["block_reason"] is not None
        assert "关键成员缺 Score" in r["block_reason"]
        assert f"symbol_id={km102}" in r["block_reason"]
        assert "缺 2 交易日" in r["block_reason"]

        # 生效的 key_members 顺序一致
        assert r["key_members_effective"] == [km101, km102]
        # 期望 × 实际计数
        assert r["key_members_total_expected_coverage_count"] == 2 * len(tds)  # 2 members × 2 days
        # 实际 = km101 有 2 条 + km102 有 0 条 = 2
        assert r["key_members_actual_found_count"] == 2

        # per_key_member_missing_detail 粒度正确
        detail_by_sym = {d["symbol_id"]: d for d in r["per_key_member_missing_detail"]}
        assert detail_by_sym[km101]["missing_count"] == 0
        assert detail_by_sym[km102]["missing_count"] == 2
        assert len(detail_by_sym[km102]["missing_dates"]) == 2

    def test_c2_default_all_members_when_null(self, db_session, portfolio_default_fallback):
        """T-A3-C2: key_members_json NULL/空 → 默认全组合关键。

        场景：snapshot+portfolio key_members_json 都 NULL。
        组合 10 只 × 1 日 = 分母 10，实际 Score = 8 → 缺 2 只。
        key_members_effective 应 = 全 10 只 member_ids；并因其中 2 只缺 → REJECTED。
        """
        from app.services.score_query_service import (
            check_key_member_score_coverage,
            estimate_score_coverage_window,
        )
        data = portfolio_default_fallback
        pid = data["portfolio_id"]
        sym_ids = data["symbol_ids"]
        tds = data["trade_dates"]
        fmr = data["fmr_id"]

        # 先确认 estimate_score_coverage_window 用的 member_ids_used
        cov = estimate_score_coverage_window(
            db_session,
            portfolio_id=pid,
            start_date=tds[0],
            end_date=tds[-1],
            bound_factor_model_run_id=fmr,
        )
        expected_ids = cov["member_ids_used"]
        assert expected_ids == sym_ids, "estimate 成员列表应等于 snapshot 内 10 只"

        # key_members 全空 → 回落全组合
        r = check_key_member_score_coverage(
            db_session,
            portfolio_id=pid,
            start_date=tds[0],
            end_date=tds[-1],
            bound_factor_model_run_id=fmr,
            strategy_snapshot_key_members_json=None,
            portfolio_key_members_json=None,
        )
        assert r["key_members_effective"] == sym_ids, (
            f"key_members_json NULL 时应回落到全组合 {sym_ids}，实际 {r['key_members_effective']}"
        )
        # 组合最后 2 只没 Score → REJECTED
        assert r["level"] == "REJECTED"
        assert r["block_code"] == "KEY_MEMBER_SCORE_MISSING"
        # 缺的是最后 2 只
        missing_syms = sorted([
            d["symbol_id"] for d in r["per_key_member_missing_detail"] if d["missing_count"] > 0
        ])
        assert missing_syms == sorted(sym_ids[-2:])

    def test_c3_json_roundtrip_snapshot(self, db_session, portfolio_for_roundtrip):
        """T-A3-C3: JSON 序列化/反序列化。

        Snapshot 创建时 key_members_json=[1,2,3,4,5]（真实 symbol_id），
        DB round-trip 读回后顺序一致（list[int] 完全相等）。
        """
        data = portfolio_for_roundtrip
        db = db_session
        pid = data["portfolio_id"]
        sym_ids = data["symbol_ids"]  # 真实存在的 5 个 symbol_id 顺序
        created_at = datetime(2025, 1, 2, 12, 0, 0)
        snap_id = "snap_T_A3_C3_RT"
        # 写：key_members_json = [sym_ids ...]（保序）
        _insert_snapshot(
            db,
            snapshot_id=snap_id,
            portfolio_id=pid,
            symbol_ids=sym_ids,
            created_at=created_at,
            key_members_json=list(sym_ids),  # [1..5] 顺序
        )
        # 同时写 Portfolio 级
        from app.models.portfolio import Portfolio
        pf = db.get(Portfolio, pid)
        assert pf is not None
        pf.key_members_json = json.dumps(list(sym_ids))
        db.commit()

        # ── 读 1：StrategyExecutionSnapshot 直接 ORM round-trip ──
        from app.models.decision_engine import StrategyExecutionSnapshot
        from app.services.score_query_service import _parse_key_members_from_json

        snap = db.get(StrategyExecutionSnapshot, snap_id)
        assert snap is not None
        # ORM 读到的是原始 Text 字符串 → 解析
        parsed_snap_km = _parse_key_members_from_json(snap.key_members_json)
        assert isinstance(parsed_snap_km, list), f"应是 list，实际 {type(parsed_snap_km)}"
        assert all(isinstance(x, int) for x in parsed_snap_km), "所有元素应为 int"
        assert parsed_snap_km == list(sym_ids), (
            f"Snapshot round-trip 顺序/内容不一致：期望 {list(sym_ids)}，实际 {parsed_snap_km}"
        )

        # ── 读 2：Portfolio 直接 ORM round-trip ──
        pf2 = db.get(Portfolio, pid)
        assert pf2 is not None
        parsed_pf_km = _parse_key_members_from_json(pf2.key_members_json)
        assert parsed_pf_km == list(sym_ids), (
            f"Portfolio round-trip 顺序/内容不一致：期望 {list(sym_ids)}，实际 {parsed_pf_km}"
        )

        # ── 读 3：Schema PortfolioRead 反序列化（Text → list[int] via validator）──
        from app.schemas.portfolio import PortfolioRead
        read = PortfolioRead.model_validate(pf2)
        assert isinstance(read.key_members_json, list)
        assert read.key_members_json == list(sym_ids), (
            f"PortfolioRead schema round-trip 失败：期望 {list(sym_ids)}，实际 {read.key_members_json}"
        )

        # ── 读 4：显式传入 check_key_member_score_coverage 时生效列表保序 ──
        _insert_index_trade_dates(db, [date(2025, 1, 3)])
        fmr = "fmr_T_A3_C3_RT"
        from app.services.score_query_service import check_key_member_score_coverage
        # 不插任何 Score → 全缺但重点检查 effective 列表保序
        r = check_key_member_score_coverage(
            db,
            portfolio_id=pid,
            start_date=date(2025, 1, 3),
            end_date=date(2025, 1, 3),
            bound_factor_model_run_id=fmr,
            strategy_snapshot_key_members_json=list(sym_ids),
        )
        assert r["key_members_effective"] == list(sym_ids), (
            f"门禁函数传入 key_members 后 effective 顺序变化：期望 {list(sym_ids)}，实际 {r['key_members_effective']}"
        )
