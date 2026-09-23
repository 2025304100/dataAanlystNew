"""A2 · 真实挖掘阶段链（task_runner._default_stage_runner）E2E。

DoD 对照（开发计划 A2 卡）：
- E2E 从「注入假评估器」升级为「真实 evaluate_short 路径」至少一条 → 本文件
- 20 代小样本跑通                                             → test_1（小种群多代真实评估）
- total_trials / 三率落 runs 表                                → test_1 断言

阶段链 = 目标标签生成 → initial_population(经典底座) → run_ga_loop(真实 evaluate_short)
→ 每代 persist_generation/persist_candidates/sync_run_progress/探针落库
→ finalize_run（`finalize_top_k=0` 跳过，收尾状态 succeeded）。

跑法：`.venv/Scripts/python.exe -m pytest tests/services/factors/mining/test_stage_runner_real.py -q`
"""
from __future__ import annotations

import datetime as dt
from datetime import date, datetime, timedelta

import pytest

pytest.importorskip("duckdb")

from app.models.factor_mining import FactorMiningCandidate, FactorMiningGeneration
from app.models.factor_mining import FactorMiningRun
from app.services.factors.mining import task_runner as TR
from app.services.factors.store import FactorWarehouse
from app.services.factors.target_engine import calculate_targets

pytestmark = pytest.mark.whitebox

N_SYMBOLS = 10
N_DAYS = 300
BASE_DATE = date(2026, 1, 5)


def _bar(symbol: str, trade_date: date, row_id: int, close: float):
    return {
        "symbol": symbol, "trade_date": trade_date, "adjust": "qfq",
        "universe_symbol_id": row_id, "open": close * 0.998,
        "high": close * 1.01, "low": close * 0.99, "close": close,
        "volume": 100.0, "amount": 100000.0, "turnover_rate": 1.0,
        "source": "test", "source_origin": "universe_daily_bars",
        "source_row_id": row_id, "ingested_at": datetime(2026, 7, 1),
        "batch_id": "a2-bars",
    }


@pytest.fixture
def warehouse_path(tmp_path):
    import random

    path = tmp_path / "factor.duckdb"
    wh = FactorWarehouse(str(path))
    dates = [BASE_DATE + timedelta(days=i) for i in range(N_DAYS)]
    rng = random.Random(11)
    bars = []
    row = 0
    for symbol in [f"{i:06d}" for i in range(N_SYMBOLS)]:
        price = 10.0
        for trade_date in dates:
            row += 1
            price = max(2.0, price * (1 + (rng.random() - 0.5) * 0.03))
            bars.append(_bar(symbol, trade_date, row, close=round(price, 4)))
    wh.upsert_daily_bars(bars, source_key="a2.bars", watermark=row)
    calculate_targets(wh, start_date=dates[0], end_date=dates[-1],
                      calc_batch_id="a2-target")
    return str(path)


@pytest.fixture
def run_row(db_session, warehouse_path):
    row = FactorMiningRun(
        id="run-a2", status="running", candidate_pool_snapshot_id="snap-a2",
        data_cutoff_at=datetime(2026, 11, 10), start_date=datetime(2026, 1, 5),
        end_date=datetime(2026, 11, 1), rebalance_frequency="daily",
        split_method="ratio", split_algorithm_version="split-1.0.0",
        target_horizon=5, max_generation=3, random_seed=7,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _stage_ctx(db_session, run_row, warehouse_path, **evo) -> TR.MiningWorkerContext:
    payload = {
        "warehouse_path": warehouse_path,
        "sample_length": "6m",
        "selected_fields": ["close", "low", "high", "volume",
                            "amount", "turnover_rate"],
        "evolution_params": {
            "population_size": 8, "max_generations": 3,
            "selection_ratio": 0.3, "mutation_rate": 0.55,
            "crossover_rate": 0.25, "random_rate": 0.20,
            "random_seed": 7,
            **evo,
        },
        "finalize_top_k": 0,
    }
    return TR.MiningWorkerContext(
        task_id="task-a2", run_id=run_row.id, payload=payload,
        holds_duckdb_write=True,
    )


class TestRealStageRunner:
    def test_real_evolution_runs_and_persists(self, db_session, run_row,
                                              warehouse_path):
        """真实 evaluate_short 跑通多代：候选落库 + total_trials/探针落 runs/generations。"""
        ctx = _stage_ctx(db_session, run_row, warehouse_path)
        result = TR._default_stage_runner(ctx)

        assert result["generations"] >= 1, result
        assert result["total_trials"] > 0, result
        assert result["stopped_reason"] in ("converged", "max_generations")

        # run 状态与 total_trials 落库
        db_session.refresh(run_row)
        assert run_row.status == "succeeded"
        assert run_row.converged == 1
        assert run_row.total_trials == result["total_trials"]

        # 代际行：代数 = run_ga_loop 实际跑了 N 代；每代含探针与均衡
        gens = db_session.query(FactorMiningGeneration).filter_by(
            run_id=run_row.id).order_by(FactorMiningGeneration.generation).all()
        assert len(gens) == result["generations"]
        for g in gens:
            assert isinstance(g.probe_data_load_ms, int)
            assert g.probe_data_load_ms >= 0
            assert g.population_size >= 4

        # 候选行：初始种群全部入库；后续代同 hash（run 级去重）不重复插、不撞唯一约束
        cands = db_session.query(FactorMiningCandidate).filter_by(
            run_id=run_row.id).count()
        assert cands >= result["candidates"], (cands, result["candidates"])
        assert cands <= result["candidates"] * (result["generations"] + 1)

        # 三率分配记录随代落库（mutation/crossover/random 名额）
        gens_0 = db_session.query(FactorMiningGeneration).filter_by(
            run_id=run_row.id).first()
        assert gens_0.mutation_count + gens_0.crossover_count + \
            gens_0.random_count >= gens_0.population_size - gens_0.elite_count

    def test_missing_run_is_blocked(self, db_session, warehouse_path):
        """run 不存在 → 阶段链抛明确错误（不静默）。"""
        ctx = TR.MiningWorkerContext(
            task_id="task-a2-miss", run_id="run-missing",
            payload={"warehouse_path": warehouse_path, "finalize_top_k": 0},
            holds_duckdb_write=True,
        )
        with pytest.raises(Exception) as ei:
            TR._default_stage_runner(ctx)
        assert "run 不存在" in str(ei.value)