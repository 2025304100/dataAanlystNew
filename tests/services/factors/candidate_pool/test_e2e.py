"""T11 · 看板分析 + 冻结快照 + 锁定/重置 —— 端到端契约测试（DoD）。

测试策略
========
- **纯引擎**（`build_analysis`）用内存面板，毫秒级。
- **端到端**用 `db_session`（SQLite）—— 绝不碰真实 MySQL 主数据。
  🚨 教训：T11 开发中我一度用真实 MySQL 做冒烟，往 `symbols` 写了 61 行测试数据，
  事后花了专门脚本清理（见 `evidence/T11_master_data_cleanup.json`）。
  候选池域的隔离模型是「不回写主数据」—— 测试同样不该写真实主数据。

`analysis_json` 的顶层 key 是 T22（AI 生成）的 Prompt 变量契约，
这里有专门的哨兵测试防漂移。
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.models.symbol import Symbol
from app.schemas.errors import FactorSevenError
from app.services.factors.candidate_pool import analysis as A
from app.services.factors.candidate_pool import service as S

N_MARKET = 120
N_MEMBERS = 60


# ══════════════════════════════════════════════════════════
# 夹具：内存面板
# ══════════════════════════════════════════════════════════


def _panel(seed: int = 7) -> A.AnalysisPanel:
    rng = np.random.default_rng(seed)
    symbols = [f"M{i:04d}" for i in range(N_MARKET)]
    members = symbols[:N_MEMBERS]
    dates = pd.date_range("2026-07-01", periods=26, freq="B")
    base = np.where(np.arange(N_MARKET) < N_MEMBERS, 100.0, 50.0)
    drift = np.where(np.arange(N_MARKET) < N_MEMBERS, 1.004, 1.000)
    rows = []
    for i, sym in enumerate(symbols):
        prices = base[i] * np.cumprod(
            np.full(len(dates), drift[i]) * rng.normal(1.0, 0.01, len(dates)))
        for d, p in zip(dates, prices):
            rows.append((sym, d, float(p), float(p) * 10, float(p) * 100,
                         round(float(rng.uniform(0.5, 3.0)), 4)))
    daily = pd.DataFrame(rows, columns=["symbol", "trade_date", "close", "volume",
                                        "amount", "turnover_rate"])
    caps = np.concatenate([
        rng.uniform(2e10, 6e10, N_MEMBERS),
        rng.uniform(1e9, 8e10, N_MARKET - N_MEMBERS),
    ])
    valuation = pd.DataFrame({
        "symbol": symbols,
        "pe_ttm": rng.uniform(5, 60, N_MARKET).round(2),
        "pb": rng.uniform(0.5, 5, N_MARKET).round(2),
        "total_market_cap": caps,
        "circulating_market_cap": 1e10,
        "dividend_yield": [None] * N_MARKET,
    })
    financials = pd.DataFrame({
        "symbol": symbols[:N_MEMBERS],
        "roe_ttm": rng.uniform(2, 25, N_MEMBERS).round(2),
        "annual_periods": [None] * N_MEMBERS,
        "annual_net_profits": [None] * N_MEMBERS,
    })
    return A.AnalysisPanel(
        daily=daily, valuation=valuation, financials=financials,
        members=members, as_of_date=date(2026, 8, 21),
        lookback_days=20, trade_days_actual=len(dates), avg_daily_symbols=N_MARKET,
    )


@pytest.fixture
def panel() -> A.AnalysisPanel:
    return _panel()


@pytest.fixture
def analysis(panel) -> dict:
    return A.build_analysis(panel)


def _seed_symbols(db_session, codes) -> dict[str, int]:
    for code in codes:
        db_session.add(Symbol(symbol=code, name=f"名{code}", asset_type="stock",
                              market="sh", board="main", is_st=0, is_active=1))
    db_session.flush()
    rows = db_session.query(Symbol).filter(Symbol.symbol.in_(codes)).all()
    return {s.symbol: s.id for s in rows}


def _make_pool(db_session, member_codes) -> tuple[Any, dict[str, int]]:
    code_to_id = _seed_symbols(db_session, member_codes)
    pool = S.create_pool(db_session, name="T11 测试池",
                         source_type=S.SOURCE_TYPE_FILTER,
                         filter_config={"markets": ["sh"]}, created_by="test")
    S.add_members(db_session, pool.id,
                  symbol_ids=[code_to_id[c] for c in member_codes],
                  operator_id="test")
    return pool, code_to_id


MEMBER_CODES = [f"T{i:04d}" for i in range(N_MEMBERS)]


# ══════════════════════════════════════════════════════════
# 1. 纯引擎
# ══════════════════════════════════════════════════════════


class TestBuildAnalysis:
    def test_top_level_keys_are_the_t22_contract(self, analysis):
        """`analysis_json` 的顶层 key 是 T22 AI 生成（Step4）的 Prompt 变量契约。
        改名 = 破坏下游 Prompt 组装。"""
        for key in ("overview", "market_cap_distribution", "industry_distribution",
                    "style_exposure", "market_environment", "data_quality",
                    "warnings"):
            assert key in analysis, f"analysis_json 缺契约 key {key}"

    def test_overview_cards(self, analysis):
        ov = analysis["overview"]
        assert ov["stock_count"] == N_MEMBERS
        assert ov["below_min_pool_size"] is False
        assert ov["avg_market_cap"] is not None
        assert ov["time_range"] == "待配置"       # Step2 配置后回填
        assert ov["data_completeness"] is not None

    def test_market_cap_tiers_use_fixed_amounts(self, analysis):
        """看板用 §3.7.4 的固定金额（>500亿/100~500亿/<100亿）；
        与 §3.2 预设的分位数**口径不同、用途不同**，两者可共存。"""
        tiers = {t["key"]: t for t in analysis["market_cap_distribution"]}
        assert set(tiers) >= {"large", "mid", "small"}
        # 合计 + 无数据 = 成员数
        total = sum(t["count"] for t in analysis["market_cap_distribution"])
        assert total == N_MEMBERS

    def test_industry_honestly_unavailable(self, analysis):
        """实测 `symbols.industry` 0 行非空 —— 必须「未知/未披露」，**不造数**。"""
        dist = analysis["industry_distribution"]
        assert len(dist) == 1
        assert "未知" in dist[0]["industry"]
        assert dist[0]["count"] == N_MEMBERS
        assert any("industry" in w for w in analysis["warnings"])

    def test_growth_dimension_honestly_unavailable(self, analysis):
        """成长依赖 net_profit_yoy（实测全 NULL）。填 0 会给 AI 假信号。"""
        dims = analysis["style_exposure"]["dimensions"]
        assert dims["growth"]["available"] is False
        assert dims["growth"]["score"] is None
        assert "net_profit_yoy" in dims["growth"]["reason_zh"]

    def test_four_dimensions_available_with_scores(self, analysis):
        dims = analysis["style_exposure"]["dimensions"]
        for dim in ("value", "quality", "momentum", "volatility"):
            assert dims[dim]["available"] is True, dim
            assert 0.0 <= dims[dim]["score"] <= 1.0, dim

    def test_dominant_style_is_momentum_by_construction(self, analysis):
        """夹具里成员池每日 +0.4% 漂移 → 动量分位应显著高于其它维度。"""
        assert analysis["style_exposure"]["dominant_style"] == "momentum"
        assert analysis["style_exposure"]["dominant_score"] > 0.6

    def test_market_environment_complete(self, analysis):
        me = analysis["market_environment"]
        for key in ("market_regime", "volatility", "trend_strength",
                    "factor_type_suggestions"):
            assert key in me
        assert me["market_regime"]["stage"] in ("bull", "bear", "sideways")
        assert me["volatility"]["band"] in ("high", "mid", "low")
        assert me["trend_strength"]["band"] in ("strong", "mid", "weak")

    def test_factor_suggestions_follow_the_wizard_rules(self, analysis):
        """向导 §3.7.4：震荡市 → 反转★★★/波动率★★☆；高波动 → 波动率收缩★★★/突破★★☆。"""
        suggestions = {s["factor_type"]: s["stars"]
                       for s in analysis["market_environment"]["factor_type_suggestions"]}
        regime = analysis["market_environment"]["market_regime"]["stage"]
        if regime == "sideways":
            assert suggestions.get("reversal") == 3
            assert suggestions.get("volatility") == 2

    def test_data_quality_flags_low_coverage(self, analysis):
        dq = analysis["data_quality"]
        by_field = {f["field"]: f for f in dq["fields"]}
        # dividend_yield 实测全 NULL → 必须标红
        assert by_field["dividend_yield"]["below_threshold"] is True
        assert by_field["dividend_yield"]["coverage"] == 0.0
        assert "dividend_yield" in dq["below_threshold_fields"]
        # close 实测 100% → 不标红
        assert by_field["close"]["below_threshold"] is False

    def test_star_label_format(self, analysis):
        for s in analysis["market_environment"]["factor_type_suggestions"]:
            assert s["stars_label_zh"].count("★") == s["stars"]
            assert len(s["stars_label_zh"]) == 3


# ══════════════════════════════════════════════════════════
# 2. 快照 / 锁定 / 重置
# ══════════════════════════════════════════════════════════


class TestSnapshotLifecycle:
    def test_freeze_captures_members_and_does_not_lock(self, db_session):
        pool, _ids = _make_pool(db_session, MEMBER_CODES)
        snap = S.freeze_snapshot(db_session, pool_id=pool.id, operator_id="test")
        assert snap.member_count == N_MEMBERS
        assert snap.analysis_status == S.SNAPSHOT_NOT_ANALYZED
        # 锁定发生在**分析完成**，不是冻结（向导 §3.7.1）
        assert snap.is_locked == 0
        assert S._locked_snapshot(db_session, pool.id) is None

    def test_freeze_captures_member_snapshot_data(self, db_session):
        pool, _ids = _make_pool(db_session, MEMBER_CODES)
        snap = S.freeze_snapshot(db_session, pool_id=pool.id)
        members = json_loads(snap.members_json)
        assert len(members) == N_MEMBERS
        first = members[0]
        for key in ("symbol_id", "symbol", "name", "market", "board",
                    "industry", "industry_observed_at"):
            assert key in first, f"冻结成员缺 {key}"

    def test_freeze_below_floor_is_blocked(self, db_session):
        codes = [f"S{i:04d}" for i in range(10)]
        _make_pool(db_session, codes)
        with pytest.raises(FactorSevenError) as ei:
            S.freeze_snapshot(db_session, pool_id=S.list_pools(db_session)[0][0].id)
        assert ei.value.error_code == "BUSINESS_BLOCKED"
        assert ei.value.extras["reason"] == "POOL_TOO_SMALL"

    def test_analyze_locks_and_read_operations_still_work(self, db_session, panel):
        pool, _ids = _make_pool(db_session, MEMBER_CODES)
        S.freeze_snapshot(db_session, pool_id=pool.id)
        res = A.analyze_pool(db_session, pool_id=pool.id, panel=panel,
                             as_of_date=date(2026, 8, 21), operator_id="test")
        assert res["is_locked"] is True
        assert res["analysis_status"] == S.SNAPSHOT_ANALYZED
        # 锁定后：写操作被拒
        with pytest.raises(FactorSevenError) as ei:
            S.add_members(db_session, pool.id,
                          symbol_ids=[max(i["symbol_id"] for i in json_loads(
                              S.get_latest_snapshot(db_session, pool.id).members_json))
                              + 1000],
                          operator_id="test")
        assert ei.value.extras["reason"] == "POOL_LOCKED"

    def test_reset_removes_latest_snapshot_and_unlocks(self, db_session, panel):
        pool, _ids = _make_pool(db_session, MEMBER_CODES)
        S.freeze_snapshot(db_session, pool_id=pool.id)
        A.analyze_pool(db_session, pool_id=pool.id, panel=panel,
                       as_of_date=date(2026, 8, 21), operator_id="test")
        assert S._locked_snapshot(db_session, pool.id) is not None

        out = S.reset_analysis(db_session, pool_id=pool.id, operator_id="test")
        assert out["removed_snapshot_id"]
        assert out["remaining_snapshots"] == 0
        assert out["is_locked"] is False
        assert S._locked_snapshot(db_session, pool.id) is None
        # 成员保留（重置只删快照，不动成员）
        assert S.get_pool_detail(db_session, pool.id)["member_count"] == N_MEMBERS

    def test_reset_then_re_freeze_works(self, db_session, panel):
        """向导 §3.7.2：重新选择后按钮回到「生成挖掘物料」→ 应可再次执行。"""
        pool, _ids = _make_pool(db_session, MEMBER_CODES)
        S.freeze_snapshot(db_session, pool_id=pool.id)
        S.reset_analysis(db_session, pool_id=pool.id)
        snap = S.freeze_snapshot(db_session, pool_id=pool.id)   # 不应被拒
        assert snap.member_count == N_MEMBERS
        S.reset_analysis(db_session, pool_id=pool.id)           # 清理

    def test_reset_without_snapshot_is_blocked(self, db_session):
        pool, _ids = _make_pool(db_session, MEMBER_CODES)
        with pytest.raises(FactorSevenError) as ei:
            S.reset_analysis(db_session, pool_id=pool.id)
        assert ei.value.extras["reason"] == "NO_SNAPSHOT"

    def test_multiple_snapshots_kept_for_traceability(self, db_session, panel):
        """同一池多次冻结各生成独立快照（需求 §3.8）；reset 只删**最新**一份，
        历史快照是已挖掘任务的引用物，删了会破坏溯源。"""
        pool, _ids = _make_pool(db_session, MEMBER_CODES)
        s1 = S.freeze_snapshot(db_session, pool_id=pool.id)
        S.reset_analysis(db_session, pool_id=pool.id)
        s2 = S.freeze_snapshot(db_session, pool_id=pool.id)
        assert s1.id != s2.id
        assert len(S.list_snapshots(db_session, pool.id)) == 1
        S.reset_analysis(db_session, pool_id=pool.id)

    def test_frozen_after_second_freeze_blocked_by_lock(self, db_session, panel):
        """分析完成（锁定）后再次冻结 → 被 POOL_LOCKED 拒绝。"""
        pool, _ids = _make_pool(db_session, MEMBER_CODES)
        A.analyze_pool(db_session, pool_id=pool.id, panel=panel,
                       as_of_date=date(2026, 8, 21), operator_id="test")
        with pytest.raises(FactorSevenError) as ei:
            S.freeze_snapshot(db_session, pool_id=pool.id)
        assert ei.value.extras["reason"] == "POOL_LOCKED"


def json_loads(raw):
    import json

    return json.loads(raw) if raw else []


# ══════════════════════════════════════════════════════════
# 3. analyze_pool 的入参防御
# ══════════════════════════════════════════════════════════


class TestAnalyzePoolGuards:
    def test_empty_pool_still_freeze_blocked(self, db_session, panel):
        pool, _ids = _make_pool(db_session, MEMBER_CODES)
        # 把成员全部软删除
        ids = [i["symbol_id"] for i in json_loads(
            S.get_latest_snapshot(db_session, pool.id).members_json)] \
            if S.get_latest_snapshot(db_session, pool.id) else []
        # 直接建池（无成员）来触发地板
        empty = S.create_pool(db_session, name="空池",
                              source_type=S.SOURCE_TYPE_FILTER,
                              filter_config={"markets": ["sh"]})
        with pytest.raises(FactorSevenError) as ei:
            A.analyze_pool(db_session, pool_id=empty.id, panel=panel,
                           as_of_date=date(2026, 8, 21), operator_id="test")
        assert ei.value.extras["reason"] == "POOL_TOO_SMALL"

    def test_analysis_json_persisted_and_round_trips(self, db_session, panel):
        pool, _ids = _make_pool(db_session, MEMBER_CODES)
        res = A.analyze_pool(db_session, pool_id=pool.id, panel=panel,
                             as_of_date=date(2026, 8, 21), operator_id="test")
        snap = S.get_latest_snapshot(db_session, pool.id)
        board = S.snapshot_to_dict(db_session, snap)
        assert board["analysis"]["overview"]["stock_count"] == N_MEMBERS
        assert board["analysis_status"] == S.SNAPSHOT_ANALYZED
        assert board["is_locked"] is True
        assert board["analysis"]["as_of_date"] == "2026-08-21"

    def test_large_members_json_roundtrip_not_truncated(self, db_session):
        """0062 回归：>64KB 成员 JSON 必须完整保存且可解析。

        曾在真实 MySQL 捕获：`members_json` 为 `Text`（65535 字节上限）时，
        数千成员的 JSON 被 MySQL **静默截断** → `json.loads` 抛
        JSONDecodeError → `analyze_pool` 500 → 向导「生成挖掘物料」不可用。
        SQLite 的 TEXT 无长度上限不会截断，本用例作为「写入/读取不截断、
        可完整往返」的契约哨兵（防 model 改回小类型 / 写入端截断回归）。
        """
        import json as _json
        from datetime import datetime as _dt

        from app.models.mining_candidate_pool import TrainingCandidatePoolSnapshot

        big = [
            {"symbol": f"{i:06d}", "name": f"股票{i:05d}", "market": "sz",
             "reason_zh": ("模拟纳入原因" * 5)}
            for i in range(6000)
        ]
        payload = _json.dumps(big, ensure_ascii=False)
        assert len(payload.encode("utf-8")) > 65535, "用例必须超过 MySQL Text 上限"

        pool = S.create_pool(db_session, name="big-pool", source_type="filter",
                             filter_config={"markets": ["sh"]})
        snap = TrainingCandidatePoolSnapshot(
            id="snap-big-1", pool_id=pool.id,
            members_json=payload, rule_hash="",
            data_cutoff_at=_dt(2026, 1, 1),
            stats_json="{}", analysis_status="not_analyzed",
            member_count=len(big), is_locked=0,
        )
        db_session.add(snap)
        db_session.commit()

        back = _json.loads(db_session.get(TrainingCandidatePoolSnapshot, "snap-big-1").members_json)
        assert len(back) == len(big)
        assert back[-1]["symbol"] == f"{len(big) - 1:06d}"


# ══════════════════════════════════════════════════════════
# 4. HTTP 层
# ══════════════════════════════════════════════════════════


def _build_client(db_session) -> TestClient:
    from app.api.routes import mining_candidate_pool as route_mod

    app = FastAPI()
    app.include_router(route_mod.router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


class TestHttpLayer:
    def _pool(self, db_session, panel):
        pool, _ids = _make_pool(db_session, MEMBER_CODES)
        return pool

    def test_materialize_and_read_board(self, db_session, panel, monkeypatch):
        """「生成挖掘物料」端到端：POST snapshot → GET latest。"""
        from app.api.routes import mining_candidate_pool as route_mod

        pool, _ids = _make_pool(db_session, MEMBER_CODES)
        client = _build_client(db_session)

        orig = A.load_analysis_panel

        def _stub(*a, **kw):
            return panel

        monkeypatch.setattr(route_mod.pool_analysis, "load_analysis_panel", _stub)
        try:
            r = client.post(f"/factor-mining/candidate-pools/{pool.id}/snapshot",
                            json={"analyze": True})
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["is_locked"] is True
            assert body["analysis"]["overview"]["stock_count"] == N_MEMBERS

            r2 = client.get(
                f"/factor-mining/candidate-pools/{pool.id}/snapshot/latest")
            assert r2.status_code == 200
            board = r2.json()
            assert board["snapshot_id"] == body["snapshot_id"]
            assert board["is_locked"] is True
        finally:
            monkeypatch.setattr(route_mod.pool_analysis, "load_analysis_panel", orig)

    def test_reset_endpoint(self, db_session, panel, monkeypatch):
        from app.api.routes import mining_candidate_pool as route_mod

        pool, _ids = _make_pool(db_session, MEMBER_CODES)
        client = _build_client(db_session)
        orig = route_mod.pool_analysis.load_analysis_panel
        monkeypatch.setattr(route_mod.pool_analysis, "load_analysis_panel",
                            lambda *a, **kw: panel)
        try:
            client.post(f"/factor-mining/candidate-pools/{pool.id}/snapshot",
                        json={"analyze": True})
            r = client.delete(
                f"/factor-mining/candidate-pools/{pool.id}/snapshot/latest")
            assert r.status_code == 200, r.text
            assert r.json()["is_locked"] is False
        finally:
            monkeypatch.setattr(route_mod.pool_analysis, "load_analysis_panel", orig)

    def test_reset_without_snapshot_returns_4xx(self, db_session):
        pool, _ids = _make_pool(db_session, MEMBER_CODES)
        client = _build_client(db_session)
        r = client.delete(f"/factor-mining/candidate-pools/{pool.id}/snapshot/latest")
        assert 400 <= r.status_code < 500
        assert r.json()["detail"]["extras"]["reason"] == "NO_SNAPSHOT"
