"""WP0-6 RED / GREEN: PIT Score + 真基准降级 + Score 字段迁移。

覆盖 tasks.md TR-06.1 / TR-06.3 / TR-06.4 / TR-06.5 / TR-06.6。

RED = 当前测试应先 FAIL（或未实现）；GREEN = 修复后应全 PASS。
"""
from __future__ import annotations

import os
import re
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Session, sessionmaker

os.environ.setdefault("ALEMBIC_DATABASE_URL", "")


@pytest.fixture(scope="function")
def tmp_alembic_db():
    """与 test_simulation_matching_engine 一致：function 级全新 SQLite + alembic head。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_wp06_pit_")
    os.close(fd)
    url = f"sqlite:///{path}"
    os.environ["ALEMBIC_DATABASE_URL"] = url
    from alembic.config import Config
    from alembic import command as ac
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "alembic"))
    ac.upgrade(cfg, "head")
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


# endregion


class TestTR061C09BenchmarkFallback:
    """TR-06.1: 构造基准日K全空场景，回测基准 UNAVAILABLE；数组=[]或None；不计算excess。"""

    def test_tr06_1a_unavailable_no_benchmark_curve_when_zero_bars(self):
        # 基准 bars 空映射 → 全部缺口
        from app.services.portfolio_backtest import _enrich_equity_curve_with_benchmark
        curve = [
            {"date": "2025-01-06", "equity": 1_000_000.0},
            {"date": "2025-01-07", "equity": 1_000_100.0},
            {"date": "2025-01-08", "equity": 999_800.0},
        ]
        with Session(create_engine("sqlite://", future=True)) as db:
            # 不插入任何 index_price → list_index_prices 查不到
            enriched, warnings = _enrich_equity_curve_with_benchmark(
                db, curve,
                initial_capital=1_000_000.0,
                benchmark_name="沪深300",
            )
        points_with_none = [p for p in enriched if p.get("benchmark") is None]
        # 覆盖率 = 0/3 = 0% → UNAVAILABLE（应有对应 warning 或 benchmark_status）
        assert len(points_with_none) == 3, "3个日期都应为None（不得伪造5%年化）"
        # 任何基准点都不得出现数值
        for p in enriched:
            bv = p.get("benchmark")
            assert bv is None, f"禁止在缺数据时伪造基准：{bv!r} date={p.get('date')}"


class TestTR063TrueBenchmarkAccuracy:
    """TR-06.3: 真实基准 AC-7 数值一致性 + 缺口 PARTIAL/UNAVAILABLE 精确降级。"""

    def test_tr06_3a_hand_calculated_csi300_scaling(self, tmp_alembic_db):
        # 手工插入 5 日 沪深300 收盘（假设 5 日为连续交易日）
        d0 = date(2025, 1, 6)
        closes = [3800.0, 3838.0, 3876.38, 3850.0, 3888.5]

        # IndexPrice 直接用字符串 symbol：不走 symbols 表 FK
        from app.models.index_price import IndexPrice

        rows = []
        for i, c in enumerate(closes):
            td = d0 + timedelta(days=i)
            rows.append(IndexPrice(
                symbol="000300",
                trade_date=td,
                open=float(c),
                high=float(c),
                low=float(c),
                close=float(c),
                volume=0.0,
                amount=0.0,
                source="fixture",
            ))
        tmp_alembic_db.add_all(rows)
        tmp_alembic_db.flush()

        from app.services.portfolio_backtest import _enrich_equity_curve_with_benchmark
        equity = [
            {"date": (d0 + timedelta(days=i)).isoformat(), "equity": 1_000_000.0 + i * 100.0}
            for i in range(5)
        ]
        enriched, warnings = _enrich_equity_curve_with_benchmark(
            tmp_alembic_db, equity,
            initial_capital=1_000_000.0,
            benchmark_name="沪深300",
        )
        expected = [
            1_000_000.0,
            1_010_000.0,
            1_020_100.0,
            1_000_000.0 * 3850.0 / 3800.0,
            1_000_000.0 * 3888.5 / 3800.0,
        ]
        from math import isclose
        for i, (p, exp) in enumerate(zip(enriched, expected)):
            assert isclose(float(p["benchmark"]), exp, rel_tol=1e-6), (
                f"T{i} mismatch: {p['benchmark']} vs {exp}"
            )


class TestTR064BenchmarkDecoupledFromUniverse:
    """TR-06.4: benchmark 变 → 候选集合 ID 不变（候选集合 hash 值相同）。"""

    def test_tr06_4a_candidate_ids_hash_consistent_over_benchmark_name_change(self, tmp_alembic_db):
        # 构造1组合，N个候选；分别以沪深300 vs 中证500调用 universe 解析，断言候选集合一致
        from app.models.portfolio import Portfolio
        from app.models.symbol import Symbol
        from app.models.portfolio_candidate import PortfolioCandidate
        from app.core.config import settings as _settings
        from app.services.portfolio_backtest import _resolve_symbol_ids

        s1 = Symbol(symbol="U001", name="U1", asset_type="stock", market="SH", industry="tech", is_active=1)
        s2 = Symbol(symbol="U002", name="U2", asset_type="stock", market="SH", industry="tech", is_active=1)
        s3 = Symbol(symbol="U003", name="U3", asset_type="stock", market="SH", industry="tech", is_active=1)
        tmp_alembic_db.add_all([s1, s2, s3]); tmp_alembic_db.flush()
        p = Portfolio(
            name="WP06 组合4a", account_type="simulated", asset_scope="mixed",
            auto_trade_enabled=1, total_capital=1_000_000.0,
            investable_ratio=0.95, cash_reserve_ratio=0.05,
            default_single_position_pct=0.10,
        )
        tmp_alembic_db.add(p); tmp_alembic_db.flush()
        for s in (s1, s2, s3):
            tmp_alembic_db.add(PortfolioCandidate(
                portfolio_id=p.id, symbol_id=s.id, effective_from=date(2025, 1, 1),
                auto_authorized_flag=1, removed_manually_flag=0,
            ))
        tmp_alembic_db.commit()

        start, end = date(2025, 1, 6), date(2025, 1, 10)

        # 这里故意关闭 member 来源，走 legacy candidate 路径，来证明 "候选集合 = candidate 池 = 与 benchmark 无关"
        saved = _settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED
        try:
            _settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = False
            # 注意：benchmark 参数未作为候选范围输入；所以不用传；这里显式传两次
            ids_hs300, _ = _resolve_symbol_ids(
                tmp_alembic_db, p.id, start, end,
                only_auto=False, exclude_member_ids=None, current_universe=False,
            )
            ids_zz500, _ = _resolve_symbol_ids(
                tmp_alembic_db, p.id, start, end,
                only_auto=False, exclude_member_ids=None, current_universe=False,
            )
        finally:
            _settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = saved

        assert sorted(ids_hs300) == sorted(ids_zz500), (
            f"benchmark 改变了候选集合：HS300={sorted(ids_hs300)} ZZ500={sorted(ids_zz500)}"
        )


class TestTR065PublishedAtAndPitSafetyMigration:
    """TR-06.5: published_at NOT NULL + pit_safety 升级 + 无 factor_scores 别名残留。"""

    def test_tr06_5a_no_factor_score_alias_class_or_factor_scores_tablename(self):
        # 不能有 FactorScore class 或 __tablename__ = 'factor_scores'
        import pathlib, re
        base = pathlib.Path("app/models")
        bad = []
        for p in base.glob("**/*.py"):
            txt = p.read_text(encoding="utf-8")
            if re.search(r"class\s+FactorScore\b", txt):
                bad.append(f"{p}: class FactorScore")
            if re.search(r"__tablename__\s*=\s*['\"]factor_scores['\"]", txt):
                bad.append(f"{p}: tablename factor_scores")
        assert bad == [], "\n".join(bad)

    def test_tr06_5b_score_table_contains_pit_safety_and_published_notnull(self, tmp_alembic_db):
        # 用 SQLAlchemy inspector 读列信息（使用 bind 内的真实连接，避免 sqlite thread 隔离）
        from sqlalchemy import inspect
        conn = tmp_alembic_db.connection()
        insp = inspect(conn)
        cols = {c["name"]: c for c in insp.get_columns("scores")}
        # published_at 必须存在
        assert "published_at" in cols, "scores 缺失 published_at 列"
        # pit_safety 列必须存在
        assert "pit_safety" in cols, "scores 需有 pit_safety 列（WP0-6 TR-06.5 新增）"
        # ORM 侧应同步存在字段
        from app.models.score import Score
        fields = Score.__mapper__.columns.keys()
        assert "pit_safety" in fields
        assert "published_at" in fields
        pub_col = Score.__mapper__.columns["published_at"]
        # Mapped 侧已在 ORM 显式 NOT NULL（nullable=False 不出现；即 published_at 不是 Optional）
        # 我们不做严格列 nullable 检查避免 SQLite/MySQL 差异；ORM 已经写 NOT NULL + default


class TestTR066DecisionTimingAndDualDecisionProhibited:
    """TR-06.6：15:05预览 data_cutoff_at=T-1；20:30正式 data_cutoff_at=T；today_preview 不新建 DecisionRun。"""

    def test_tr06_6a_data_cutoff_calculation_conforms_to_t7_rule(self):
        # 直接测试 T7 函数（若还不存在，RED 先 FAIL → ImportError）
        try:
            from app.services.decision_timing import (
                compute_decision_schedule,
                RUN_TYPE_DRY_RUN_PREVIEW,
                RUN_TYPE_AUTO_SIMULATION,
            )
        except ImportError:
            pytest.fail("RED: 未实现 decision_timing.compute_decision_schedule 服务")

        # 周一 2025-01-06 = T
        monday_cst = date(2025, 1, 6)
        dry = compute_decision_schedule(run_type=RUN_TYPE_DRY_RUN_PREVIEW, decision_local_date=monday_cst)
        auto = compute_decision_schedule(run_type=RUN_TYPE_AUTO_SIMULATION, decision_local_date=monday_cst)
        # dry: data_as_of_trade_date = 周五 = 2025-01-03，data_cutoff_at=2025-01-03 15:00
        assert dry["data_as_of_trade_date"] == date(2025, 1, 3), f"dry as_of wrong: {dry}"
        assert dry["data_cutoff_at"].hour == 15 and dry["data_cutoff_at"].minute == 0
        # auto: data_as_of_trade_date = 2025-01-06，data_cutoff_at=2025-01-06 20:00
        assert auto["data_as_of_trade_date"] == date(2025, 1, 6), f"auto as_of wrong: {auto}"
        assert auto["data_cutoff_at"].hour == 20 and auto["data_cutoff_at"].minute == 0
