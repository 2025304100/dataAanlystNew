"""G1-WP0-6a: PIT Score 契约 + 反例测试。

Q4 / Q6 / Q22 严格断言：
1. Score 查询必须 factor_model_run_id == bound_id（禁止跨模型串接）
2. trade_date <= decision_date（无未来交易信号）
3. factor_data_cutoff_at <= data_cutoff_at（无未来数据水位）
4. published_at <= data_cutoff_at（披露时间 PIT 契约，NOT_PIT_SAFE 标记）
5. 反例：注入"未来披露"分数 → 查询结果必须剔除（pit_safe_flag=NOT_PIT_SAFE 且不纳入 coverage）
"""
from __future__ import annotations

import dataclasses as dc
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from app.models.symbol import Symbol
from app.models.score import Score
from app.services.score_query_service import (
    ScoreRow, estimate_score_coverage, fetch_latest_pit_scores,
)


def _make_s(db: "Session", sym: Symbol) -> Symbol:
    db.add(sym)
    db.commit()
    db.refresh(sym)
    return sym


def _insert(
    db: "Session",
    *,
    fmr_id: str,
    symbol_id: int,
    trade_date: date,
    cutoff_utc: datetime,
    published_at: datetime | None,
    score_value: float = 0.5,
    factor_set_id: str | None = "fs_pit_1",
    batch_id: str = "batch_pit_default",
) -> Score:
    pit_flag = "PIT_SAFE" if (
        published_at is not None and published_at <= cutoff_utc
    ) else ("NOT_PIT_SAFE" if published_at is not None else "NOT_CHECKED")
    s = Score(
        factor_model_run_id=fmr_id,
        symbol_id=symbol_id,
        trade_date=trade_date,
        factor_data_cutoff_at=cutoff_utc,
        factor_set_id=factor_set_id,
        weight_mode="ridge",
        priority_score=score_value,
        quality_score=score_value,
        timing_score=score_value,
        quality_grade="A",
        stage="growth",
        action="HOLD",
        model_alpha_score=score_value,
        calc_batch_id=batch_id,
        published_at=published_at,
        pit_flag=pit_flag,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


class TestPitScoreContract:
    """严格 PIT 反例与契约 (WP0-6a / Q22 / Q4)。"""

    @pytest.fixture()
    def syms(self, db_session: "Session") -> list[Symbol]:
        return [
            _make_s(db_session, Symbol(symbol="600000", name="浦发", market="SH", asset_type="stock")),
            _make_s(db_session, Symbol(symbol="000001", name="平安", market="SZ", asset_type="stock")),
            _make_s(db_session, Symbol(symbol="300750", name="宁德", market="SZ", asset_type="stock")),
        ]

    # ------------------------------------------------------------------
    # T_PIT_01：trade_date > decision_date → 未来数据，必须被排除
    # ------------------------------------------------------------------
    def test_t_pit_01_future_trade_date_is_excluded(self, db_session, syms):
        fmr = "fmr_pit_01"
        decision_dt = date(2025, 1, 10)  # Friday
        cutoff = datetime(2025, 1, 10, 7, 0, 0)  # UTC naive 07:00 = 15:00 CST
        pub = datetime(2025, 1, 10, 7, 0, 0)

        _insert(db_session, fmr_id=fmr, symbol_id=syms[0].id,
                trade_date=date(2025, 1, 10), cutoff_utc=cutoff, published_at=pub,
                score_value=0.8)  # valid, same day decision
        _insert(db_session, fmr_id=fmr, symbol_id=syms[1].id,
                trade_date=date(2025, 1, 13), cutoff_utc=cutoff, published_at=pub,
                score_value=0.9)  # INVALID: future Monday

        hits: list[ScoreRow] = fetch_latest_pit_scores(
            db_session,
            factor_model_run_id=fmr,
            symbol_ids=[s.id for s in syms[:2]],
            decision_date=decision_dt,
            data_cutoff_at=cutoff,
            require_published_at=False,
        )
        by_sym = {h.symbol_id: h for h in hits}
        assert syms[0].id in by_sym, "同日有效 Score 必须保留"
        assert syms[1].id not in by_sym, "未来 trade_date Score 必须剔除"

    # ------------------------------------------------------------------
    # T_PIT_02：published_at > cutoff → NOT_PIT_SAFE；require_published_at=True → 剔除
    # ------------------------------------------------------------------
    def test_t_pit_02_published_after_cutoff_must_be_rejected_in_pit_mode(
        self, db_session, syms,
    ):
        fmr = "fmr_pit_02"
        decision_dt = date(2025, 1, 6)  # Monday
        cutoff = datetime(2025, 1, 3, 7, 0, 0)  # Friday 15:00 CST → UTC 07:00

        _insert(db_session, fmr_id=fmr, symbol_id=syms[0].id,
                trade_date=date(2025, 1, 3), cutoff_utc=cutoff,
                published_at=datetime(2025, 1, 3, 6, 59, 0),
                score_value=0.55)  # PIT_SAFE
        _insert(db_session, fmr_id=fmr, symbol_id=syms[1].id,
                trade_date=date(2025, 1, 3), cutoff_utc=cutoff,
                published_at=datetime(2025, 1, 3, 7, 1, 0),  # 晚于 cutoff 1 分钟
                score_value=0.99)

        # 严格 PIT: published_at 不可缺失、必须 <= cutoff
        hits = fetch_latest_pit_scores(
            db_session,
            factor_model_run_id=fmr,
            symbol_ids=[s.id for s in syms[:2]],
            decision_date=decision_dt,
            data_cutoff_at=cutoff,
            require_published_at=True,  # 正式 PIT
        )
        by_sym = {h.symbol_id: h for h in hits}
        assert syms[0].id in by_sym and by_sym[syms[0].id].pit_safe_flag == "PIT_SAFE"
        assert syms[1].id not in by_sym, "NOT_PIT_SAFE(published>cutoff) 在正式PIT必须被剔除 (Q4.2)"

    # ------------------------------------------------------------------
    # T_PIT_03：研究模式 (require_published_at=False) 允许但标记 NOT_PIT_SAFE
    # ------------------------------------------------------------------
    def test_t_pit_03_research_mode_allows_not_pit_but_labels_it(self, db_session, syms):
        fmr = "fmr_pit_03"
        decision_dt = date(2025, 1, 6)
        cutoff = datetime(2025, 1, 3, 7, 0, 0)

        _insert(db_session, fmr_id=fmr, symbol_id=syms[0].id,
                trade_date=date(2025, 1, 3), cutoff_utc=cutoff,
                published_at=None, score_value=0.3)  # Q4.2: 空披露 → NOT_PIT_SAFE

        hits = fetch_latest_pit_scores(
            db_session,
            factor_model_run_id=fmr,
            symbol_ids=[syms[0].id],
            decision_date=decision_dt,
            data_cutoff_at=cutoff,
            require_published_at=False,  # 研究模式
        )
        assert len(hits) == 1
        assert hits[0].pit_safe_flag == "NOT_PIT_SAFE", (
            f"published_at=None 必须标记 NOT_PIT_SAFE, 实际 {hits[0].pit_safe_flag}"
        )

    # ------------------------------------------------------------------
    # T_PIT_04：factor_model_run_id 错配 → 0 命中（Q22.1 禁止隐式匹配）
    # ------------------------------------------------------------------
    def test_t_pit_04_wrong_model_id_always_zero(self, db_session, syms):
        fmr_a = "fmr_pit_A"
        fmr_b = "fmr_pit_B"
        decision_dt = date(2025, 1, 10)
        cutoff = datetime(2025, 1, 10, 7, 0, 0)
        pub = datetime(2025, 1, 10, 6, 0, 0)

        _insert(db_session, fmr_id=fmr_a, symbol_id=syms[0].id,
                trade_date=decision_dt, cutoff_utc=cutoff, published_at=pub,
                score_value=0.7)

        hits = fetch_latest_pit_scores(
            db_session,
            factor_model_run_id=fmr_b,  # 错的模型
            symbol_ids=[syms[0].id],
            decision_date=decision_dt,
            data_cutoff_at=cutoff,
            require_published_at=False,
        )
        assert hits == [], "不同 factor_model_run_id 必须严格 0 命中（Q22.1 禁止隐式匹配）"

    # ------------------------------------------------------------------
    # T_PIT_05：Coverage 对每个成员单独统计，返回 actual/expected (Q5)
    # ------------------------------------------------------------------
    def test_t_pit_05_coverage_counts_per_member(self, db_session, syms):
        fmr = "fmr_pit_05"
        decision_dt = date(2025, 1, 7)
        cutoff = datetime(2025, 1, 7, 7, 0, 0)
        pub = datetime(2025, 1, 7, 6, 0, 0)

        # 3 个成员，只给前 2 个写 Score → actual=2/expected=3
        _insert(db_session, fmr_id=fmr, symbol_id=syms[0].id,
                trade_date=decision_dt, cutoff_utc=cutoff, published_at=pub)
        _insert(db_session, fmr_id=fmr, symbol_id=syms[1].id,
                trade_date=decision_dt, cutoff_utc=cutoff, published_at=pub)

        expected, actual, _age = estimate_score_coverage(
            db_session,
            factor_model_run_id=fmr,
            symbol_ids=[s.id for s in syms],  # N=3
            decision_date=decision_dt,
            data_cutoff_at=cutoff,
        )
        assert expected == 3
        assert actual == 2, "前 2 个成员有 Score → actual=2"
        # 覆盖率 66.7% < 95% (Q5.1) → 正式链路阻断；这里只验证口径
        pct = actual / expected * 100.0
        assert pct < 95.0
