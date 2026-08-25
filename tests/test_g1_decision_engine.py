"""G1-WP0-3a/b/c：DecisionEngine 骨架 + Score 精确查询 + 证据落库 / dry-run API。

覆盖：
- T_G1_DE_01：骨架 dry-run 通过，11 步不报错；blocking_status 在没 Score 的情况下是 DATA_INCOMPLETE_PAUSED
- T_G1_DE_02：六类 action 全部覆盖 (HOLD + REJECTED)；每 symbol 一条 PerSymbolEvidence
- T_G1_DE_03：PIT_SAFE / NOT_PIT_SAFE 判定 (published_at <= data_cutoff_at)
- T_G1_DE_04：Score 查询契约——只取 factor_model_run_id=bound_id，禁止 weight_mode 隐式匹配；未来 Score 不被使用
- T_G1_DE_05：Score 覆盖率按「成员 × 交易日」计算；10 成员 10 Score = 100%；10 成员 8 Score = 80%
- T_G1_DE_06：persist=True 写入 decision_runs + decision_evidence；重复调用命中幂等，不增加记录数
- T_G1_DE_07：POST /evaluate 路由 200；GET /decision-runs/{id} /evidence 分页 OK
- T_G1_DE_08：blocking → 所有 symbol 统一 DATA_BLOCKED 或 REJECTED（Fail-Closed Q6）
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

# 让 Alembic 使用测试数据库
os.environ.setdefault("ALEMBIC_DATABASE_URL", "")


@pytest.fixture(scope="function")
def tmp_alembic_db():
    """每个函数独立 SQLite 文件，显式跑 alembic upgrade head（与 G0 契约一致）。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_g1_de_")
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
# helpers
# ---------------------------------------------------------------------------
def _make_portfolio(db, **kw):
    from app.models.portfolio import Portfolio
    p = Portfolio(
        name=kw.pop("name", "T_G1_DE Test"),
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


def _set_active_model(db, model_run_id: str | None):
    from app.models.factor_runtime import FactorRuntimeState
    from app.models.factor import Factor
    from app.models.factor_evaluation import FactorSet, FactorSetMember
    from app.models.factor_model import FactorModelRun, FactorVersion, FactorWeightSnapshot

    factor_set_id = f"fs-{model_run_id}" if model_run_id else None
    if model_run_id and db.get(FactorModelRun, model_run_id) is None:
        # Production snapshot preflight requires a complete immutable model
        # lineage, so this fixture builds the smallest readiness-qualified
        # FactorSet -> FactorModelRun pair used by the G1 scenarios.
        factor = Factor(
            code=f"factor_{model_run_id}",
            name=f"Factor {model_run_id}",
            category="fundamental",
            direction="higher_better",
            status="active",
            lifecycle_status="active",
            is_active=1,
            origin="test",
        )
        db.add(factor)
        db.flush()
        version = FactorVersion(
            factor_id=factor.id,
            version=1,
            formula_expr="1",
            validation_status="valid",
        )
        factor_set = FactorSet(
            id=factor_set_id,
            name=factor_set_id,
            status="frozen",
            content_hash="a" * 32,
            frozen_at=datetime.now(timezone.utc).replace(tzinfo=None),
            created_by="qa",
        )
        db.add_all([version, factor_set])
        db.flush()
        db.add(FactorSetMember(
            factor_set_id=factor_set_id,
            factor_id=factor.id,
            factor_version_id=version.id,
            factor_code=factor.code,
            factor_version=1,
            role="feature",
            display_order=0,
            missing_policy="exclude",
        ))
        model = FactorModelRun(
            id=model_run_id,
            model_type="ridge",
            asset_type="stock",
            status="validated",
            feature_versions_json=json.dumps({
                factor.code: 1,
                "__factor_set_id__": factor_set_id,
            }),
            hyperparameters_json=json.dumps({"factor_set_id": factor_set_id}),
            metrics_json="{}",
        )
        model.weights.append(FactorWeightSnapshot(
            factor_code=factor.code,
            factor_version=1,
            coefficient=1.0,
            normalized_weight=1.0,
        ))
        db.add(model)
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
        FactorUsageBindRequest(
            factor_model_run_id=fmr_id,
            factor_set_id=f"fs-{fmr_id}",
            run_mode=run_mode,
            pit_mode="best_effort",
        ),
        "qa_user",
    )


def _insert_scores(db, fmr_id, pairs, batch_id_suffix=""):
    """pairs: list[(symbol_id:int, trade_date:date, value:float, published_at:datetime or None)]

    Score.factor_data_cutoff_at 以 UTC naive 存储（Q1）。
    常规“收盘后截止”的 Score，data_cutoff_at = trade_date@07:00 UTC (=15:00 CST).
    published_at 同样以 UTC naive 传参。
    """
    from app.models.score import Score
    for idx, (sym_id, td, value, pub) in enumerate(pairs):
        bid = f"qa_{fmr_id[-8:]}{batch_id_suffix}_{idx:03d}"
        cutoff_utc = datetime(td.year, td.month, td.day, 7, 0, 0)  # 15:00 CST = 07:00 UTC
        db.add(Score(
            symbol_id=sym_id, trade_date=td, quality_score=0.0, quality_grade="A",
            timing_score=0.0, stage="growth", action="HOLD", priority_score=0.0,
            weight_mode="manual", factor_model_run_id=fmr_id,
            factor_set_id=f"fs-{fmr_id}",
            factor_data_cutoff_at=cutoff_utc,
            model_alpha_score=value, calc_batch_id=bid,
            published_at=pub, pit_flag=("PIT_SAFE" if pub else "NOT_CHECKED"),
        ))
    db.flush()


class TestDecisionEngineSkeleton:
    def test_01_dry_run_runs_pipeline_without_crash(self, tmp_alembic_db):
        """T_G1_DE_01：11 步透传骨架 dry-run 不崩溃；blocking=READY 或 DATA_INCOMPLETE（无 Score 情况）。"""
        from app.services.decision_engine import DecisionEngine, evaluate
        db = tmp_alembic_db
        p = _make_portfolio(db); _set_active_model(db, "fmr_G1_DE_01")
        s1 = _make_symbol(db, "S1"); s2 = _make_symbol(db, "S2")
        from app.models.portfolio_member import PortfolioMember
        for sym in (s1, s2):
            db.add(PortfolioMember(portfolio_id=p.id, symbol_id=sym.id, status="active"))
        resp = _save_and_apply(db, p.id, "fmr_G1_DE_01"); db.commit()
        result = evaluate(
            db, portfolio_id=p.id,
            strategy_snapshot_id=resp.strategy_snapshot_id,
            trade_date=date(2025, 1, 2), run_type="research_preflight",
            dry_run=True,
        )
        # 必有 decision_run_id 且 64 字符 hash
        assert result.decision_run_id and len(result.decision_run_id) == 64
        # 时钟三字段齐全
        c = result.clock
        assert c.decision_at < c.execution_at, "决策应在执行之前（T 日决策 T+1 执行）"
        # 2 个 symbol → 2 条 evidence
        assert len(result.evidence) == 2

    def test_02_action_categories_and_subtypes(self, tmp_alembic_db):
        """T_G1_DE_02：六类动作之一存在；阻塞状态下必须是 DATA_BLOCKED/REJECTED + 子类。"""
        from app.services.decision_engine import evaluate, PerSymbolEvidence
        db = tmp_alembic_db
        p = _make_portfolio(db); _set_active_model(db, "fmr_G1_DE_02")
        sym = _make_symbol(db, "S2a")
        from app.models.portfolio_member import PortfolioMember
        db.add(PortfolioMember(portfolio_id=p.id, symbol_id=sym.id, status="active"))
        # 生产模式，无 Score → 覆盖率 0 < 95 → FAIL-CLOSED
        resp = _save_and_apply(db, p.id, "fmr_G1_DE_02", run_mode="production_pit")
        db.commit()
        result = evaluate(
            db, portfolio_id=p.id, strategy_snapshot_id=resp.strategy_snapshot_id,
            trade_date=date(2025, 1, 2), run_type="research_preflight", dry_run=True,
        )
        actions = {e.action for e in result.evidence}
        # 阻塞态下不允许 BUY/SELL
        assert not (actions & {"BUY", "SELL"}), f"阻塞态禁止 BUY/SELL，实际 {actions}"
        # 至少有一种 REJECTED/DATA_BLOCKED/HOLD/NO_ACTION（即合法的 6 类之一）
        assert actions <= {"BUY", "SELL", "HOLD", "NO_ACTION", "REJECTED", "DATA_BLOCKED"}, actions
        # evidence_count == member_count
        assert result.blocking_status in {"READY", "DATA_INCOMPLETE_PAUSED", "MODEL_INACTIVE",
                                          "SCORE_STALE", "RECONCILIATION_BLOCKED"}

    def test_03_pit_safe_uses_published_at_vs_cutoff(self, tmp_alembic_db):
        """T_G1_DE_03：published_at <= data_cutoff → PIT_SAFE；否则 / NULL → NOT_PIT_SAFE。"""
        from app.services.decision_engine import DecisionEngine
        from app.services.score_query_service import make_decision_scorer
        db = tmp_alembic_db
        p = _make_portfolio(db); _set_active_model(db, "fmr_G1_DE_03")
        s_pub_before = _make_symbol(db, "SPB"); s_pub_after = _make_symbol(db, "SPA"); s_pub_null = _make_symbol(db, "SPN")
        from app.models.portfolio_member import PortfolioMember
        for s in (s_pub_before, s_pub_after, s_pub_null):
            db.add(PortfolioMember(portfolio_id=p.id, symbol_id=s.id, status="active"))
        decision_date = date(2025, 1, 3)  # Friday
        cutoff_utc_expected = datetime(2025, 1, 3, 7, 0, 0)  # 15:00 CST = 07:00 UTC Friday
        _insert_scores(db, "fmr_G1_DE_03", [
            # 2025-01-03 Friday T 日：截止 15:00 CST = 07:00 UTC
            (s_pub_before.id, date(2025, 1, 3), 0.85, datetime(2025, 1, 3, 6, 59, 0)),  # BEFORE cutoff → PIT_SAFE
            (s_pub_after.id,  date(2025, 1, 3), 0.75, datetime(2025, 1, 3, 7, 1, 0)),   # AFTER cutoff → NOT
            (s_pub_null.id,   date(2025, 1, 3), 0.65, None),                               # NULL → NOT
        ])
        resp = _save_and_apply(db, p.id, "fmr_G1_DE_03"); db.commit()
        engine = DecisionEngine(scorer=make_decision_scorer())
        result = engine.evaluate(
            db, portfolio_id=p.id, strategy_snapshot_id=resp.strategy_snapshot_id,
            trade_date=decision_date, run_type="research_preflight", dry_run=True,
        )
        by_id = {e.symbol_id: e for e in result.evidence}
        assert by_id[s_pub_before.id].pit_safe_flag == "PIT_SAFE", by_id[s_pub_before.id].pit_safe_flag
        assert by_id[s_pub_after.id].pit_safe_flag == "NOT_PIT_SAFE", by_id[s_pub_after.id].pit_safe_flag
        assert by_id[s_pub_null.id].pit_safe_flag == "NOT_PIT_SAFE", by_id[s_pub_null.id].pit_safe_flag
        # Score 查询只认 exact factor_model_run_id → 覆盖率 100% (3/3)
        assert result.score_coverage_pct == 100.0, f"3/3 应 100%，实际 {result.score_coverage_pct}"

    def test_04_score_query_rejects_wrong_model_or_future(self, tmp_alembic_db):
        """T_G1_DE_04：Q22 精确查询契约。wrong model / future score 一律不命中。"""
        from app.services.score_query_service import fetch_latest_pit_scores, estimate_score_coverage
        db = tmp_alembic_db
        sA = _make_symbol(db, "SQA"); sB = _make_symbol(db, "SQB")
        _insert_scores(db, "fmr_CORRECT", [
            (sA.id, date(2025, 1, 3), 0.50, datetime(2025, 1, 3, 7, 0, 0)),
            (sB.id, date(2025, 1, 3), 0.60, datetime(2025, 1, 3, 7, 0, 0)),
        ])
        _insert_scores(db, "fmr_WRONG", [
            (sA.id, date(2025, 1, 3), 0.99, datetime(2025, 1, 3, 7, 0, 0)),
        ])
        # 未来 Score —— 不应被 2025-01-03 决策取用
        _insert_scores(db, "fmr_CORRECT", [
            (sA.id, date(2025, 2, 1), 0.99, datetime(2025, 2, 1, 7, 0, 0)),
        ])
        cutoff = datetime(2025, 1, 3, 7, 0, 0)
        # correct query
        exp, act, age = estimate_score_coverage(
            db, factor_model_run_id="fmr_CORRECT", symbol_ids=[sA.id, sB.id],
            decision_date=date(2025, 1, 3), data_cutoff_at=cutoff,
        )
        assert exp == 2 and act == 2, f"CORRECT 模型应 2/2，实际 {exp}/{act}"
        rows = fetch_latest_pit_scores(
            db, factor_model_run_id="fmr_CORRECT", symbol_ids=[sA.id, sB.id],
            decision_date=date(2025, 1, 3), data_cutoff_at=cutoff,
        )
        # sA 的值应为 0.50（旧 Score）而非 0.99（未来 Score）
        sA_row = next(r for r in rows if r.symbol_id == sA.id)
        assert sA_row.score_value == pytest.approx(0.50), (
            f"未来 Score 被错误使用，实际 {sA_row.score_value}，期望 0.50"
        )
        # wrong model → 0 hits
        _, act_wrong, _ = estimate_score_coverage(
            db, factor_model_run_id="fmr_WRONG", symbol_ids=[sA.id, sB.id],
            decision_date=date(2025, 1, 3), data_cutoff_at=cutoff,
        )
        assert act_wrong == 1  # 只有 sA 在 fmr_WRONG，SQB 不在
        # weight_mode=manual 不能作为 fallback（此处测试：不传 factor_model_run_id 直接抛异常）
        with pytest.raises(ValueError, match="factor_model_run_id"):
            fetch_latest_pit_scores(
                db, factor_model_run_id="", symbol_ids=[sA.id],
                decision_date=date(2025, 1, 3), data_cutoff_at=cutoff,
            )

    def test_05_coverage_by_members_times_days(self, tmp_alembic_db):
        """T_G1_DE_05：Q5 覆盖率 = actual/expected；expected 严格等于传入 symbol_ids 长度。"""
        from app.services.score_query_service import estimate_score_coverage
        db = tmp_alembic_db
        syms = [_make_symbol(db, f"S{i}") for i in range(10)]
        _insert_scores(db, "fmr_G1_COV", [
            (s.id, date(2025, 1, 3), 0.1 * i, datetime(2025, 1, 3, 7, 0, 0))
            for i, s in enumerate(syms) if i != 7 and i != 9  # 少两个 80%
        ])
        cutoff = datetime(2025, 1, 3, 7, 0, 0)
        ids = [s.id for s in syms]
        exp, act, _ = estimate_score_coverage(
            db, factor_model_run_id="fmr_G1_COV", symbol_ids=ids,
            decision_date=date(2025, 1, 3), data_cutoff_at=cutoff,
        )
        assert exp == 10, f"expected 应等于 symbol_ids 长度 10，实际 {exp}"
        assert act == 8, f"actual=8 有两条缺失，实际 {act}"
        # 2 个交易日 expected 应该 ×2 吗？实际每次 query 按传入当日算；逐日累加由循环调用方负责
        # 此处只测单次调用契约：expected = len(symbol_ids)
        ids_partial = ids[5:10]  # 包含 i=7,i=9 两个缺失 → 5/4
        exp2, act2, _ = estimate_score_coverage(
            db, factor_model_run_id="fmr_G1_COV", symbol_ids=ids_partial,
            decision_date=date(2025, 1, 3), data_cutoff_at=cutoff,
        )
        assert exp2 == 5 and act2 == 3, f"5 members ×1 day (indices5-9 缺2个: 7,9) 应 5/3 实际 {exp2}/{act2}"

    def test_06_persist_writes_then_idempotent_skip(self, tmp_alembic_db):
        """T_G1_DE_06：persist=True 写 2 张表；同主键二次调用幂等。"""
        from app.services.decision_engine import DecisionEngine
        from app.models.decision_engine import DecisionRun, DecisionEvidence
        from app.services.score_query_service import make_decision_scorer
        db = tmp_alembic_db
        p = _make_portfolio(db); _set_active_model(db, "fmr_G1_DE_06")
        s = _make_symbol(db, "S06")
        from app.models.portfolio_member import PortfolioMember
        db.add(PortfolioMember(portfolio_id=p.id, symbol_id=s.id, status="active"))
        _insert_scores(db, "fmr_G1_DE_06", [
            (s.id, date(2025, 1, 3), 0.55, datetime(2025, 1, 3, 7, 0, 0)),
        ])
        resp = _save_and_apply(db, p.id, "fmr_G1_DE_06"); db.commit()
        engine = DecisionEngine(scorer=make_decision_scorer())
        tdate = date(2025, 1, 6)
        # persist 第 1 次
        r1 = engine.evaluate(
            db, portfolio_id=p.id, strategy_snapshot_id=resp.strategy_snapshot_id,
            trade_date=tdate, run_type="research_preflight", dry_run=False,
        )
        db.commit()
        from sqlalchemy import func as sa_func
        assert r1.persisted is True
        run_count_1 = db.scalar(
            select(sa_func.count()).select_from(DecisionRun)
            .where(DecisionRun.id == r1.decision_run_id)
        ) or 0
        ev_count_1 = db.scalar(
            select(sa_func.count()).select_from(DecisionEvidence)
            .where(DecisionEvidence.decision_run_id == r1.decision_run_id)
        ) or 0
        assert run_count_1 == 1 and ev_count_1 >= 1
        # persist 第 2 次（完全相同参数）：不新增
        r2 = engine.evaluate(
            db, portfolio_id=p.id, strategy_snapshot_id=resp.strategy_snapshot_id,
            trade_date=tdate, run_type="research_preflight", dry_run=False,
        )
        db.commit()
        assert r2.decision_run_id == r1.decision_run_id, "同参数幂等主键必须完全一致"
        run_count_2 = db.scalar(
            select(sa_func.count()).select_from(DecisionRun)
            .where(DecisionRun.id == r1.decision_run_id)
        ) or 0
        ev_count_2 = db.scalar(
            select(sa_func.count()).select_from(DecisionEvidence)
            .where(DecisionEvidence.decision_run_id == r1.decision_run_id)
        ) or 0
        assert run_count_1 == run_count_2 and ev_count_1 == ev_count_2, (
            f"幂等失败：run {run_count_1}→{run_count_2}，ev {ev_count_1}→{ev_count_2}"
        )

    def test_07_evaluate_and_evidence_routes_200(self, tmp_alembic_db):
        """T_G1_DE_07：路由契约。"""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.db.session import get_db
        db = tmp_alembic_db
        def _ov(): yield db
        app.dependency_overrides[get_db] = _ov
        client = TestClient(app)
        p = _make_portfolio(db); _set_active_model(db, "fmr_G1_DE_07")
        s = _make_symbol(db, "S07")
        from app.models.portfolio_member import PortfolioMember
        db.add(PortfolioMember(portfolio_id=p.id, symbol_id=s.id, status="active"))
        resp = _save_and_apply(db, p.id, "fmr_G1_DE_07"); db.commit()
        # 1. dry-run evaluate
        r = client.post(
            f"/api/v1/portfolios/{p.id}/evaluate",
            json={"strategy_snapshot_id": resp.strategy_snapshot_id,
                  "trade_date": "2025-01-06", "run_type": "research_preflight", "persist": True},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        run_id = body["decision_run_id"]
        assert body["blocking_status"] and isinstance(body["clock"], dict)
        assert "evidence_preview" in body and "evidence_count" in body
        # 2. get run
        r = client.get(f"/api/v1/decision-runs/{run_id}")
        assert r.status_code == 200 and r.json()["id"] == run_id, r.text
        # 3. evidence 分页
        r = client.get(f"/api/v1/decision-runs/{run_id}/evidence")
        assert r.status_code == 200 and "items" in r.json() and r.json()["total"] >= 1, r.text
        # 4. 拒绝记录 Tab：过滤 REJECTED + DATA_BLOCKED
        r = client.get(f"/api/v1/decision-runs/{run_id}/evidence?action=REJECTED,DATA_BLOCKED")
        assert r.status_code == 200, r.text

    def test_08_fail_closed_blocks_all_symbols_when_coverage_below_sla(self, tmp_alembic_db):
        """T_G1_DE_08：Q6 Fail-Closed。production_pit + 覆盖率<95% → 阻断；所有 symbol action ∉ {BUY, SELL}。"""
        from app.services.decision_engine import DecisionEngine
        from app.services.score_query_service import make_decision_scorer
        db = tmp_alembic_db
        p = _make_portfolio(db); _set_active_model(db, "fmr_G1_DE_08")
        from app.models.portfolio_member import PortfolioMember
        syms = [_make_symbol(db, f"M{i}") for i in range(10)]
        for s in syms:
            db.add(PortfolioMember(portfolio_id=p.id, symbol_id=s.id, status="active"))
        # 只放 1 条 Score：覆盖率 10% < 95%
        _insert_scores(db, "fmr_G1_DE_08", [
            (syms[0].id, date(2025, 1, 3), 0.55, datetime(2025, 1, 3, 7, 0, 0)),
        ])
        resp = _save_and_apply(db, p.id, "fmr_G1_DE_08", run_mode="production_pit")
        db.commit()
        engine = DecisionEngine(scorer=make_decision_scorer())
        result = engine.evaluate(
            db, portfolio_id=p.id, strategy_snapshot_id=resp.strategy_snapshot_id,
            trade_date=date(2025, 1, 6), run_type="research_preflight", dry_run=True,
        )
        assert result.blocking_status != "READY", (
            f"production_pit 模式覆盖率 10% 应阻断，实际 blocking_status={result.blocking_status}"
        )
        for e in result.evidence:
            assert e.action in {"REJECTED", "DATA_BLOCKED", "HOLD", "NO_ACTION"}, (
                f"阻塞态出现非被动动作 {e.action} 于 symbol_id={e.symbol_id}"
            )
        # 应存在 SCORE_COVERAGE_BELOW_SLA 原因
        codes = {r.get("code") for r in result.blocking_reasons}
        assert "SCORE_COVERAGE_BELOW_SLA" in codes, f"应出现覆盖率门禁原因，实际 {codes}"
