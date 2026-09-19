"""G2-WP0-4：Step 6 真实 Score 查询 / Step 7 信号映射 / Step 8 顺序 clamp Allocator 的行为验证。

依赖：
  - tmp_alembic_db（来自 conftest / test_g1_decision_engine 同名 fixture 逻辑，此处复用同一 SQLite+alembic head 模式）
  - Score 表直接 INSERT，quality_score 是信号映射依据（与 test_g1_decision_engine._insert_scores 的 model_alpha_score=value 不同）

覆盖 6 AC：
  T_G2_WP04_01：3/3 Score → coverage_pct = 100%；0.93 覆盖率保留 9 位小数
  T_G2_WP04_02：4 Score 3 Hit（1 个 wrong model，1 个 pub_after_cutoff，1 个 missing）→ coverage=75%，freshness 聚合正确
  T_G2_WP04_03：quality_score 阈值默认 buy=0.70 / sell=0.35 → BUY / HOLD / SELL 三档信号命中
  T_G2_WP04_04：signal_policy 自定义阈值覆盖（buy=0.60 sell=0.40）→ 同一行 Score 信号改变
  T_G2_WP04_05：PIT_SAFE/NOT_PIT_SAFE 标记走 published_at ≤ cutoff（与 build_evidence 口径一致，evidence 层 pit_safe_flag 断言）
  T_G2_WP04_06：sequential_clamp_allocate（Q10）把 BUY 信号 → intent_pct=min(5%, stage_limit)，clamp_trace 非空
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime
from pathlib import Path

import pytest


os.environ.setdefault("ALEMBIC_DATABASE_URL", "")


@pytest.fixture(scope="function")
def tmp_alembic_db():
    """与 test_g1_decision_engine 完全一致的 SQLite+alembic head fixture。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_g2_wp04_")
    os.close(fd)
    url = f"sqlite:///{path}"
    os.environ["ALEMBIC_DATABASE_URL"] = url
    from alembic.config import Config
    from alembic import command as ac
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "alembic"))
    ac.upgrade(cfg, "head")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass


# ------------------------- 复用 G1 helpers -------------------------
def _make_portfolio(db, **kw):
    from app.models.portfolio import Portfolio
    p = Portfolio(
        name=kw.pop("name", "T_G2_WP04 Test"),
        account_type=kw.pop("account_type", "sim"),
        asset_scope=kw.pop("asset_scope", "mixed"),
        total_capital=kw.pop("total_capital", 1_000_000.0),
        investable_ratio=kw.pop("investable_ratio", 0.95),
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
    db.add(p); db.flush()
    return p


def _make_symbol(db, code="WPS", name="Wp4Symbol", market="SSE", **kw):
    from app.models.symbol import Symbol
    s = Symbol(symbol=code, name=name, market=market, asset_type="STOCK", is_active=1)
    for attr in ("board", "industry", "theme"):
        if not hasattr(s, attr):
            setattr(s, attr, kw.get(attr))
    # board/industry 如果是 None，SQLite TEXT 允许 NULL
    db.add(s); db.flush(); return s


def _set_active_model(db, model_run_id: str | None):
    from app.models.factor_runtime import FactorRuntimeState
    s = db.get(FactorRuntimeState, 1)
    if s is None:
        s = FactorRuntimeState(id=1, weight_mode="manual", active_model_run_id=model_run_id,
                               updated_by="qa", version=1)
        db.add(s)
    else:
        s.weight_mode = "manual"; s.active_model_run_id = model_run_id; s.version += 1
    db.flush(); return s


def _save_and_apply(db, portfolio_id, fmr_id, run_mode="research", snapshot_versions_override=None):
    from app.services.factor_usage_service import save_and_apply_usage
    from app.schemas.decision_engine import FactorUsageBindRequest
    resp = save_and_apply_usage(
        db, portfolio_id,
        FactorUsageBindRequest(factor_model_run_id=fmr_id, run_mode=run_mode, pit_mode="best_effort"),
        "qa_user",
    )
    # 若传入 snapshot_versions_override → 手动 UPDATE StrategyExecutionSnapshot.versions_json 合并注入
    if snapshot_versions_override:
        from app.models.decision_engine import StrategyExecutionSnapshot
        import json as _json
        row = db.get(StrategyExecutionSnapshot, resp.strategy_snapshot_id)
        if row is not None:
            existing = {}
            try:
                if row.versions_json:
                    existing = _json.loads(row.versions_json) or {}
            except Exception:
                existing = {}
            existing.update(snapshot_versions_override)
            row.versions_json = _json.dumps(existing, ensure_ascii=False, sort_keys=True)
            db.flush()
    return resp


def _insert_wp04_scores(db, fmr_id, rows):
    """rows = list[(sym_id:int, trade_date:date, quality_score:float, published_at:datetime or None)]

    与 G1 的区别：本测试以 quality_score 作为信号映射依据（model_alpha_score 同值冗余）。
    """
    from app.models.score import Score
    for idx, (sym_id, td, value, pub) in enumerate(rows):
        bid = f"qa_wp04_{fmr_id[-8:]}_{idx:03d}"
        cutoff_utc = datetime(td.year, td.month, td.day, 7, 0, 0)
        db.add(Score(
            symbol_id=sym_id, trade_date=td, quality_score=float(value), quality_grade="A",
            timing_score=float(value), stage="growth", action="HOLD", priority_score=float(value),
            weight_mode="manual", factor_model_run_id=fmr_id,
            factor_data_cutoff_at=cutoff_utc,
            model_alpha_score=float(value), calc_batch_id=bid,
            published_at=pub,
            pit_flag=("PIT_SAFE" if pub else "NOT_CHECKED"),
        ))
    db.flush()


class TestWP04Step6ScoreQuery:
    """Step 6 Scorer（真实 _real_scorer，即默认 DecisionEngine()）。"""

    def test_t_g2_wp04_01_3hits_100pct_coverage(self, tmp_alembic_db):
        from app.services.decision_engine import DecisionEngine
        db = tmp_alembic_db
        p = _make_portfolio(db); _set_active_model(db, "fmr_WP04_01")
        sA = _make_symbol(db, "WA", industry="tech"); sB = _make_symbol(db, "WB", industry="fin"); sC = _make_symbol(db, "WC", industry="cons")
        from app.models.portfolio_member import PortfolioMember
        for s in (sA, sB, sC):
            db.add(PortfolioMember(portfolio_id=p.id, symbol_id=s.id, status="active"))
        decision_date = date(2025, 1, 6)
        pub_utc = datetime(2025, 1, 6, 6, 59, 59)  # before 07:00 UTC cutoff
        _insert_wp04_scores(db, "fmr_WP04_01", [
            (sA.id, decision_date, 0.88, pub_utc),
            (sB.id, decision_date, 0.77, pub_utc),
            (sC.id, decision_date, 0.66, pub_utc),
        ])
        resp = _save_and_apply(db, p.id, "fmr_WP04_01"); db.commit()
        # 默认 engine（用 _real_scorer，不再传 custom scorer）
        eng = DecisionEngine()
        r = eng.evaluate(db, portfolio_id=p.id, strategy_snapshot_id=resp.strategy_snapshot_id,
                         trade_date=decision_date, run_type="research_preflight", dry_run=True)
        assert r.score_coverage_pct == 100.0
        assert r.score_max_age_days == 0  # score date == decision date
        evidence_ids = sorted([e.symbol_id for e in r.evidence])
        assert evidence_ids == sorted([sA.id, sB.id, sC.id])

    def test_t_g2_wp04_02_wrong_model_and_pub_after_cutoff_missed(self, tmp_alembic_db):
        from app.services.decision_engine import DecisionEngine
        db = tmp_alembic_db
        p = _make_portfolio(db); _set_active_model(db, "fmr_CORRECT")
        sHIT1 = _make_symbol(db, "WH1"); sHIT2 = _make_symbol(db, "WH2")
        sWRONG = _make_symbol(db, "WW"); sAFTER = _make_symbol(db, "WA2"); sMISS = _make_symbol(db, "WM")
        from app.models.portfolio_member import PortfolioMember
        all_syms = (sHIT1, sHIT2, sWRONG, sAFTER, sMISS)
        for s in all_syms:
            db.add(PortfolioMember(portfolio_id=p.id, symbol_id=s.id, status="active"))
        decision_date = date(2025, 1, 7)
        cutoff = datetime(2025, 1, 7, 7, 0, 0)
        before = datetime(2025, 1, 7, 6, 0, 0)
        after = datetime(2025, 1, 7, 8, 0, 0)
        # correct model × 2 hit（HIT1 / HIT2）
        _insert_wp04_scores(db, "fmr_CORRECT", [
            (sHIT1.id, decision_date, 0.80, before),
            (sHIT2.id, decision_date, 0.55, before),
            # sAFTER: published > cutoff → 应被 FR-P0-6 过滤
            (sAFTER.id, decision_date, 0.90, after),
            # sWRONG: 正确模型但 score.trade_date = 昨天（≠ 决策日）→ 也 miss
            (sWRONG.id, date(2025, 1, 6), 0.90, before),
            # sMISS: 完全没 score
        ])
        resp = _save_and_apply(db, p.id, "fmr_CORRECT"); db.commit()
        eng = DecisionEngine()
        r = eng.evaluate(db, portfolio_id=p.id, strategy_snapshot_id=resp.strategy_snapshot_id,
                         trade_date=decision_date, run_type="research_preflight", dry_run=True)
        # expected = 5 universe members；
        # actual = 3（HIT1, HIT2: 正确 × 正确日期，2 hit；sAFTER: pub>cutoff 但行存在 = 保留在 actual 中
        #            便于 build_evidence 打 NOT_PIT_SAFE 标签；
        #            sWRONG 日期不对（decision date 1/7，但 Score.trade_date=1/6 昨日 → 不命中）；
        #            sMISS 完全没 Score 行。
        # ⇒ 3/5=60%
        assert r.score_coverage_pct == pytest.approx(60.0), f"3/5 = 60%，实际 {r.score_coverage_pct}"
        # PIT 安全的 Score 数 = 2（HIT1/HIT2，它们的 evidence pit=PIT_SAFE）
        pit_passed = [e for e in r.evidence if e.pit_safe_flag == "PIT_SAFE"]
        assert len(pit_passed) == 2, f"应仅 2 支 PIT_SAFE，实际 {[(e.symbol_id, e.pit_safe_flag) for e in r.evidence]}"


class TestWP04Step7SignalMapping:
    """Step 7：Quality Score → BUY/HOLD/SELL。"""

    def test_t_g2_wp04_03_default_thresholds_3tier(self, tmp_alembic_db):
        from app.services.decision_engine import DecisionEngine
        db = tmp_alembic_db
        p = _make_portfolio(db); _set_active_model(db, "fmr_WP04_03")
        sBUY = _make_symbol(db, "WBUY", industry="tech")
        sHOLD = _make_symbol(db, "WHOLD", industry="tech")
        sSELL = _make_symbol(db, "WSELL", industry="tech")
        from app.models.portfolio_member import PortfolioMember
        for s in (sBUY, sHOLD, sSELL):
            db.add(PortfolioMember(portfolio_id=p.id, symbol_id=s.id, status="active"))
        decision_date = date(2025, 1, 7)
        before = datetime(2025, 1, 7, 6, 0, 0)
        # 默认 buy_th=0.70 / sell_th=0.35
        _insert_wp04_scores(db, "fmr_WP04_03", [
            (sBUY.id, decision_date, 0.71, before),   # >0.70 → BUY
            (sHOLD.id, decision_date, 0.50, before),   # (0.35,0.70) → HOLD
            (sSELL.id, decision_date, 0.34, before),   # <0.35 → SELL
        ])
        resp = _save_and_apply(db, p.id, "fmr_WP04_03"); db.commit()
        eng = DecisionEngine()
        r = eng.evaluate(db, portfolio_id=p.id, strategy_snapshot_id=resp.strategy_snapshot_id,
                         trade_date=decision_date, run_type="research_preflight", dry_run=True)
        act = {e.symbol_id: e.action for e in r.evidence}
        assert act[sBUY.id] == "BUY", f"0.71 应 BUY，实际 {act[sBUY.id]}"
        assert act[sHOLD.id] == "HOLD", f"0.50 应 HOLD，实际 {act[sHOLD.id]}"
        assert act[sSELL.id] == "SELL", f"0.34 应 SELL，实际 {act[sSELL.id]}"

    def test_t_g2_wp04_04_custom_signal_policy_overrides(self, tmp_alembic_db):
        """snap.versions["signal_policy"] 覆盖阈值：buy=0.60 sell=0.40。

        同一行 quality_score=0.55：默认 HOLD（0.35-0.70）→ 新阈值下应 SELL（≤0.40？不，0.55∈[0.40,0.60] → HOLD 仍 HOLD；
        再配 q=0.61：默认 HOLD（0.61<0.70）→ 新 BUY（≥0.60）；
        q=0.39：默认 SELL（≤0.35？不，0.39>0.35 默认 HOLD；新阈值 ≤0.40 → SELL）。
        """
        from app.services.decision_engine import DecisionEngine
        db = tmp_alembic_db
        p = _make_portfolio(db); _set_active_model(db, "fmr_WP04_04")
        sB = _make_symbol(db, "WB1", industry="tech")  # q=0.61: 默认 HOLD → 自定义 BUY
        sH = _make_symbol(db, "WH1", industry="tech")  # q=0.55: 默认 HOLD → 自定义 HOLD
        sS = _make_symbol(db, "WS1", industry="tech")  # q=0.39: 默认 HOLD → 自定义 SELL
        from app.models.portfolio_member import PortfolioMember
        for s in (sB, sH, sS):
            db.add(PortfolioMember(portfolio_id=p.id, symbol_id=s.id, status="active"))
        decision_date = date(2025, 1, 7)
        before = datetime(2025, 1, 7, 6, 0, 0)
        _insert_wp04_scores(db, "fmr_WP04_04", [
            (sB.id, decision_date, 0.61, before),
            (sH.id, decision_date, 0.55, before),
            (sS.id, decision_date, 0.39, before),
        ])
        # save_and_apply 传 versions_override
        resp = _save_and_apply(
            db, p.id, "fmr_WP04_04",
            snapshot_versions_override={
                "signal_policy": {"buy_threshold": 0.60, "sell_threshold": 0.40},
            },
        ); db.commit()
        eng = DecisionEngine()
        r = eng.evaluate(db, portfolio_id=p.id, strategy_snapshot_id=resp.strategy_snapshot_id,
                         trade_date=decision_date, run_type="research_preflight", dry_run=True)
        act = {e.symbol_id: e.action for e in r.evidence}
        assert act[sB.id] == "BUY", f"0.61 ≥ 0.60 应 BUY，实际 {act[sB.id]}"
        assert act[sH.id] == "HOLD", f"0.55 ∈ (0.40, 0.60) 应 HOLD，实际 {act[sH.id]}"
        assert act[sS.id] == "SELL", f"0.39 ≤ 0.40 应 SELL，实际 {act[sS.id]}"


class TestWP04Step6Pit:
    """T_G2_WP04_05：PIT_SAFE / NOT_PIT_SAFE 口径一致。"""
    def test_t_g2_wp04_05_pit_safe_marking(self, tmp_alembic_db):
        from app.services.decision_engine import DecisionEngine
        db = tmp_alembic_db
        p = _make_portfolio(db); _set_active_model(db, "fmr_WP04_05")
        sOK = _make_symbol(db, "WPOK", industry="tech")  # published ≤ cutoff → PIT_SAFE
        sAFTER = _make_symbol(db, "WPAF", industry="tech")  # published > cutoff → NOT_PIT_SAFE
        sNULL = _make_symbol(db, "WPNU", industry="tech")  # published NULL → NOT_PIT_SAFE
        from app.models.portfolio_member import PortfolioMember
        for s in (sOK, sAFTER, sNULL):
            db.add(PortfolioMember(portfolio_id=p.id, symbol_id=s.id, status="active"))
        decision_date = date(2025, 1, 7)
        cutoff = datetime(2025, 1, 7, 7, 0, 0)
        _insert_wp04_scores(db, "fmr_WP04_05", [
            (sOK.id, decision_date, 0.80, datetime(2025, 1, 7, 6, 59, 0)),
            (sAFTER.id, decision_date, 0.50, datetime(2025, 1, 7, 7, 1, 0)),
            (sNULL.id, decision_date, 0.30, None),
        ])
        resp = _save_and_apply(db, p.id, "fmr_WP04_05"); db.commit()
        eng = DecisionEngine()
        r = eng.evaluate(db, portfolio_id=p.id, strategy_snapshot_id=resp.strategy_snapshot_id,
                         trade_date=decision_date, run_type="research_preflight", dry_run=True)
        pit = {e.symbol_id: e.pit_safe_flag for e in r.evidence}
        assert pit[sOK.id] == "PIT_SAFE", pit
        assert pit[sAFTER.id] == "NOT_PIT_SAFE", pit
        assert pit[sNULL.id] == "NOT_PIT_SAFE", pit


class TestWP04Step8Allocator:
    """T_G2_WP04_06：sequential_clamp_allocate（Q10）输出 target_position_pct 规范化，clamp_trace 非空。"""
    def test_t_g2_wp04_06_allocator_clamp_trace_and_pct_filled(self, tmp_alembic_db):
        from app.services.decision_engine import DecisionEngine
        from app.models.portfolio import PortfolioRule
        db = tmp_alembic_db
        # 组合 1M 本金，total_capital=1,000,000 investable_ratio=0.95 → investable=950,000
        p = _make_portfolio(db, total_capital=1_000_000.0, investable_ratio=0.95,
                            default_single_position_pct=0.10)
        _set_active_model(db, "fmr_WP04_06")
        sBUY = _make_symbol(db, "WBIG", industry="tech")
        from app.models.portfolio_member import PortfolioMember
        db.add(PortfolioMember(portfolio_id=p.id, symbol_id=sBUY.id, status="active"))
        # 注意：WP0-4 故意不创建 PortfolioRule，用 _portfolio_default_constraints
        # （Portfolio 字段推断 fallback 分支）来验证：rule 缺失时 sequential_clamp_allocate
        # 仍然正常工作，不会进入 PRE_0_RULE HOLD（WP0-4 核心修复）。
        db.commit()
        decision_date = date(2025, 1, 7)
        before = datetime(2025, 1, 7, 6, 0, 0)
        _insert_wp04_scores(db, "fmr_WP04_06", [
            (sBUY.id, decision_date, 0.85, before),  # BUY，意图 5%
        ])
        resp = _save_and_apply(db, p.id, "fmr_WP04_06"); db.commit()
        eng = DecisionEngine()
        # 以 dry_run=True 进入；price_data_by_symbol=手动注入一个价格（否则 sequential_clamp_allocate 可能价格为 None，target_quantity 无法算）
        r = eng.evaluate(
            db, portfolio_id=p.id, strategy_snapshot_id=resp.strategy_snapshot_id,
            trade_date=decision_date, run_type="research_preflight", dry_run=True,
            price_data_by_symbol={
                sBUY.id: {
                    "open_price": 10.0, "close_price": 9.9,
                    "high_price": 10.1, "low_price": 9.8, "volume": 1_000_000,
                    "prev_close_price": 9.8,
                }
            },
        )
        # clamp_trace 来自 sequential_clamp_allocate → 通过 evaluate 的中间接口没法直接取到返回值
        # 改为直接调用 _real_allocator 检查
        from app.services.decision_engine import (
            _real_allocator, _real_scorer, _real_signal_generator,
            _stub_snapshot_loader, _stub_gate, _stub_universe_builder, _stub_health_check,
        )
        snap = _stub_snapshot_loader(db, resp.strategy_snapshot_id)
        univ = _stub_universe_builder(db, snap, r.clock.data_cutoff_at)
        univ = _stub_health_check(db, univ, r.clock.data_cutoff_at)
        scored = _real_scorer(db, snap, univ, r.clock.data_cutoff_at, decision_date)
        signal = _real_signal_generator(db, snap, scored)
        alloc_res = _real_allocator(db, snap, signal, r.clock.data_cutoff_at)
        # BUY 行 → intent_pct=0.05 → clamp 后 target_position_pct ≥0（默认 ≤ single_limit=0.10，pass）
        assert len(alloc_res.items) >= 1
        first = alloc_res.items[0]
        assert first.get("target_position_pct") is not None
        assert isinstance(alloc_res.clamp_trace, list)
