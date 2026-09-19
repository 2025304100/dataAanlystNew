"""T-A1: Q5.1 Score SLA 覆盖率窗口累计。

Checklist:
- T-A1-C1: 3 档 level 判定 (PASS ≥95%, WARN 92~95%, FAIL <92%)
- T-A1-C2: 交易日窗口排除周末/节假日（从 index_price 000300 取）
- T-A1-C3: 成员列表必须来自 StrategyExecutionSnapshot.member_snapshot_json（而非当前 PortfolioMember 表）
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
        name=kw.pop("name", "T_A1 组合"),
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
        calc_batch_id="batch_T_A1",
        published_at=cutoff_utc,
        pit_flag="PIT_SAFE" if cutoff_utc else "NOT_CHECKED",
    )
    db.add(s)


def _insert_snapshot(
    db,
    *,
    portfolio_id: int,
    symbol_ids: list[int],
    created_at: datetime,
    snapshot_id: str = "snap_T_A1",
) -> None:
    from app.models.decision_engine import StrategyExecutionSnapshot
    members_json = json.dumps([{"symbol_id": sid} for sid in symbol_ids])
    s = StrategyExecutionSnapshot(
        id=snapshot_id,
        portfolio_id=portfolio_id,
        decision_clock_json="{}",
        member_snapshot_json=members_json,
        universe_type="portfolio_members",
        snapshot_type="save_and_apply",
        snapshot_hash="a" * 64,
        idempotency_key=f"idemp_{snapshot_id}",
        effective_from=created_at,
        created_at=created_at,
    )
    db.add(s)
    db.flush()


@pytest.fixture()
def portfolio_with_100_members_snapshot(db_session):
    """构造 100 只成员的组合 snapshot + 2 个交易日的 index_price。"""
    db = db_session
    pid = _make_portfolio(db, name="T_A1_C1_100x2")
    symbol_ids = [_make_symbol(db, f"T{i:06d}", f"测试股{i}") for i in range(100)]
    created_at = datetime(2025, 1, 2, 12, 0, 0)
    _insert_snapshot(db, portfolio_id=pid, symbol_ids=symbol_ids, created_at=created_at)
    trade_dates = [date(2025, 1, 3), date(2025, 1, 6)]
    _insert_index_trade_dates(db, trade_dates)
    db.commit()
    return {
        "portfolio_id": pid,
        "symbol_ids": symbol_ids,
        "trade_dates": trade_dates,
        "fmr_id": "fmr_T_A1_C1",
    }


class TestT_A1_CoverageWindow:

    def test_c1_3tier_levels_100x2(self, db_session, portfolio_with_100_members_snapshot):
        """T-A1-C1: 3 档 level 判定。
        100 只 × 2 交易日 = 分母 200。
        场景 A：100 条 → 50% → FAIL
        场景 B：190 条 → 95% → PASS
        场景 C：185 条 → 92.5% → WARN
        """
        from app.services.score_query_service import estimate_score_coverage_window
        data = portfolio_with_100_members_snapshot
        pid = data["portfolio_id"]
        sym_ids = data["symbol_ids"]
        tds = data["trade_dates"]
        fmr = data["fmr_id"]
        cutoff = datetime(2025, 1, 6, 7, 0, 0)

        # ========== 场景 A：100 条（每只成员只有第 1 天有 Score） ==========
        for sid in sym_ids:
            _insert_score(db_session, fmr_id=fmr, symbol_id=sid, trade_date=tds[0], cutoff_utc=cutoff)
        db_session.commit()

        r = estimate_score_coverage_window(
            db_session,
            portfolio_id=pid,
            start_date=date(2025, 1, 3),
            end_date=date(2025, 1, 6),
            bound_factor_model_run_id=fmr,
        )
        assert r["member_count_day_count_expected_denominator"] == 200
        assert r["score_rows_found_numerator"] == 100
        assert r["coverage_rate"] == round(100 / 200, 9)
        assert r["coverage_level"] == "FAIL", f"50% 应为 FAIL，实际 {r['coverage_level']}"

        # ========== 场景 B：追加 90 条（凑 190 条 = 95%） ==========
        for sid in sym_ids[:90]:
            _insert_score(db_session, fmr_id=fmr, symbol_id=sid, trade_date=tds[1], cutoff_utc=cutoff)
        db_session.commit()

        r = estimate_score_coverage_window(
            db_session,
            portfolio_id=pid,
            start_date=date(2025, 1, 3),
            end_date=date(2025, 1, 6),
            bound_factor_model_run_id=fmr,
        )
        assert r["member_count_day_count_expected_denominator"] == 200
        assert r["score_rows_found_numerator"] == 190
        assert r["coverage_rate"] == round(190 / 200, 9)
        assert r["coverage_level"] == "PASS", f"95% 应为 PASS，实际 {r['coverage_level']}"

        # ========== 场景 C：回滚场景 B，改为 185 条 = 92.5% ==========
        from app.models.score import Score
        db_session.query(Score).filter(
            Score.factor_model_run_id == fmr,
            Score.trade_date == tds[1],
            Score.symbol_id.in_(sym_ids[85:90]),
        ).delete(synchronize_session=False)
        db_session.commit()

        r = estimate_score_coverage_window(
            db_session,
            portfolio_id=pid,
            start_date=date(2025, 1, 3),
            end_date=date(2025, 1, 6),
            bound_factor_model_run_id=fmr,
        )
        assert r["member_count_day_count_expected_denominator"] == 200
        assert r["score_rows_found_numerator"] == 185
        assert r["coverage_rate"] == round(185 / 200, 9)
        assert r["coverage_level"] == "WARN", f"92.5% 应为 WARN，实际 {r['coverage_level']}"

        # 边界：92.0% 精确
        assert r["coverage_rate"] == 0.925
        # per_member_coverage 长度 = 100
        assert len(r["per_member_coverage"]) == 100
        # 抽查第 1 个成员：两天都有 → rate 1.0
        first = r["per_member_coverage"][0]
        assert first["expected_days"] == 2
        assert first["actual_days"] == 2
        assert first["rate"] == 1.0
        assert first["level"] == "PASS"

    def test_c2_trade_dates_exclude_weekend_holiday(self, db_session):
        """T-A1-C2: 交易日窗口排除周末/节假日（自然日 5 天 vs 实际交易日 3 天）。
        start=2025-01-03(周五) end=2025-01-07(周二) → 只有 01-03,01-06,01-07 = 3 天。
        """
        from app.services.score_query_service import estimate_score_coverage_window
        db = db_session
        pid = _make_portfolio(db, name="T_A1_C2_Weekend")
        sym_ids = [_make_symbol(db, f"T{i:06d}") for i in range(5)]
        created_at = datetime(2025, 1, 2, 12, 0, 0)
        _insert_snapshot(db, portfolio_id=pid, symbol_ids=sym_ids, created_at=created_at)
        trade_dates = [
            date(2025, 1, 3),   # 周五
            date(2025, 1, 6),   # 周一
            date(2025, 1, 7),   # 周二
        ]
        _insert_index_trade_dates(db, trade_dates)
        fmr = "fmr_T_A1_C2"
        cutoff = datetime(2025, 1, 7, 7, 0, 0)
        for sid in sym_ids:
            for td in trade_dates:
                _insert_score(db, fmr_id=fmr, symbol_id=sid, trade_date=td, cutoff_utc=cutoff)
        db.commit()

        r = estimate_score_coverage_window(
            db,
            portfolio_id=pid,
            start_date=date(2025, 1, 3),
            end_date=date(2025, 1, 7),
            bound_factor_model_run_id=fmr,
        )
        assert r["trade_date_count_in_window"] == 3, (
            f"应为 3 个交易日（排除周末 01-04/01-05），实际 {r['trade_date_count_in_window']}"
        )
        assert r["trade_dates_used"] == trade_dates
        assert r["member_count_from_snapshot"] == 5
        assert r["member_count_day_count_expected_denominator"] == 5 * 3
        assert r["score_rows_found_numerator"] == 5 * 3
        assert r["coverage_rate"] == 1.0
        assert r["coverage_level"] == "PASS"

    def test_c3_members_from_snapshot_not_current_table(self, db_session):
        """T-A1-C3: 成员必须来自 StrategyExecutionSnapshot.member_snapshot_json。
        反例：当前 PortfolioMember 表 90 只，但 snapshot 有 100 只 → member_count_from_snapshot 必须 = 100。
        """
        from app.services.score_query_service import estimate_score_coverage_window
        from app.models.portfolio_member import PortfolioMember
        db = db_session
        pid = _make_portfolio(db, name="T_A1_C3_PIT")
        snapshot_sym_ids = [_make_symbol(db, f"S{i:06d}", f"快照股{i}") for i in range(100)]
        current_table_sym_ids = snapshot_sym_ids[:90]
        created_at = datetime(2025, 1, 2, 12, 0, 0)
        _insert_snapshot(
            db,
            portfolio_id=pid,
            symbol_ids=snapshot_sym_ids,
            created_at=created_at,
            snapshot_id="snap_T_A1_C3",
        )
        for sid in current_table_sym_ids:
            pm = PortfolioMember(
                portfolio_id=pid,
                symbol_id=sid,
                status="active",
                execution_mode="manual",
                source_type="manual",
            )
            db.add(pm)
        trade_dates = [date(2025, 1, 3)]
        _insert_index_trade_dates(db, trade_dates)
        fmr = "fmr_T_A1_C3"
        cutoff = datetime(2025, 1, 3, 7, 0, 0)
        for sid in snapshot_sym_ids:
            _insert_score(db, fmr_id=fmr, symbol_id=sid, trade_date=trade_dates[0], cutoff_utc=cutoff)
        db.commit()

        r = estimate_score_coverage_window(
            db,
            portfolio_id=pid,
            start_date=date(2025, 1, 3),
            end_date=date(2025, 1, 3),
            bound_factor_model_run_id=fmr,
        )
        assert r["member_count_from_snapshot"] == 100, (
            f"必须来自 snapshot 的 100 只，而非当前 PortfolioMember 表，实际 {r['member_count_from_snapshot']}"
        )
        assert len(r["member_ids_used"]) == 100
        assert r["member_count_day_count_expected_denominator"] == 100 * 1
        assert r["score_rows_found_numerator"] == 100
        assert r["coverage_rate"] == 1.0
        assert r["coverage_level"] == "PASS"
