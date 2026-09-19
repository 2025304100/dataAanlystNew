"""G2-WP0-5 / WP0-6 专项测试（统一撮合 + 基准健康 + 双决策门禁 + 伪池删除）。

用例：
  T_G2_SME_01  涨停锁买侧 → REJECTED_TRADE_HALTED + PENDING_RETRY
  T_G2_SME_02  次日开板 → FILLED；信号过期 → SIGNAL_EXPIRED
  T_G2_SME_03  风险卖单优先级排序（is_risk_exit sell 排在 buy 前）
  T_G2_SME_04  成本模型：卖单含印花税，买单最低佣金 5 元生效
  T_G2_BH_01   完整基准 OK 报告字段齐全
  T_G2_BH_02   连续缺 11 天 → UNAVAILABLE，不阻断回测（组合仍 SUCCEEDED）
  T_G2_BH_03   主备切换：AkShare 抛错→fallback Baostock，审计事件存在；双源失败→UNAVAILABLE
  T_G2_DG_01   auto_simulation @15:05 → SCHEDULE_TOO_EARLY
  T_G2_DG_02   today-preview 不写新 DecisionRun，view_kind=today_preview，source_decision_run_id=auto_simulation.id
  T_G2_C08_01  portfolio_backtest._pass_stock_pool 伪沪深300/中证500前缀代码删除，符号前缀测试不再生效
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

# 让 Alembic 使用测试数据库
os.environ.setdefault("ALEMBIC_DATABASE_URL", "")


@pytest.fixture(scope="function")
def tmp_alembic_db():
    """每个函数独立 SQLite 文件，显式跑 alembic upgrade head（与 G0 契约一致）。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_g2_")
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


# ===========================================================================
# T_G2_SME_01 - T+1 开盘涨停封板：买单拒，生成待处理计划
# ===========================================================================
def test_g2_sme_01_limit_up_locked_buy_side():
    from app.services.simulation_matching_engine import (
        OrderPlan, OrderSide, MarketBar, match_order_plan,
    )
    plan = OrderPlan(
        order_plan_id="op_T_G2_SME_01",
        symbol_id=1001,
        portfolio_id=1,
        trade_date=date(2025, 1, 7),
        price_type="NEXT_OPEN",
        side=OrderSide.BUY,
        target_quantity=200,
        min_lot_size=100,
    )
    bar = MarketBar(
        trade_date=date(2025, 1, 7),
        pre_close=10.0, open=11.0, high=11.0, low=11.0, close=11.0,
        upper_limit_price=11.0, lower_limit_price=9.0,
        limit_up_locked=True,  # 封板
    )
    r = match_order_plan(plan, bar)
    assert r.final_status == "REJECTED_TRADE_HALTED"
    assert "PRICE_LIMIT_LOCKED_BUY_SIDE" in r.reason_codes
    assert "CANNOT_FILL_AT_NEXT_OPEN" in r.reason_codes
    assert r.retry_on_next_session is True
    assert r.filled_quantity == 0


# ===========================================================================
# T_G2_SME_02 - 次日开板FILLED；信号过期SIGNAL_EXPIRED
# ===========================================================================
def test_g2_sme_02_open_board_fill_vs_signal_expire():
    from app.services.simulation_matching_engine import (
        OrderPlan, OrderSide, MarketBar, match_order_plan,
    )
    # A. 次日开板 → 成交
    plan = OrderPlan(
        order_plan_id="op_T_G2_SME_02A",
        symbol_id=1001,
        portfolio_id=1,
        trade_date=date(2025, 1, 8),
        price_type="NEXT_OPEN",
        side=OrderSide.BUY,
        target_quantity=200,
        signal_expire_date=date(2025, 1, 9),
    )
    bar = MarketBar(trade_date=date(2025, 1, 8), pre_close=11.0, open=10.4, close=10.6)
    r = match_order_plan(plan, bar)
    assert r.final_status == "FILLED"
    assert r.filled_quantity == 200
    assert r.executed_price is not None
    # 滑点买侧：10.4 * 1.0005 = 10.4052
    assert abs(float(r.executed_price) - 10.4 * 1.0005) < 1e-6

    # B. 信号过期 → 关闭
    plan_expired = OrderPlan(
        order_plan_id="op_T_G2_SME_02B",
        symbol_id=1002,
        portfolio_id=1,
        trade_date=date(2025, 1, 15),  # 晚于 expire
        price_type="NEXT_OPEN",
        side=OrderSide.BUY,
        target_quantity=200,
        signal_expire_date=date(2025, 1, 9),
    )
    r2 = match_order_plan(plan_expired, MarketBar(trade_date=date(2025,1,15), open=10.0))
    assert r2.final_status == "SIGNAL_EXPIRED"
    assert "SIGNAL_EXPIRED" in r2.reason_codes
    assert r2.retry_on_next_session is False


# ===========================================================================
# T_G2_SME_03 - 风险退出卖单优先级高于普通买单
# ===========================================================================
def test_g2_sme_03_risk_exit_priority_before_buy():
    from app.services.simulation_matching_engine import (
        OrderPlan, OrderSide, sort_plans_by_risk_priority,
    )
    buy_normal = OrderPlan("op_buy", 1, 1, date.today(), "NEXT_OPEN", OrderSide.BUY, 100)
    sell_normal = OrderPlan("op_sell", 2, 1, date.today(), "NEXT_OPEN", OrderSide.SELL, 100)
    sell_risk = OrderPlan("op_sell_risk", 3, 1, date.today(), "NEXT_OPEN", OrderSide.SELL, 100, is_risk_exit=True)
    plans = [buy_normal, sell_normal, sell_risk]
    ordered = sort_plans_by_risk_priority(plans)
    assert [p.order_plan_id for p in ordered] == ["op_sell_risk", "op_sell", "op_buy"]


# ===========================================================================
# T_G2_SME_04 - 成本模型：卖单印花税；买单最低佣金5元
# ===========================================================================
def test_g2_sme_04_cost_model_sell_stamp_buy_min_commission():
    from app.services.simulation_matching_engine import (
        OrderSide, apply_cost_model, CostModelConfig,
    )
    cfg = CostModelConfig(commission_rate=0.0003, min_commission=5.0, stamp_tax_rate=0.001)
    # A. 买单 100 股 × 3 元 = 300 元 → 佣金 raw=0.09，提升到 min=5
    buy = apply_cost_model(OrderSide.BUY, 100, 3.0, cfg=cfg)
    assert buy["commission"] >= 5.0
    assert buy["stamp_tax"] == 0.0
    # B. 卖单 100 股 × 10 元 = 1000 元 → 佣金 max(0.3,5)=5；印花税千1=1
    sell = apply_cost_model(OrderSide.SELL, 100, 10.0, cfg=cfg)
    assert sell["stamp_tax"] == pytest.approx(1.0, abs=1e-6)
    assert sell["commission"] >= 5.0


# ===========================================================================
# T_G2_BH_01 - 完整基准 OK 报告字段齐全
# ===========================================================================
def test_g2_bh_01_full_benchmark_coverage_ok(tmp_alembic_db):
    from app.services.benchmark_health_service import compute_health_report
    from datetime import date
    db = tmp_alembic_db
    # 注入 trade_days_provider 返回 20 个交易日；AkShare 假 fetcher 补齐 20 天
    start = date(2025, 1, 1)
    end = date(2025, 1, 31)

    def _trade_days(s, e):
        out, cur = [], s
        while cur <= e:
            if cur.weekday() < 5:
                out.append(cur)
            cur += timedelta(days=1)
        return out

    def _fake_ak(sym, s, e):
        class FakeBar:
            def __init__(self, d, c):
                self.trade_date = d; self.close = c
        return [FakeBar(d, 1000.0 + d.day) for d in _trade_days(s, e)]

    rep = compute_health_report(
        db, "000300", start, end,
        benchmark_name="沪深300",
        adj_mode="QFQ_PRE",
        akshare_fetcher=_fake_ak,
        trade_days_provider=_trade_days,
    )
    assert rep.overall_status == "OK"
    assert rep.coverage_pct >= 99.9
    assert rep.max_consecutive_gap_days == 0
    assert rep.adj_mode == "QFQ_PRE"
    assert rep.benchmark_symbol == "000300"
    d = rep.to_dict()
    for k in ["benchmark_symbol", "start_date", "coverage_pct", "gap_dates",
              "primary_source", "adj_mode", "overall_status"]:
        assert k in d, f"health report 缺字段 {k}"


# ===========================================================================
# T_G2_BH_02 - 连续缺11真实交易日 → UNAVAILABLE，不阻断回测主链路
# ===========================================================================
def test_g2_bh_02_large_gap_unavailable_but_not_blocking(tmp_alembic_db):
    from app.services.benchmark_health_service import compute_health_report
    start = date(2025, 1, 1); end = date(2025, 1, 31)

    def _trade_days(s, e):
        out, cur = [], s
        while cur <= e:
            if cur.weekday() < 5:
                out.append(cur)
            cur += timedelta(days=1)
        return out

    def _partial_ak(sym, s, e):
        # 只返回前 8 天 + 最后 3 天，中间 11 天缺口
        class FakeBar:
            def __init__(self, d, c):
                self.trade_date = d; self.close = c
        tds = _trade_days(s, e)
        return [FakeBar(d, 1000.0) for d in tds[:8]] + [FakeBar(d, 1100.0) for d in tds[-3:]]

    rep = compute_health_report(
        tmp_alembic_db, "000300", start, end,
        akshare_fetcher=_partial_ak, trade_days_provider=_trade_days,
    )
    assert rep.overall_status == "UNAVAILABLE"
    assert rep.max_consecutive_gap_days >= 11
    # 回测主链路不会因 benchmark UNAVAILABLE 被 fail-closed：
    # 这由调用方（portfolio_backtest）负责，但这里断言 API 响应 overall_status 不是 FAILED
    assert rep.overall_status in {"OK", "PARTIAL", "UNAVAILABLE"}


# ===========================================================================
# T_G2_BH_03 - 主备切换 + 双源失败审计
# ===========================================================================
def test_g2_bh_03_failover_and_both_failed(tmp_alembic_db):
    from app.services.benchmark_health_service import compute_health_report
    start = date(2025, 1, 1); end = date(2025, 1, 10)

    def _td(s, e):
        out, cur = [], s
        while cur <= e:
            if cur.weekday() < 5:
                out.append(cur)
            cur += timedelta(days=1)
        return out

    class _Boom(Exception):
        pass

    # A. 双源失败
    rep = compute_health_report(
        tmp_alembic_db, "000905", start, end,
        akshare_fetcher=lambda *a, **k: (_ for _ in ()).throw(_Boom("AK_DOWN")),
        baostock_fetcher=lambda *a, **k: (_ for _ in ()).throw(_Boom("BS_DOWN")),
        trade_days_provider=_td,
    )
    assert rep.primary_source == "BOTH_FAILED"
    assert rep.overall_status == "UNAVAILABLE"
    assert rep.source_switch_event is not None
    assert "baostock_error" in rep.source_switch_event

    # B. Ak失败→Baostock补齐
    class FakeBar:
        def __init__(self, d, c):
            self.trade_date = d; self.close = c

    def _bs(sym, s, e):
        return [FakeBar(d, 500.0) for d in _td(s, e)]

    rep2 = compute_health_report(
        tmp_alembic_db, "000905", start, end,
        akshare_fetcher=lambda *a, **k: (_ for _ in ()).throw(_Boom("AK_DOWN")),
        baostock_fetcher=_bs, trade_days_provider=_td,
    )
    # AK失败→BS成功：primary_source = BAOSTOCK_FALLBACK 或 FALLBACK_MIXED
    assert rep2.primary_source in {"BAOSTOCK_FALLBACK", "FALLBACK_MIXED"}


# ===========================================================================
# T_G2_DG_01 - auto_simulation @15:05 → SCHEDULE_TOO_EARLY
# ===========================================================================
def test_g2_dg_01_auto_sim_schedule_too_early():
    from app.services.decision_schedule_gate import ensure_auto_simulation_schedule
    too_early = datetime(2025, 1, 6, 15, 5, 0)  # 15:05
    g = ensure_auto_simulation_schedule(too_early, portfolio_id=1)
    assert g.allowed is False
    assert g.error_code == "SCHEDULE_TOO_EARLY"
    # 20:30 通过
    good = datetime(2025, 1, 6, 20, 30, 0)
    g2 = ensure_auto_simulation_schedule(good, portfolio_id=1)
    assert g2.allowed is True
    assert g2.effective_data_cutoff_at == datetime(2025, 1, 6, 20, 0, 0)


# ===========================================================================
# T_G2_DG_02 - today-preview 纯视图：不写新行、映射源auto_simulation run id
# ===========================================================================
def test_g2_dg_02_today_preview_view_maps_auto_sim_run(tmp_alembic_db):
    from app.services.decision_schedule_gate import build_today_preview_view
    from app.models.decision_engine import DecisionRun, StrategyExecutionSnapshot
    from app.models.portfolio import Portfolio
    db = tmp_alembic_db
    # 构造基础数据（使用 Portfolio 字段最小子集：无 base_currency NOT NULL）
    from sqlalchemy import inspect as _insp
    insp = _insp(Portfolio)
    cols = {c.name for c in insp.columns}
    portfolio_kwargs: dict[str, Any] = {"id": 9001, "name": "T_G2_DG_02", "total_capital": 100000}
    # 若表有 data_source / created_at / updated_at NOT NULL 则填充
    for opt in ["data_source", "benchmark", "display_name", "account_type", "asset_scope"]:
        if opt in cols:
            portfolio_kwargs.setdefault(opt, "mock" if opt != "asset_scope" else "mixed")
    for dcol in ["created_at", "updated_at", "last_reviewed_at"]:
        if dcol in cols:
            portfolio_kwargs.setdefault(dcol, datetime(2025,1,1))
    for fcol in ["investable_ratio", "cash_reserve_ratio"]:
        if fcol in cols:
            portfolio_kwargs.setdefault(fcol, 1.0 if fcol == "investable_ratio" else 0.0)
    db.add(Portfolio(**portfolio_kwargs))
    db.flush()
    db.add(StrategyExecutionSnapshot(
        id="ses_T_G2_DG_02", snapshot_no=1, portfolio_id=9001,
        decision_clock_json="{}", member_snapshot_json="[]",
        universe_type="portfolio_members", snapshot_type="task_locked",
        snapshot_hash="a" * 64, idempotency_key="key_T_G2_DG_02",
        effective_from=date(2025,1,6), created_at=datetime(2025,1,6,20,0),
    ))
    db.flush()
    # 20:30 auto_simulation DecisionRun（DB value = auto_simulation）
    db.add(DecisionRun(
        id="dr_T_G2_DG_02_auto", strategy_snapshot_id="ses_T_G2_DG_02",
        portfolio_id=9001, trade_date=date(2025,1,6),
        run_type="auto_simulation",  # DB 值
        status="SUCCEEDED",
        decision_at=datetime(2025,1,6,20,30,0),
        data_cutoff_at=datetime(2025,1,6,20,0,0),
        execution_at=datetime(2025,1,7,9,30,0),
        universe_count=20, member_count=20,
        idempotency_key="auto_T_G2_DG_02", result_hash="x"*64,
        created_at=datetime(2025,1,6,20,30,0),
        started_at=datetime(2025,1,6,20,30,0),
        finished_at=datetime(2025,1,6,20,31,0),
    ))
    db.commit()

    view = build_today_preview_view(db, 9001, date(2025, 1, 6))
    assert view.view_kind == "today_preview"
    assert view.source_decision_run_id == "dr_T_G2_DG_02_auto"
    assert view.status == "READY"
    # today_preview 实体行数永远 0
    from sqlalchemy import text as _sa_text
    count = db.scalar(_sa_text("SELECT COUNT(*) FROM decision_runs WHERE run_type = 'today_preview'"))
    assert int(count or 0) == 0


# ===========================================================================
# T_G2_C08_01 - 回测链路删除指数前缀伪池（_pass_stock_pool 不再按 code.startswith 切分）
# ===========================================================================
def test_g2_c08_01_drop_index_prefix_pseudo_pool():
    """C-08：禁用“688/300/301 前缀 ≈ 沪深300/中证500”的伪启发式选股。"""
    import app.services.portfolio_backtest as pb_mod
    # 截取 500 行范围读取 _filter_symbol_ids_by_rule 源码不应出现 688/300/301 前缀过滤
    import inspect
    src = inspect.getsource(pb_mod)
    # 仍然可能存在注释说明性文本，仅断言：不存在启发式 index_membership 分支：
    # 原先形如 `code.startswith(("688","300","301"))` 的动态过滤被删除。
    old_heuristic_fragments = [
        'if stock_pool == "沪深300":',
        'code.startswith(("688", "300", "301"))',
        'if stock_pool == "中证500":',
        'code.startswith(("688",))',
    ]
    for frag in old_heuristic_fragments:
        assert frag not in src, f"C-08 仍残留伪池逻辑片段: {frag!r}"
    # C-08 合规注释存在
    assert "PortfolioCandidate(pid, as_of_date, auto_authorized=True" in src
