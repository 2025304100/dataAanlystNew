"""T-A2: Q5.2 Score 新鲜度 SLA。

Checklist:
- T-A2-C1: PASS 条件：cutoff 在 1 个完整交易日内（含当天 T / 前一交易日 T-1）
- T-A2-C2: gap 按交易日历索引差（非日历日），周末/节假日跳过
- T-A2-C3: 枚举对齐 READY=1 / SCORE_STALE=3，不能是 2/4 等其他值
"""
from __future__ import annotations

from datetime import date, datetime

import pytest


def _make_symbol(db, sym_code: str, name: str = "测试股") -> int:
    from app.models.symbol import Symbol
    s = Symbol(symbol=sym_code, name=name, market="SH", asset_type="stock")
    db.add(s)
    db.flush()
    return s.id


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
        calc_batch_id="batch_T_A2",
        published_at=cutoff_utc,
        pit_flag="PIT_SAFE" if cutoff_utc else "NOT_CHECKED",
    )
    db.add(s)


def _make_fresh_shenzhen_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Shanghai")
    except Exception:
        pass
    try:
        import pytz
        return pytz.timezone("Asia/Shanghai")
    except Exception:
        return None


class TestT_A2_Staleness:

    def test_c1_pass_within_one_trade_day(self, db_session):
        """T-A2-C1: decision_at=2025-01-07 15:05+08(=07:05 UTC)。
        交易日历：[..., 01-02, 01-03, 01-06, 01-07]（01-06=周一，01-07=周二）。
        - 最新 Score cutoff = 2025-01-06 15:00 → trade_day_gap ≤ 1 → PASS
        - 最新 Score cutoff = 2025-01-07 15:00 → gap = 0 → PASS
        """
        from app.services.score_query_service import score_staleness_check

        db = db_session
        sid = _make_symbol(db, "T000001", "测试股1")
        trade_dates = [
            date(2025, 1, 2),
            date(2025, 1, 3),
            date(2025, 1, 6),
            date(2025, 1, 7),
        ]
        _insert_index_trade_dates(db, trade_dates)
        fmr = "fmr_T_A2_C1"

        tz_sh = _make_fresh_shenzhen_tz()
        decision_at_sh = datetime(2025, 1, 7, 15, 5, 0, tzinfo=tz_sh)

        # ========== 场景 A：cutoff = 2025-01-07 15:00 SH (当天 T) ==========
        cutoff_0107_sh = datetime(2025, 1, 7, 15, 0, 0, tzinfo=tz_sh)
        from app.services.decision_clock import shanghai_to_utc_naive
        cutoff_0107_utc = shanghai_to_utc_naive(cutoff_0107_sh)
        _insert_score(db, fmr_id=fmr, symbol_id=sid, trade_date=date(2025, 1, 7), cutoff_utc=cutoff_0107_utc)
        db.commit()

        r = score_staleness_check(
            db,
            bound_factor_model_run_id=fmr,
            decision_at=decision_at_sh,
            portfolio_id=123,
        )
        assert r["level"] == "PASS", f"T-day cutoff 应为 PASS，实际 {r['level']}"
        assert r["trade_day_gap"] == 0, f"T-day cutoff gap 应为 0，实际 {r['trade_day_gap']}"
        assert r["latest_trade_date_before_decision"] == date(2025, 1, 6)
        assert r["preceding_trade_date_before_decision"] == date(2025, 1, 3)
        assert r["sla_threshold_trade_days"] == 1
        assert r["decision_at_utc_naive"].tzinfo is None
        assert r["latest_score_factor_data_cutoff_at"] == cutoff_0107_utc
        assert r["portfolio_score_status_enum_3"] == 1

        # ========== 场景 B：更新 cutoff = 2025-01-06 15:00 SH (T-1) ==========
        cutoff_0106_sh = datetime(2025, 1, 6, 15, 0, 0, tzinfo=tz_sh)
        cutoff_0106_utc = shanghai_to_utc_naive(cutoff_0106_sh)
        _insert_score(db, fmr_id=fmr, symbol_id=sid, trade_date=date(2025, 1, 6), cutoff_utc=cutoff_0106_utc)
        db.commit()

        r2 = score_staleness_check(
            db,
            bound_factor_model_run_id=fmr,
            decision_at=decision_at_sh,
        )
        assert r2["level"] == "PASS", f"T-1 cutoff 应为 PASS，实际 {r2['level']}"
        assert r2["trade_day_gap"] <= 1, f"T-1 cutoff gap 应 ≤ 1，实际 {r2['trade_day_gap']}"
        assert r2["latest_score_factor_data_cutoff_at"] == cutoff_0107_utc
        assert r2["portfolio_score_status_enum_3"] == 1

    def test_c2_gap_counted_on_trade_days_not_calendar(self, db_session):
        """T-A2-C2: gap 按交易日历索引差，非日历日。
        decision_at=2025-01-07 15:05；最新 cutoff=2025-01-03（周五）。
        日历日差 = 4 天（01-03 → 01-07 含 04/05 周末），但交易日差只 = 1 → PASS！
        反证：若按日历日算 4 天 → 误判 STALE。
        再构造 cutoff=2025-01-02 → 交易日差 2 → STALE。
        """
        from app.services.score_query_service import score_staleness_check
        from app.services.decision_clock import shanghai_to_utc_naive

        db = db_session
        sid = _make_symbol(db, "T000002", "测试股2")
        trade_dates = [
            date(2024, 12, 31),
            date(2025, 1, 2),
            date(2025, 1, 3),
            date(2025, 1, 6),
            date(2025, 1, 7),
        ]
        _insert_index_trade_dates(db, trade_dates)
        fmr = "fmr_T_A2_C2"

        tz_sh = _make_fresh_shenzhen_tz()
        decision_at_sh = datetime(2025, 1, 7, 15, 5, 0, tzinfo=tz_sh)

        # ========== 场景 A：cutoff=2025-01-03（周五）→ 交易日差 1 → PASS ==========
        cutoff_0103_sh = datetime(2025, 1, 3, 15, 0, 0, tzinfo=tz_sh)
        cutoff_0103_utc = shanghai_to_utc_naive(cutoff_0103_sh)
        _insert_score(db, fmr_id=fmr, symbol_id=sid, trade_date=date(2025, 1, 3), cutoff_utc=cutoff_0103_utc)
        db.commit()

        r = score_staleness_check(
            db,
            bound_factor_model_run_id=fmr,
            decision_at=decision_at_sh,
        )
        calendar_days = (date(2025, 1, 7) - date(2025, 1, 3)).days
        assert calendar_days == 4, "日历日差应为 4（供对照）"
        assert r["trade_day_gap"] == 1, (
            f"交易日差应为 1（跳过 01-04/01-05 周末），实际 {r['trade_day_gap']}。"
            f"若按日历日算=4会误判 STALE，这正是 C2 要抓的 bug。"
        )
        assert r["level"] == "PASS", f"gap=1 ≤ SLA=1 应为 PASS，实际 {r['level']}"
        assert r["latest_trade_date_before_decision"] == date(2025, 1, 6)
        assert r["preceding_trade_date_before_decision"] == date(2025, 1, 3)

        # ========== 场景 B：cutoff=2025-01-02（周四）→ 交易日差 2 → STALE ==========
        from app.models.score import Score as _ScoreModel
        db.query(_ScoreModel).filter(
            _ScoreModel.factor_model_run_id == fmr,
            _ScoreModel.trade_date == date(2025, 1, 3),
        ).delete(synchronize_session=False)

        cutoff_0102_sh = datetime(2025, 1, 2, 15, 0, 0, tzinfo=tz_sh)
        cutoff_0102_utc = shanghai_to_utc_naive(cutoff_0102_sh)
        _insert_score(db, fmr_id=fmr, symbol_id=sid, trade_date=date(2025, 1, 2), cutoff_utc=cutoff_0102_utc)
        db.commit()

        r2 = score_staleness_check(
            db,
            bound_factor_model_run_id=fmr,
            decision_at=decision_at_sh,
        )
        assert r2["trade_day_gap"] >= 2, (
            f"cutoff=01-02 距 01-07 交易日差应 ≥ 2，实际 {r2['trade_day_gap']}"
        )
        assert r2["level"] == "STALE", f"gap≥2 应为 STALE，实际 {r2['level']}"

    def test_c3_enum_align_ready_1_stale_3(self, db_session):
        """T-A2-C3: PASS → portfolio_score_status_enum_3 = 1(READY)；
        STALE → = 3(SCORE_STALE)。
        断言不能是 2(RUNNING) / 4(DATA_INCOMPLETE) 等其他值。
        """
        from app.services.score_query_service import score_staleness_check
        from app.services.decision_clock import shanghai_to_utc_naive

        db = db_session
        sid = _make_symbol(db, "T000003", "测试股3")
        trade_dates = [
            date(2025, 1, 2),
            date(2025, 1, 3),
            date(2025, 1, 6),
            date(2025, 1, 7),
        ]
        _insert_index_trade_dates(db, trade_dates)
        fmr_good = "fmr_T_A2_C3_GOOD"
        fmr_bad = "fmr_T_A2_C3_BAD"

        tz_sh = _make_fresh_shenzhen_tz()
        decision_at_sh = datetime(2025, 1, 7, 15, 5, 0, tzinfo=tz_sh)

        # ========== PASS 场景：cutoff=T-1 (01-06) → enum 必须 = 1 ==========
        cutoff_good_sh = datetime(2025, 1, 6, 15, 0, 0, tzinfo=tz_sh)
        cutoff_good_utc = shanghai_to_utc_naive(cutoff_good_sh)
        _insert_score(db, fmr_id=fmr_good, symbol_id=sid, trade_date=date(2025, 1, 6), cutoff_utc=cutoff_good_utc)
        db.commit()

        r_pass = score_staleness_check(
            db,
            bound_factor_model_run_id=fmr_good,
            decision_at=decision_at_sh,
        )
        assert r_pass["level"] == "PASS"
        assert r_pass["portfolio_score_status_enum_3"] == 1, (
            f"PASS 对应 READY 枚举应为 1，实际 {r_pass['portfolio_score_status_enum_3']}"
        )
        assert r_pass["portfolio_score_status_enum_3"] not in {2, 4, 5, 0}, (
            f"PASS 枚举不能是 RUNNING(2)/DATA_INCOMPLETE(4)/其他值，"
            f"实际 {r_pass['portfolio_score_status_enum_3']}"
        )

        # ========== STALE 场景：cutoff 太旧（01-02）→ enum 必须 = 3 ==========
        cutoff_bad_sh = datetime(2025, 1, 2, 15, 0, 0, tzinfo=tz_sh)
        cutoff_bad_utc = shanghai_to_utc_naive(cutoff_bad_sh)
        _insert_score(db, fmr_id=fmr_bad, symbol_id=sid, trade_date=date(2025, 1, 2), cutoff_utc=cutoff_bad_utc)
        db.commit()

        r_stale = score_staleness_check(
            db,
            bound_factor_model_run_id=fmr_bad,
            decision_at=decision_at_sh,
        )
        assert r_stale["level"] == "STALE"
        assert r_stale["portfolio_score_status_enum_3"] == 3, (
            f"STALE 对应 SCORE_STALE 枚举应为 3，实际 {r_stale['portfolio_score_status_enum_3']}"
        )
        assert r_stale["portfolio_score_status_enum_3"] not in {1, 2, 4, 5, 0}, (
            f"STALE 枚举不能是 READY(1)/RUNNING(2)/DATA_INCOMPLETE(4)/其他值，"
            f"实际 {r_stale['portfolio_score_status_enum_3']}"
        )
