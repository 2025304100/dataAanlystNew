"""T-A6 Q6.3：禁止 legacy fallback 在生产/正式链路使用。

覆盖：
- T-A6-C1：生产 + allow_legacy_fallback=True → 抛错码 FALLBACK_NOT_ALLOWED_IN_PRODUCTION
- T-A6-C2：研究模式 + allow_legacy_fallback=True + Score 缺失 → legacy 留痕
- T-A6-C3：生产不传 allow_legacy_fallback → 默认 False，无 legacy，正常跑
- B0 基线：evaluate 不传 allow_legacy_fallback 新参数 → 不抛，旧路径不崩
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
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_g1_legacy_")
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


def _make_100_members(missing_symbol_ids: list[int] | None = None,
                      staled_symbol_ids: list[int] | None = None,
                      not_pit_safe_ids: list[int] | None = None,
                      use_legacy_fallback_ids: list[int] | None = None) -> list[dict]:
    missing = set(missing_symbol_ids or [])
    staled = set(staled_symbol_ids or [])
    nps = set(not_pit_safe_ids or [])
    legacy_ids = set(use_legacy_fallback_ids or [])
    out: list[dict] = []
    for i in range(1, 101):
        out.append({
            "symbol_id": i,
            "score_found": (i not in missing),
            "staled": (i in staled),
            "not_pit_safe": (i in nps),
            "use_legacy_fallback": (i in legacy_ids),
        })
    return out


class TestLegacyFallbackBlock:
    # ------------------------------------------------------------------
    # C1: 生产 + allow_legacy_fallback=True → 抛错码 FALLBACK_NOT_ALLOWED_IN_PRODUCTION
    # ------------------------------------------------------------------
    def test_c1_production_allow_legacy_raises_fallback_not_allowed(self, tmp_alembic_db):
        """T-A6-C1a: run_mode=production, allow_legacy_fallback=True
        → validate_allow_legacy_fallback raise ValueError(meta error_code=FALLBACK_NOT_ALLOWED_IN_PRODUCTION)
        """
        from app.services.decision_engine import validate_allow_legacy_fallback

        with pytest.raises(ValueError) as exc_info:
            validate_allow_legacy_fallback(
                run_mode="production",
                allow_legacy_fallback=True,
            )

        meta = getattr(exc_info.value, "meta", None)
        assert meta is not None, "ValueError 应包含 meta 属性"
        assert meta.get("error_code") == "FALLBACK_NOT_ALLOWED_IN_PRODUCTION", (
            f"error_code 应为 FALLBACK_NOT_ALLOWED_IN_PRODUCTION，实际 {meta.get('error_code')}"
        )

    def test_c1_strict_pit_allow_legacy_also_raises(self, tmp_alembic_db):
        """T-A6-C1b: run_mode=strict_pit (也是正式), allow_legacy_fallback=True
        → 同样抛相同码
        """
        from app.services.decision_engine import validate_allow_legacy_fallback

        with pytest.raises(ValueError) as exc_info:
            validate_allow_legacy_fallback(
                run_mode="strict_pit",
                allow_legacy_fallback=True,
            )

        meta = getattr(exc_info.value, "meta", None)
        assert meta is not None
        assert meta.get("error_code") == "FALLBACK_NOT_ALLOWED_IN_PRODUCTION"

    def test_c1_evaluate_fail_closed_also_blocks_production_legacy(self, tmp_alembic_db):
        """T-A6-C1c: evaluate_fail_closed_decision 双重保险 — production + legacy=True → 也抛。"""
        from app.services.decision_engine import evaluate_fail_closed_decision
        db = tmp_alembic_db
        coverage_pass = {"coverage_level": "PASS", "coverage_pct": 98.0, "expected": 100, "actual": 98}
        key_pass = {"level": "PASS", "missing_key_members": []}
        stale_pass = {"level": "PASS", "max_age_days": 0, "sla_max_age_days": 1}
        members = _make_100_members()

        with pytest.raises(ValueError) as exc_info:
            evaluate_fail_closed_decision(
                db,
                run_mode="production",
                coverage_result=coverage_pass,
                key_member_result=key_pass,
                staleness_result=stale_pass,
                per_member_score_avail=members,
                portfolio_id=1,
                strategy_snapshot_id="snap_c1c",
                decision_at=datetime(2025, 1, 6, 7, 0, 0),
                allow_legacy_fallback=True,
            )

        meta = getattr(exc_info.value, "meta", None)
        assert meta is not None
        assert meta.get("error_code") == "FALLBACK_NOT_ALLOWED_IN_PRODUCTION"


class TestLegacyFallbackResearchTrace:
    # ------------------------------------------------------------------
    # C2: 研究模式 + allow_legacy_fallback=True + Score 缺失 → legacy 留痕
    # ------------------------------------------------------------------
    def test_c2_research_legacy_used_traces_warning_and_extra_info(self, tmp_alembic_db):
        """T-A6-C2: run_mode=research，allow_legacy_fallback=True，
        per_member_score_avail 有 1 只 score_found=False 但 use_legacy_fallback=True
        → degraded_warnings 有 FALLBACK_USED，per_member_extra_info 标记，
        per_member_rejections_research_only 不应有 42，
        is_result_production_eligible=False。
        """
        from app.services.decision_engine import evaluate_fail_closed_decision
        db = tmp_alembic_db
        coverage_pass = {"coverage_level": "PASS", "coverage_pct": 99.0, "expected": 100, "actual": 99}
        key_pass = {"level": "PASS", "missing_key_members": []}
        stale_pass = {"level": "PASS", "max_age_days": 0, "sla_max_age_days": 1}
        # symbol_id=42 缺 Score，但启用了 use_legacy_fallback=True
        members = _make_100_members(
            missing_symbol_ids=[42],
            use_legacy_fallback_ids=[42],
        )

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
            allow_legacy_fallback=True,
        )

        # degraded_warnings 有一条 FALLBACK_USED，symbol_id=42，subtype=LEGACY_SCORE_FALLBACK
        warns = result["degraded_warnings"]
        fb_warns = [
            w for w in warns
            if w.get("code") == "FALLBACK_USED"
            and w.get("symbol_id") == 42
            and w.get("subtype") == "LEGACY_SCORE_FALLBACK"
        ]
        assert len(fb_warns) >= 1, (
            f"应有 FALLBACK_USED warning（symbol_id=42, LEGACY_SCORE_FALLBACK），"
            f"实际 degraded_warnings={warns}"
        )

        # per_member_extra_info 中对应 symbol_id=42 有 legacy_fallback_used=True
        extra = result.get("per_member_extra_info", [])
        sym42_extra = [e for e in extra if e.get("symbol_id") == 42]
        assert len(sym42_extra) >= 1, (
            f"per_member_extra_info 应有 symbol_id=42，实际 {extra}"
        )
        assert sym42_extra[0].get("legacy_fallback_used") is True, (
            f"symbol_id=42 的 legacy_fallback_used 应为 True，实际 {sym42_extra[0]}"
        )

        # per_member_rejections_research_only 不应出现 42（legacy 成功修复了，不算 REJECTED）
        rejects = result["per_member_rejections_research_only"]
        reject_42 = [r for r in rejects if r.get("symbol_id") == 42]
        assert len(reject_42) == 0, (
            f"symbol_id=42 不应在 per_member_rejections_research_only（legacy 修复），"
            f"实际 {rejects}"
        )

        # is_result_production_eligible=False（使用了 fallback，研究结果不入生产）
        assert result["is_result_production_eligible"] is False, (
            "使用了 legacy fallback → eligible=False"
        )

        # 不阻断（研究模式）
        assert result["block_all_members"] is False


class TestProductionDefaultNoLegacy:
    # ------------------------------------------------------------------
    # C3: 生产不传 allow_legacy_fallback → 默认 False，无 legacy，正常跑
    # ------------------------------------------------------------------
    def test_c3_production_default_none_no_legacy_runs_ok(self, tmp_alembic_db):
        """T-A6-C3: run_mode=production，allow_legacy_fallback=None（默认）
        → validate 不抛，evaluate_fail_closed_decision 全 PASS 时 eligible=True，warnings=0。
        """
        from app.services.decision_engine import (
            validate_allow_legacy_fallback,
            evaluate_fail_closed_decision,
        )
        db = tmp_alembic_db

        # validate 不抛
        validate_allow_legacy_fallback(
            run_mode="production",
            allow_legacy_fallback=None,
        )
        validate_allow_legacy_fallback(
            run_mode="production",
            allow_legacy_fallback=False,
        )

        # evaluate_fail_closed_decision 全 PASS 场景
        coverage_pass = {"coverage_level": "PASS", "coverage_pct": 98.0, "expected": 100, "actual": 98}
        key_pass = {"level": "PASS", "missing_key_members": []}
        stale_pass = {"level": "PASS", "max_age_days": 0, "sla_max_age_days": 1}
        members = _make_100_members()

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
            allow_legacy_fallback=None,
        )

        assert result["composite_level"] == "PASS"
        assert result["block_all_members"] is False
        assert result["is_result_production_eligible"] is True
        assert result["block_codes"] == []
        assert result["block_reasons"] == []
        assert result["degraded_warnings"] == []
        assert result["per_member_rejections_research_only"] == []
        assert result.get("per_member_extra_info", []) == []


class TestB0BackwardCompat:
    # ------------------------------------------------------------------
    # B0: 基线 + 向后兼容 — evaluate 不传 allow_legacy_fallback → 不崩
    # ------------------------------------------------------------------
    def test_b0_evaluate_without_legacy_param_backward_compat(self, tmp_alembic_db):
        """B0: DecisionEngine.evaluate() 不传 allow_legacy_fallback 新参数
        → 不抛，旧路径不崩（DecisionEngine 原 8 tests GREEN 基线）。
        """
        from app.services.decision_engine import DecisionEngine
        db = tmp_alembic_db

        def _make_portfolio(db, **kw):
            from app.models.portfolio import Portfolio
            p = Portfolio(
                name=kw.pop("name", "T-A6 B0 Test"),
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
        p = _make_portfolio(db); _set_active_model(db, "fmr_TA6_B0")
        s1 = _make_symbol(db, "TA6B1"); s2 = _make_symbol(db, "TA6B2")
        for sym in (s1, s2):
            db.add(PortfolioMember(portfolio_id=p.id, symbol_id=sym.id, status="active"))
        resp = _save_and_apply(db, p.id, "fmr_TA6_B0"); db.commit()

        engine = DecisionEngine()
        # 显式不传 allow_legacy_fallback，也不传 coverage_result 等
        result = engine.evaluate(
            db, portfolio_id=p.id,
            strategy_snapshot_id=resp.strategy_snapshot_id,
            trade_date=date(2025, 1, 2), run_type="research_preflight",
            dry_run=True,
        )

        assert result.is_result_production_eligible is True, (
            "不传 fail-closed 参数时，默认应 True 兼容旧路径"
        )
        assert result.fail_closed_result is None
        assert result.decision_run_id and len(result.decision_run_id) == 64
        # 关键：不因为 allow_legacy_fallback 缺失而崩溃
        assert len(result.evidence) == 2
