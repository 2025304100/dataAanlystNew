"""T-A4 Q6.1：Fail-Closed 组合级门禁单元测试。

覆盖：
- T-A4-C1：正式模式（production / strict_pit）单成员缺 Score → 全组合 BLOCKED
- T-A4-C2：研究模式（research）单成员缺 Score → 不阻断，只 WARN + DEGRADED_DATA
- T-A4-C3：全 PASS 场景 → composite_level=PASS，is_result_production_eligible=True
- T-A4-B0：基线前后回退（不传新参数则等价于旧行为，PASS）
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("ALEMBIC_DATABASE_URL", "")


@pytest.fixture(scope="function")
def tmp_alembic_db():
    """每个函数独立 SQLite 文件，显式跑 alembic upgrade head。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_g1_fc_")
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


# ---------------------------------------------------------------------------
# helpers: 构造 100 成员 per_member_score_avail
# ---------------------------------------------------------------------------
def _make_100_members(missing_symbol_ids: list[int] | None = None,
                      staled_symbol_ids: list[int] | None = None,
                      not_pit_safe_ids: list[int] | None = None) -> list[dict]:
    """构造 100 成员 per_member_score_avail 列表，symbol_id=1..100。"""
    missing = set(missing_symbol_ids or [])
    staled = set(staled_symbol_ids or [])
    nps = set(not_pit_safe_ids or [])
    out: list[dict] = []
    for i in range(1, 101):
        out.append({
            "symbol_id": i,
            "score_found": (i not in missing),
            "staled": (i in staled),
            "not_pit_safe": (i in nps),
        })
    return out


class TestFailClosedComposite:
    # ------------------------------------------------------------------
    # C1: 正式阻断（production / strict_pit 场景，缺 1 只 Score 42）
    # ------------------------------------------------------------------
    def test_c1_production_single_missing_blocks_all(self, tmp_alembic_db):
        """T-A4-C1a: run_mode=production 1缺/100 → block_all_members=True, BLOCKED。"""
        from app.services.decision_engine import evaluate_fail_closed_decision
        db = tmp_alembic_db
        coverage_pass = {"coverage_level": "PASS", "coverage_pct": 99.0, "expected": 100, "actual": 99}
        key_pass = {"level": "PASS", "missing_key_members": []}
        stale_pass = {"level": "PASS", "max_age_days": 0, "sla_max_age_days": 1}
        members = _make_100_members(missing_symbol_ids=[42])

        result = evaluate_fail_closed_decision(
            db,
            run_mode="production",
            coverage_result=coverage_pass,
            key_member_result=key_pass,
            staleness_result=stale_pass,
            per_member_score_avail=members,
            portfolio_id=1,
            strategy_snapshot_id="snap_c1a",
            decision_at=datetime(2025, 1, 6, 7, 0, 0),
        )

        assert result["block_all_members"] is True, "production 缺 1 只应全组合阻断"
        assert result["composite_level"] == "BLOCKED"
        assert "SINGLE_MEMBER_STALE_SCORE" in result["block_codes"], (
            f"block_codes 应含 SINGLE_MEMBER_STALE_SCORE，实际 {result['block_codes']}"
        )
        assert result["is_result_production_eligible"] is False
        # research-only 列表：正式模式下为空
        assert len(result["per_member_rejections_research_only"]) == 0, (
            "正式 BLOCKED 模式下，不使用 per_member_rejections_research_only 字段"
        )

    def test_c1_strict_pit_same_scenario_also_blocks(self, tmp_alembic_db):
        """T-A4-C1b: run_mode=strict_pit 同场景 → 同样 BLOCKED（strict 也正式）。"""
        from app.services.decision_engine import evaluate_fail_closed_decision
        db = tmp_alembic_db
        coverage_pass = {"coverage_level": "PASS", "coverage_pct": 99.0, "expected": 100, "actual": 99}
        key_pass = {"level": "PASS", "missing_key_members": []}
        stale_pass = {"level": "PASS", "max_age_days": 0, "sla_max_age_days": 1}
        members = _make_100_members(missing_symbol_ids=[42])

        result = evaluate_fail_closed_decision(
            db,
            run_mode="strict_pit",
            coverage_result=coverage_pass,
            key_member_result=key_pass,
            staleness_result=stale_pass,
            per_member_score_avail=members,
            portfolio_id=1,
            strategy_snapshot_id="snap_c1b",
            decision_at=datetime(2025, 1, 6, 7, 0, 0),
        )

        assert result["block_all_members"] is True
        assert result["composite_level"] == "BLOCKED"
        assert "SINGLE_MEMBER_STALE_SCORE" in result["block_codes"]
        assert result["is_result_production_eligible"] is False

    # ------------------------------------------------------------------
    # C2: 研究模式不阻断 + DEGRADED_DATA
    # ------------------------------------------------------------------
    def test_c2_research_no_block_but_degraded(self, tmp_alembic_db):
        """T-A4-C2: run_mode=research 1缺/100 → 不阻断，WARN，仅 1 条 rejection 给 42。"""
        from app.services.decision_engine import evaluate_fail_closed_decision
        db = tmp_alembic_db
        coverage_pass = {"coverage_level": "PASS", "coverage_pct": 99.0, "expected": 100, "actual": 99}
        key_pass = {"level": "PASS", "missing_key_members": []}
        stale_pass = {"level": "PASS", "max_age_days": 0, "sla_max_age_days": 1}
        members = _make_100_members(missing_symbol_ids=[42])

        result = evaluate_fail_closed_decision(
            db,
            run_mode="research",
            coverage_result=coverage_pass,
            key_member_result=key_pass,
            staleness_result=stale_pass,
            per_member_score_avail=members,
            portfolio_id=1,
            strategy_snapshot_id="snap_c2",
            decision_at=datetime(2025, 1, 6, 7, 0, 0),
        )

        # 不阻断
        assert result["block_all_members"] is False, "research 不应全组合阻断"
        assert result["composite_level"] == "WARN"
        # per_member_rejections_research_only 应只有 symbol_id=42 一条
        rejects = result["per_member_rejections_research_only"]
        assert len(rejects) == 1, f"只应 1 条 rejection，实际 {len(rejects)}"
        assert rejects[0]["symbol_id"] == 42
        assert rejects[0]["action"] == "REJECTED"
        # degraded_warnings 至少 1 条 code=DEGRADED_DATA
        warns = result["degraded_warnings"]
        assert len(warns) >= 1, f"至少 1 条 DEGRADED warning，实际 {len(warns)}"
        assert any(w.get("code") == "DEGRADED_DATA" for w in warns), (
            f"degraded_warnings 中至少一条 code=DEGRADED_DATA，实际 {[w.get('code') for w in warns]}"
        )
        # 生产不合格
        assert result["is_result_production_eligible"] is False
        # block_codes / block_reasons 在 research 下为空
        assert result["block_codes"] == []
        assert result["block_reasons"] == []

    # ------------------------------------------------------------------
    # C3: 全 PASS 场景
    # ------------------------------------------------------------------
    def test_c3_all_pass_production_eligible(self, tmp_alembic_db):
        """T-A4-C3: 100/100 Score, coverage 98% PASS, key PASS, staleness PASS → 全 PASS。"""
        from app.services.decision_engine import evaluate_fail_closed_decision
        db = tmp_alembic_db
        coverage_pass = {"coverage_level": "PASS", "coverage_pct": 98.0, "expected": 100, "actual": 98}
        key_pass = {"level": "PASS", "missing_key_members": []}
        stale_pass = {"level": "PASS", "max_age_days": 0, "sla_max_age_days": 1}
        members = _make_100_members(missing_symbol_ids=[])  # 全有 Score

        result = evaluate_fail_closed_decision(
            db,
            run_mode="production",
            coverage_result=coverage_pass,
            key_member_result=key_pass,
            staleness_result=stale_pass,
            per_member_score_avail=members,
            portfolio_id=1,
            strategy_snapshot_id="snap_c3",
            decision_at=datetime(2025, 1, 6, 7, 0, 0),
        )

        assert result["composite_level"] == "PASS"
        assert result["block_all_members"] is False
        assert result["is_result_production_eligible"] is True
        assert result["block_codes"] == [], f"0 block_code 期望，实际 {result['block_codes']}"
        assert result["block_reasons"] == []
        assert result["degraded_warnings"] == []
        assert result["per_member_rejections_research_only"] == []

    # ------------------------------------------------------------------
    # B0: 基线回退（旧 evaluate 不传 coverage_result 等 → 行为不变 PASS）
    # ------------------------------------------------------------------
    def test_b0_backward_compat_without_fail_closed_params(self, tmp_alembic_db):
        """T-A4-B0: 不传 4 个新参数 → EvaluateResult 默认 is_prod_eligible=True，
        fail_closed_result=None，等价于旧行为。"""
        from app.services.decision_engine import DecisionEngine
        db = tmp_alembic_db

        def _make_portfolio(db, **kw):
            from app.models.portfolio import Portfolio
            p = Portfolio(
                name=kw.pop("name", "B0 Test"),
                account_type=kw.pop("account_type", "sim"),
                asset_scope=kw.pop("asset_scope", "mixed"),
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
            db.add(p); db.flush()
            return p

        def _make_symbol(db, code="TEST", name="Test", market="SSE"):
            from app.models.symbol import Symbol
            s = Symbol(symbol=code, name=name, market=market, asset_type="STOCK", is_active=1)
            for attr in ("board", "industry", "theme"):
                if not hasattr(s, attr):
                    setattr(s, attr, None)
            db.add(s); db.flush(); return s

        def _set_active_model(db, model_run_id):
            from app.models.factor_runtime import FactorRuntimeState
            s = db.get(FactorRuntimeState, 1)
            if s is None:
                s = FactorRuntimeState(id=1, weight_mode="manual", active_model_run_id=model_run_id,
                                       updated_by="qa", version=1)
                db.add(s)
            else:
                s.weight_mode = "manual"; s.active_model_run_id = model_run_id; s.version += 1
            db.flush(); return s

        def _save_and_apply(db, portfolio_id, fmr_id, run_mode="research"):
            from app.services.factor_usage_service import save_and_apply_usage
            from app.schemas.decision_engine import FactorUsageBindRequest
            return save_and_apply_usage(
                db, portfolio_id,
                FactorUsageBindRequest(factor_model_run_id=fmr_id, run_mode=run_mode, pit_mode="best_effort"),
                "qa_user",
            )

        from app.models.portfolio_member import PortfolioMember
        p = _make_portfolio(db); _set_active_model(db, "fmr_TA4_B0")
        s1 = _make_symbol(db, "B0S1"); s2 = _make_symbol(db, "B0S2")
        for sym in (s1, s2):
            db.add(PortfolioMember(portfolio_id=p.id, symbol_id=sym.id, status="active"))
        resp = _save_and_apply(db, p.id, "fmr_TA4_B0"); db.commit()

        engine = DecisionEngine()
        result = engine.evaluate(
            db, portfolio_id=p.id,
            strategy_snapshot_id=resp.strategy_snapshot_id,
            trade_date=date(2025, 1, 2), run_type="research_preflight",
            dry_run=True,
            # 显式不传 coverage_result / key_member_result / staleness_result / per_member_score_avail
        )

        # 默认值（向后兼容）
        assert result.is_result_production_eligible is True, (
            "不传 fail-closed 参数时，默认应 True 兼容旧路径"
        )
        assert result.fail_closed_result is None
        # blocking_status 是旧逻辑结果（这里 skeleton 无 Score，可能 READY 或 DATA_INCOMPLETE），
        # 关键在于：不因为新字段缺失而崩溃
        assert result.decision_run_id and len(result.decision_run_id) == 64
        assert len(result.evidence) == 2
