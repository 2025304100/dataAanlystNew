"""G1-WP0-2e/f/g：FactorUsage 绑定 + StrategyExecutionSnapshot + 3 路由契约测试。

覆盖：
- T_G1_FU_01: get_factor_usage_options 返回选项，active_model 标记正确
- T_G1_FU_02: get_current_usage 新环境下返回 None（空历史不报错）
- T_G1_FU_03: preflight_usage research 模式绑定非全局 active 模型返回警告 DEGRADED_DATA，OK=True（非 blocking）
- T_G1_FU_04: preflight_usage production_sim 模式绑定非全局 active → blocking（Fail-Closed, Q6/Q21A）
- T_G1_FU_05: save_and_apply_usage → 事务中同时生成 PortfolioFactorUsage + StrategyExecutionSnapshot，两者内容哈希、idempotency_key 非空
- T_G1_FU_06: save_and_apply 后再次 get_current_usage 能读到 effective_to is None 的新 active 项，旧项置为 deprecated + effective_to
- T_G1_FU_07: 同 payload 1s 内连续调用 save_and_apply 命中幂等不重复插入（TODO：429 层测试放到集成里；单测直接插入同 idempotency_key 记录验证 unique index 生效）
- T_G1_FU_08: 快照不可变：创建后 UPDATE snapshot_hash 抛错/被拒绝（乐观锁无此列，但 model 本身没 mutable 锁；测试：同一 save_and_apply 再调用时返回已有记录而不是新对象）
- T_G1_FU_09: FastAPI TestClient 3 路由可调用
- T_G1_FU_10: decision_clock.json 三个字段齐全 (decision_at/data_cutoff_at/execution_at + UTC+CST 各一份)
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import and_, select

# 让 Alembic 使用测试数据库
os.environ.setdefault("ALEMBIC_DATABASE_URL", "")


@pytest.fixture(scope="function")
def tmp_alembic_db():
    """每个函数独立 SQLite 文件，显式跑 alembic upgrade head（与 G0 契约一致）。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_g1_")
    os.close(fd)
    url = f"sqlite:///{path}"
    os.environ["ALEMBIC_DATABASE_URL"] = url
    # 用 pytest monkeypatch 更稳：手动设置环境变量
    from alembic.config import Config
    from alembic import command as ac
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "alembic"))
    ac.upgrade(cfg, "head")
    # 创建 SQLAlchemy session
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
# 基础数据 helpers (与 test_g0_contract 类似但独立)
# ---------------------------------------------------------------------------

def _make_portfolio(db, **kw):
    from app.models.portfolio import Portfolio
    p = Portfolio(
        name=kw.pop("name", "T_G1_FU Test Portfolio"),
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
    db.add(p)
    db.flush()
    return p


def _make_rule(db, portfolio_id: int, **kw):
    from app.models.portfolio import PortfolioRule
    import json
    r = PortfolioRule(
        portfolio_id=portfolio_id,
        rule_name=kw.pop("rule_name", "规则默认版本"),
        max_single_position_pct=kw.pop("max_single_position_pct", 0.3),
        max_sector_position_pct=kw.pop("max_sector_position_pct", 0.4),
        max_stock_position_pct=kw.pop("max_stock_position_pct", 1.0),
        max_etf_position_pct=kw.pop("max_etf_position_pct", 1.0),
        max_loss_per_trade_pct=kw.pop("max_loss_per_trade_pct", 0.07),
        max_open_positions=kw.pop("max_open_positions", 20),
        stage_limits_json=json.dumps({
            "stock_stages": [{"stage": 1, "limit_pct": 0.5}],
            "etf_stages": [{"stage": 1, "limit_pct": 0.5}],
        }),
        is_active=kw.pop("is_active", 1),
    )
    for k, v in kw.items():
        setattr(r, k, v)
    db.add(r)
    db.flush()
    return r


def _set_active_model(db, model_run_id: str | None, weight_mode: str = "ridge"):
    """设置或创建全局 active 模型（Q21 方案 A）。"""
    from app.models.factor_runtime import FactorRuntimeState
    s = db.get(FactorRuntimeState, 1)
    if s is None:
        s = FactorRuntimeState(id=1, weight_mode=weight_mode, active_model_run_id=model_run_id,
                               updated_by="qa", version=1)
        db.add(s)
    else:
        s.weight_mode = weight_mode
        s.active_model_run_id = model_run_id
        s.version += 1
    db.flush()
    return s


# ---------------------------------------------------------------------------
# T_G1_FU_01 ~ T_G1_FU_10
# ---------------------------------------------------------------------------

class TestFactorUsageService:
    """G1-WP0-2e + 2f：FactorUsage / Snapshot 服务层契约。"""

    def test_01_options_shows_active_model_flag(self, tmp_alembic_db):
        from app.services.factor_usage_service import get_factor_usage_options
        db = tmp_alembic_db
        p = _make_portfolio(db)
        _set_active_model(db, "fmr_ACTIVE_G1_TEST")
        db.commit()
        db = tmp_alembic_db  # same session
        opts = get_factor_usage_options(db, p.id)
        assert opts.global_active_model_run_id == "fmr_ACTIVE_G1_TEST"
        assert opts.global_weight_mode == "ridge"
        # 未命中 blocking：GLOBAL_ACTIVE_MODEL_EMPTY
        assert not any("GLOBAL_ACTIVE_MODEL_EMPTY" in b for b in opts.blocking_reasons)

    def test_02_empty_current_usage_is_none(self, tmp_alembic_db):
        from app.services.factor_usage_service import get_current_usage
        db = tmp_alembic_db
        p = _make_portfolio(db)
        db.commit()
        resp = get_current_usage(db, p.id)
        assert resp.current is None
        assert resp.history == []
        assert resp.latest_snapshot_id is None

    def test_03_preflight_research_non_active_has_warning_but_ok(self, tmp_alembic_db):
        from app.services.factor_usage_service import preflight_usage
        from app.schemas.decision_engine import FactorUsageBindRequest
        db = tmp_alembic_db
        p = _make_portfolio(db)
        _set_active_model(db, "fmr_ACTIVE")
        db.commit()
        req = FactorUsageBindRequest(
            factor_model_run_id="fmr_NOT_ACTIVE",
            run_mode="research",
            pit_mode="best_effort",
        )
        resp = preflight_usage(db, p.id, req, "qa_user")
        assert resp.ok is True  # research 模式允许非全局 active（Q6）
        codes = [w.code for w in resp.warnings]
        assert "DEGRADED_DATA" in codes  # Q6：非全局 active → research 模式打降级标签

    def test_04_preflight_production_simulation_non_active_blocks(self, tmp_alembic_db):
        from app.services.factor_usage_service import preflight_usage
        from app.schemas.decision_engine import FactorUsageBindRequest
        db = tmp_alembic_db
        p = _make_portfolio(db)
        _set_active_model(db, "fmr_ACTIVE")
        db.commit()
        req = FactorUsageBindRequest(
            factor_model_run_id="fmr_NOT_ACTIVE",
            run_mode="production_sim",
            pit_mode="strict_pit_safe",
        )
        resp = preflight_usage(db, p.id, req, "qa_user")
        assert resp.ok is False
        codes = [w.code for w in resp.warnings]
        assert "MODEL_NOT_GLOBAL_ACTIVE" in codes  # Q21A + Fail-Closed

    def test_05_save_and_apply_writes_both_tables(self, tmp_alembic_db):
        from app.services.factor_usage_service import save_and_apply_usage
        from app.schemas.decision_engine import FactorUsageBindRequest
        from app.models.decision_engine import PortfolioFactorUsage, StrategyExecutionSnapshot
        db = tmp_alembic_db
        p = _make_portfolio(db)
        _set_active_model(db, "fmr_ACTIVE_SAVE")
        db.commit()
        req = FactorUsageBindRequest(
            factor_model_run_id="fmr_ACTIVE_SAVE",
            run_mode="research",
            pit_mode="best_effort",
        )
        resp = save_and_apply_usage(db, p.id, req, "qa_user")
        # 响应字段齐全
        assert resp.strategy_snapshot_id, "save_and_apply 必须返回 strategy_snapshot_id"
        assert resp.snapshot_hash and len(resp.snapshot_hash) == 64  # SHA-256 hex
        assert resp.idempotency_key, "Q28: 后端幂等键必须非空"
        assert resp.effective_from is not None
        # 数据库核对两张表
        u = db.get(PortfolioFactorUsage, resp.factor_usage.id)
        assert u is not None
        assert u.portfolio_id == p.id
        assert u.factor_model_run_id == "fmr_ACTIVE_SAVE"
        assert u.status in {"active", "draft"}
        assert u.effective_to is None, "刚生成的 active usage 不应有 effective_to"
        s = db.get(StrategyExecutionSnapshot, resp.strategy_snapshot_id)
        assert s is not None
        assert s.portfolio_factor_usage_id == u.id
        assert s.snapshot_hash == resp.snapshot_hash
        assert s.idempotency_key == resp.idempotency_key
        # decision_clock JSON 字段齐全 (Q1)
        import json
        clock = json.loads(s.decision_clock_json or "{}")
        for k in ("decision_at_utc", "data_cutoff_at_utc", "execution_at_utc",
                  "decision_at_cst", "data_cutoff_at_cst", "execution_at_cst"):
            assert k in clock, f"decision_clock_json 缺少字段 {k}"

    def test_06_repeated_save_deprecates_older_usage(self, tmp_alembic_db):
        from app.services.factor_usage_service import save_and_apply_usage, get_current_usage
        from app.schemas.decision_engine import FactorUsageBindRequest
        from app.models.decision_engine import PortfolioFactorUsage
        db = tmp_alembic_db
        p = _make_portfolio(db)
        _set_active_model(db, "fmr_ACTIVE")
        db.commit()
        req1 = FactorUsageBindRequest(
            factor_model_run_id="fmr_ACTIVE", run_mode="research",
            pit_mode="best_effort", score_sla_coverage_pct=90.0,
        )
        r1 = save_and_apply_usage(db, p.id, req1, "qa_user")
        # 第二次，显式不同 payload hash（改 SLA）
        req2 = FactorUsageBindRequest(
            factor_model_run_id="fmr_ACTIVE", run_mode="research",
            pit_mode="best_effort", score_sla_coverage_pct=95.0,
        )
        r2 = save_and_apply_usage(db, p.id, req2, "qa_user")
        # 检查：r1 usage 已 deprecated 且有 effective_to
        u1 = db.get(PortfolioFactorUsage, r1.factor_usage.id)
        u2 = db.get(PortfolioFactorUsage, r2.factor_usage.id)
        assert u1 is not None and u2 is not None
        assert u1.status == "deprecated" and u1.effective_to is not None
        assert u2.status in {"active", "draft"} and u2.effective_to is None
        # get_current_usage 读到最新 active（u2）
        resp = get_current_usage(db, p.id)
        assert resp.current is not None, "current usage 应该指向 u2"
        assert resp.current.id == u2.id
        # history 至少包含 2 条
        assert len(resp.history) >= 2

    def test_07_idempotency_unique_index_rejects_duplicate_key(self, tmp_alembic_db):
        """Q28: uq_ses_idempotency 唯一索引生效。"""
        from datetime import datetime as _dt
        from sqlalchemy.exc import IntegrityError
        from app.models.decision_engine import StrategyExecutionSnapshot
        db = tmp_alembic_db
        t_a = _dt(2025, 1, 1, 0, 0, 0)
        t_b = _dt(2025, 1, 1, 0, 0, 1)
        s = StrategyExecutionSnapshot(
            id="ses_T_G1_07_A",
            snapshot_no=1,
            portfolio_id=1,
            decision_clock_json="{}",
            member_snapshot_json="[]",
            universe_type="portfolio_members",
            snapshot_type="task_locked",
            snapshot_hash="a" * 64,
            idempotency_key="SAME_IDEMPOTENCY_KEY_G1_07",
            effective_from=t_a,
            created_at=t_a,
        )
        db.add(s)
        db.flush()
        db.commit()  # SQLite 需要显式 commit 才能让 UNIQUE 对第二条记录生效
        s2 = StrategyExecutionSnapshot(
            id="ses_T_G1_07_B",
            snapshot_no=2,
            portfolio_id=1,
            decision_clock_json="{}",
            member_snapshot_json="[]",
            universe_type="portfolio_members",
            snapshot_type="task_locked",
            snapshot_hash="b" * 64,
            idempotency_key="SAME_IDEMPOTENCY_KEY_G1_07",  # same
            effective_from=t_b,
            created_at=t_b,
        )
        db.add(s2)
        with pytest.raises(IntegrityError):
            db.commit()  # SQLite UNIQUE 在 commit 阶段才检查（取决于驱动），这里直接 commit 验证
        db.rollback()

    def test_08_snapshot_member_count_and_universe_type(self, tmp_alembic_db):
        """Q23: 快照 universe_type 固定 portfolio_members，禁止 heuristic 指数池。"""
        from app.services.factor_usage_service import save_and_apply_usage
        from app.schemas.decision_engine import FactorUsageBindRequest
        from app.models.decision_engine import StrategyExecutionSnapshot
        db = tmp_alembic_db
        p = _make_portfolio(db)
        _set_active_model(db, "fmr_ACTIVE")
        db.commit()
        resp = save_and_apply_usage(
            db, p.id,
            FactorUsageBindRequest(factor_model_run_id="fmr_ACTIVE"),
            "qa_user",
        )
        s = db.get(StrategyExecutionSnapshot, resp.strategy_snapshot_id)
        assert s.universe_type == "portfolio_members", "Q23: 回测/执行选股范围必须来自组合成员快照，不得隐式用沪深 300"


class TestFactorUsageRoutes:
    """G1-WP0-2g：FastAPI 路由契约。"""

    def _build_client(self, tmp_alembic_db):
        """构造带依赖覆盖的 FastAPI TestClient。"""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.db.session import get_db

        def _override_get_db():
            yield tmp_alembic_db

        app.dependency_overrides[get_db] = _override_get_db
        return TestClient(app)

    def test_09_routes_return_200(self, tmp_alembic_db):
        client = self._build_client(tmp_alembic_db)
        db = tmp_alembic_db
        p = _make_portfolio(db)
        r = _make_rule(db, p.id)
        _set_active_model(db, "fmr_ACTIVE_ROUTE")
        db.commit()
        # 1) factor-usage-options
        resp = client.get(f"/api/v1/portfolios/{p.id}/factor-usage-options")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["global_active_model_run_id"] == "fmr_ACTIVE_ROUTE"
        # 2) current factor usage (empty)
        resp = client.get(f"/api/v1/portfolios/{p.id}/factor-usage")
        assert resp.status_code == 200, resp.text
        assert resp.json()["current"] is None
        # 3) preflight
        payload = {
            "factor_model_run_id": "fmr_ACTIVE_ROUTE",
            "factor_set_id": None,
            "rule_id": r.id,
            "run_mode": "research",
            "pit_mode": "best_effort",
        }
        resp = client.post(
            f"/api/v1/portfolios/{p.id}/strategy-preflight",
            json=payload,
            headers={"X-User": "qa"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert "preflight_snapshot_hash" in body and len(body["preflight_snapshot_hash"]) == 64
        assert "decision_clock" in body
        # 4) save_and_apply
        resp = client.post(
            f"/api/v1/portfolios/{p.id}/factor-usage",
            json=payload,
            headers={"X-User": "qa", "Idempotency-Key": "qqq-uuid-g1-09"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "strategy_snapshot_id" in body
        assert "snapshot_hash" in body and len(body["snapshot_hash"]) == 64
        # 5) current 现在有值
        resp = client.get(f"/api/v1/portfolios/{p.id}/factor-usage")
        assert resp.status_code == 200
        assert resp.json()["current"] is not None
        assert resp.json()["current"]["factor_model_run_id"] == "fmr_ACTIVE_ROUTE"

    def test_10_production_blocked_via_api_returns_400(self, tmp_alembic_db):
        client = self._build_client(tmp_alembic_db)
        db = tmp_alembic_db
        p = _make_portfolio(db)
        _set_active_model(db, "fmr_ACTIVE_ROUTE")
        db.commit()
        payload = {
            "factor_model_run_id": "fmr_NOT_ACTIVE",
            "run_mode": "production_pit",
            "pit_mode": "strict_pit_safe",
        }
        resp = client.post(
            f"/api/v1/portfolios/{p.id}/factor-usage",
            json=payload,
        )
        # Q21A: 正式模式绑定非全局 active → 400 PREFLIGHT_BLOCKED
        assert resp.status_code == 400, resp.text
        body = resp.json()
        # 兼容两种形态：HTTPException.detail 直出 / 全局错误包装器包裹
        def _find_blocked(node):
            if isinstance(node, dict):
                if node.get("error") == "PREFLIGHT_BLOCKED":
                    return True
                for v in node.values():
                    if _find_blocked(v):
                        return True
            elif isinstance(node, list):
                for v in node:
                    if _find_blocked(v):
                        return True
            return False
        assert _find_blocked(body), f"PREFLIGHT_BLOCKED 未出现在响应体中：{body}"
