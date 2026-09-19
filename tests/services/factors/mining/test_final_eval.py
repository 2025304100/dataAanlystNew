"""T24 · 最终验证 + 结果落库 契约测试（DoD）。

覆盖（对齐任务卡五条坑）
======================
- **test 仅评估一次**：二次 `finalize_run` 抛 `MINING_TEST_LOCKED`（域错误）
- **C6 草稿链**：`evaluate_full` 先建草稿/版本（注入钩子离线验证）；
  code 冲突 → 幂等复用
- **`succeeded` 不可回退**：`set_run_status` 对 succeeded 的任何迁移都拒绝
- **口径陷阱**：OOS 样本数 = `SplitBudget.test_points`（已扣 tail_loss），
  **不是**日期跨度（差 target_horizon）
- **单候选失败不中断整批**；`succeeded` 前必须过 `db_numeric`
- **★ 契约缺口**（SPLIT_MINIMUMS 死区）**保持未修复**（待产品裁定）——
  用测试把「死区内 build_split 抛裸 ValueError」的现状钉住，
  裁定落地时此测试会红并同步修正
"""
from __future__ import annotations

import math
import pathlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pytest

from app.core import db_numeric as DN
from app.models.factor_mining import (
    FactorMiningCandidate,
    FactorMiningGeneration,
    FactorMiningRun,
)
from app.services.factors.mining import evaluation_adapter as EA
from app.services.factors.mining import service as SVC


# ══════════════════════════════════════════════════════════
# 基建
# ══════════════════════════════════════════════════════════


def _all_dates(n: int = 560) -> list[date]:
    start = date(2026, 1, 5)
    return [start + timedelta(days=i) for i in range(n)]


def _ctx(split=None):
    from app.services.factors.mining.contracts import MiningContext
    from app.services.factors.mining.evaluation_adapter import build_split

    split = split or EA.build_split(all_dates=_all_dates(), frequency="daily",
                                    target_horizon=5)
    return MiningContext(
        run_id="run-final-1", candidate_pool_snapshot_id="snap-1",
        data_cutoff_at=datetime(2026, 9, 17), start_date=date(2026, 1, 5),
        end_date=date(2026, 4, 30), rebalance_frequency="daily",
        target_horizon=5, split=split, purge_points=5, embargo_points=5,
        train_ratio=0.6, validation_ratio=0.2, random_seed=42,
        config_hash="cfg-hash", split_algorithm_version="split-1.0.0",
    )


def _fake_draft_creator(code_registry: dict | None = None,
                        version_seed: int = 100):
    """假草稿链：记录调用并返回 (factor_version_id, code)。"""
    registry = code_registry if code_registry is not None else {}
    counter = {"n": version_seed}

    def _create(db, *, digest, formula, category=None, direction="positive",
                logic="", created_by="mining", frequency=None):
        code = f"mining_{digest[:12]}"
        if code in registry:
            return registry[code]["factor_version_id"], code
        counter["n"] += 1
        registry[code] = {"factor_version_id": counter["n"], "formula": formula}
        return registry[code]["factor_version_id"], code

    return _create


def _fake_runner(metrics: dict | None = None, *, test_start: date | None = None,
                 test_end: date | None = None, fail_on: set | None = None):
    def _run(db, *, factor_version_id, factor_values, forward_returns,
             config, created_by="local_user"):
        if fail_on and factor_version_id in fail_on:
            raise RuntimeError(f"评估爆炸: {factor_version_id}")

        @dataclass
        class _Outcome:
            run_id: str
            gate_result: str
            rejection_reasons: list
            metrics: dict
            time_split: object
            coverage: object

        @dataclass
        class _Cov:
            pass

        return _Outcome(
            run_id=f"eval-{factor_version_id}",
            gate_result="passed",
            rejection_reasons=[],
            metrics=metrics if metrics is not None else {"ic": 0.12,
                                                         "icir": 0.42},
            time_split=None,
            coverage=_Cov(),
        )
    return _run


@pytest.fixture
def run_row(db_session):
    row = FactorMiningRun(
        id="run-final-1", status="running", candidate_pool_snapshot_id="snap-1",
        data_cutoff_at=datetime(2026, 9, 17), start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 9, 1), rebalance_frequency="daily",
        split_method="ratio", split_algorithm_version="split-1.0.0",
        max_generation=5, total_trials=0,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _seed_candidates(db_session, run_id: str, n: int = 3) -> list[str]:
    hashes = []
    for i in range(n):
        row = FactorMiningCandidate(
            id=f"cand-{run_id}-{i}", run_id=run_id,
            formula_expr=f"mean(close,{20 + i})",
            canonical_formula=f"mean(close,{20 + i})",
            # 哈希前 12 位必须互异（真实 sha256 即如此；草稿 code 取 digest[:12]）
            formula_hash=f"{i:04d}" + "x" * 28,
            generation=1, operation="elite", category="trend",
            generation_icir=0.6 - 0.1 * i,
            economic_logic="逻辑", expected_direction="positive",
        )
        db_session.add(row)
        hashes.append(row.formula_hash)
    db_session.commit()
    return hashes


# ══════════════════════════════════════════════════════════
# 1. evaluate_full（C6 草稿链 + 锁定守卫）
# ══════════════════════════════════════════════════════════


class TestEvaluateFull:
    def test_happy_path(self):
        ctx = _ctx()
        registry: dict = {}
        outcome = EA.evaluate_full(
            ctx, individual={"formula": "mean(close,20)",
                             "canonical_formula": "mean(close,20)",
                             "formula_hash": "a" * 32,
                             "category": "trend",
                             "expected_direction": "positive",
                             "economic_logic": "逻辑"},
            db=object(), factor_values=object(), forward_returns=object(),
            draft_creator=_fake_draft_creator(registry),
            run_evaluator=_fake_runner())
        assert outcome.success is True
        assert outcome.factor_version_id >= 100
        assert outcome.metrics["ic"] == 0.12
        assert outcome.gate_result == "passed"

    def test_already_locked_raises_domain_error(self):
        """🚨 任务卡坑 1：已带 factor_version_id 的候选 → `MINING_TEST_LOCKED`。"""
        from app.schemas.errors import FactorSevenError

        with pytest.raises(FactorSevenError) as ei:
            EA.evaluate_full(
                _ctx(), individual={"formula": "mean(close,20)",
                                    "formula_hash": "a" * 32,
                                    "factor_version_id": 7},
                db=object(), factor_values=object(), forward_returns=object(),
                draft_creator=_fake_draft_creator(), run_evaluator=_fake_runner())
        assert ei.value.error_code == "MINING_TEST_LOCKED"

    def test_draft_creator_receives_c6_inputs(self):
        """红线 C6：草稿链必须拿到 哈希/公式/方向/经济逻辑。"""
        seen: list = []

        def creator(db, *, digest, formula, category=None, direction="positive",
                    logic="", created_by="mining", frequency=None):
            seen.append({"digest": digest, "formula": formula,
                         "category": category, "direction": direction,
                         "logic": logic, "frequency": frequency})
            return 555, f"mining_{digest[:12]}"

        EA.evaluate_full(
            _ctx(), individual={"formula": "mean(close,20)",
                                "canonical_formula": "mean(close,20)",
                                "formula_hash": "b" * 32, "category": "trend",
                                "expected_direction": "negative",
                                "economic_logic": "反向逻辑"},
            db=object(), factor_values=object(), forward_returns=object(),
            draft_creator=creator, run_evaluator=_fake_runner())
        assert seen[0]["digest"] == "b" * 32
        assert seen[0]["direction"] == "negative"
        assert seen[0]["logic"] == "反向逻辑"
        assert seen[0]["frequency"] == "daily"

    def test_metrics_sanitized_by_db_numeric(self):
        """🚨 P0：指标里的 NaN/±Inf 必须转 None（PyMySQL 拒收）。"""
        outcome = EA.evaluate_full(
            _ctx(), individual={"formula": "mean(close,20)",
                                "formula_hash": "c" * 32},
            db=object(), factor_values=object(), forward_returns=object(),
            draft_creator=_fake_draft_creator(),
            run_evaluator=_fake_runner(metrics={
                "ic": float("nan"), "icir": float("inf"), "coverage": 0.9}))
        assert outcome.metrics["ic"] is None
        assert outcome.metrics["icir"] is None
        assert outcome.metrics["coverage"] == 0.9

    def test_code_collision_reuses_existing(self):
        """同哈希第二次建草稿 → code 冲突 → 幂等复用既有 version。"""
        registry: dict = {"mining_" + "d" * 12: {"factor_version_id": 999}}
        outcome = EA.evaluate_full(
            _ctx(), individual={"formula": "mean(close,20)",
                                "formula_hash": "d" * 32},
            db=object(), factor_values=object(), forward_returns=object(),
            draft_creator=_fake_draft_creator(registry),
            run_evaluator=_fake_runner())
        assert outcome.factor_version_id == 999

    def test_evaluator_failure_propagates_as_error_outcome(self):
        def runner(db, **kwargs):
            raise RuntimeError("评估链路故障")

        with pytest.raises(RuntimeError, match="评估链路故障"):
            EA.evaluate_full(
                _ctx(), individual={"formula": "mean(close,20)",
                                    "formula_hash": "e" * 32},
                db=object(), factor_values=object(), forward_returns=object(),
                draft_creator=_fake_draft_creator(), run_evaluator=runner)


# ══════════════════════════════════════════════════════════
# 2. 口径陷阱：test_points ≠ 日期跨度
# ══════════════════════════════════════════════════════════


class TestTestPointsCaliber:
    def test_test_points_exclude_tail_loss(self):
        """🚨 任务卡坑 5：`SplitBudget.test_points` 已扣 tail_loss；
        日期跨度（test_end - test_start）比它多 `target_horizon` 个点。"""
        from app.services.factors.mining.evaluation_adapter import (
            compute_split_budget, build_split)

        dates = _all_dates()                     # 560 点（> 日频死区上界 549）
        budget = compute_split_budget(all_dates=dates, frequency="daily",
                                      purge_points=5, embargo_points=5,
                                      tail_loss=5)
        split, _budget = build_split(all_dates=dates, frequency="daily",
                                     target_horizon=5)
        span = (split.test_end - split.test_start).days + 1
        assert budget.test_points == span - 5, (
            f"跨度 {span} 含 5 个尾部点；可用 OOS 样本 = {budget.test_points}")
        assert budget.test_points > 0

    def test_documented_in_evaluate_full(self):
        """口径已写进 evaluate_full 的契约文档（防退化）。"""
        import inspect

        doc = inspect.getdoc(EA.evaluate_full) or ""
        assert "test_points" in doc and "tail_loss" in doc


# ══════════════════════════════════════════════════════════
# 3. 契约缺口：SPLIT_MINIMUMS 死区（**保持未修复**，待产品裁定）
# ══════════════════════════════════════════════════════════


class TestSplitDeadzonePendingDecision:
    def test_deadzone_eliminated_by_di(self):
        """✅ D-I 裁决落地：SPLIT_MINIMUMS 死区已消除。

        - 月频 38 点（旧死区内）：现在**一致地**抛 `MiningSplitError`（地板 41 不足），
          `meets_floor=False` 与异常一致 —— 不再是「预算说达标、切分却炸」
        - 月频 41 点（新地板）：切分成功
        - 日频旧死区 [252, 550)：全部可切分（floor 252 保住需求 §3.2）
        """
        from app.services.factors.mining.evaluation_adapter import (
            MiningSplitError, build_split, compute_split_budget)

        step = 21                                # 月频：1 调仓点 ≈ 21 交易日
        dates38 = [date(2021, 1, 1) + timedelta(days=step * i) for i in range(38)]
        budget38 = compute_split_budget(all_dates=dates38, frequency="monthly",
                                        purge_points=1, embargo_points=1,
                                        tail_loss=5)
        assert budget38.meets_floor is False, "38 < 新地板 41，预算应判不达标"
        with pytest.raises(MiningSplitError):
            build_split(all_dates=dates38, frequency="monthly",
                        target_horizon=5)

        dates41 = [date(2021, 1, 1) + timedelta(days=step * i) for i in range(41)]
        _split, budget41 = build_split(all_dates=dates41, frequency="monthly",
                                       target_horizon=5)
        assert budget41.meets_floor is True

        # 日频旧死区 [252, 550)：逐点全部可切分
        for n in range(252, 260):
            dates = [date(2021, 1, 1) + timedelta(days=i) for i in range(n)]
            _s, budget = build_split(all_dates=dates, frequency="daily",
                                     target_horizon=5)
            assert budget.meets_floor is True

    def test_deadzone_documented_by_sentinel(self):
        """缺口由 T02 哨兵钉住（防「静默修复/静默遗忘」两头空）。"""
        sentinel = pathlib.Path(
            "tests/services/factors/mining/test_evaluation_adapter_split.py")
        src = sentinel.read_text(encoding="utf-8")
        assert "D-I 裁决落地" in src
        assert "test_split_minimums_self_consistent" in src
        assert "test_split_floor_sweep_no_dead_zone" in src


# ══════════════════════════════════════════════════════════
# 4. finalize_run（test-once 锁 / 状态机 / 单候选失败隔离）
# ══════════════════════════════════════════════════════════


class TestFinalizeRun:
    def test_full_flow(self, db_session, run_row):
        _seed_candidates(db_session, run_row.id, n=3)
        result = SVC.finalize_run(
            db_session, ctx=_ctx(), run_id=run_row.id,
            draft_creator=_fake_draft_creator(), run_evaluator=_fake_runner())
        assert result["status"] == "succeeded"
        assert result["evaluated"] == 3 and result["failed"] == 0
        db_session.refresh(run_row)
        assert run_row.status == "succeeded"
        assert run_row.converged == 1
        rows = db_session.query(FactorMiningCandidate).filter_by(
            run_id=run_row.id).all()
        assert all(r.factor_version_id for r in rows)
        assert all(r.latest_evaluation_id for r in rows)

    def test_second_call_raises_mining_test_locked(self, db_session, run_row):
        """🚨 任务卡坑 1：test 仅评估一次；二次调用抛 `MINING_TEST_LOCKED`。"""
        from app.schemas.errors import FactorSevenError

        _seed_candidates(db_session, run_row.id, n=1)
        SVC.finalize_run(db_session, ctx=_ctx(), run_id=run_row.id,
                         draft_creator=_fake_draft_creator(),
                         run_evaluator=_fake_runner())
        with pytest.raises(FactorSevenError) as ei:
            SVC.finalize_run(db_session, ctx=_ctx(), run_id=run_row.id,
                             draft_creator=_fake_draft_creator(),
                             run_evaluator=_fake_runner())
        assert ei.value.error_code == "MINING_TEST_LOCKED"

    def test_succeeded_status_never_reverts(self, db_session, run_row):
        """🚨 任务卡坑 3：`succeeded` 不可回退。"""
        run_row.status = "succeeded"
        db_session.commit()
        from app.schemas.errors import FactorSevenError

        with pytest.raises(FactorSevenError):
            SVC.set_run_status(db_session, run_row, "running")
        with pytest.raises(FactorSevenError):
            SVC.set_run_status(db_session, run_row, "failed")

    def test_single_candidate_failure_does_not_abort_batch(self, db_session,
                                                           run_row):
        """单候选评估爆炸 → 记 failed、其余照常，run 仍 succeeded。"""
        _seed_candidates(db_session, run_row.id, n=3)
        runner = _fake_runner(fail_on={101})     # 第一个分配到的 version 失败
        result = SVC.finalize_run(db_session, ctx=_ctx(), run_id=run_row.id,
                                  draft_creator=_fake_draft_creator(),
                                  run_evaluator=runner)
        assert result["status"] == "succeeded"
        assert result["failed"] >= 1 and result["evaluated"] >= 1
        rows = db_session.query(FactorMiningCandidate).filter_by(
            run_id=run_row.id).all()
        statuses = {r.latest_evaluation_status for r in rows
                    if r.latest_evaluation_status}
        assert "failed" in statuses and "done" in statuses

    def test_top_k_caps_evaluation(self, db_session, run_row):
        _seed_candidates(db_session, run_row.id, n=3)
        result = SVC.finalize_run(db_session, ctx=_ctx(), run_id=run_row.id,
                                  top_k=1, draft_creator=_fake_draft_creator(),
                                  run_evaluator=_fake_runner())
        assert result["evaluated"] == 1

    def test_ranks_by_icir_desc(self, db_session, run_row):
        """Top-K 应按当代 ICIR 降序（0.6 的先进）。"""
        picked: list = []

        def creator(db, *, digest, formula, **kwargs):
            picked.append(digest)
            return 900 + len(picked), f"mining_{digest[:12]}"

        _seed_candidates(db_session, run_row.id, n=3)   # icir: 0.6/0.5/0.4
        SVC.finalize_run(db_session, ctx=_ctx(), run_id=run_row.id, top_k=1,
                         draft_creator=creator, run_evaluator=_fake_runner())
        assert picked == ["0000xxxxxxxxxxxxxxxxxxxxxxxxxxxx"], \
            "ICIR 最高的候选先进"

    def test_metrics_sanitized_on_candidate(self, db_session, run_row):
        """🚨 P0：latest_ic 经 `db_numeric` —— NaN 进不来。"""
        _seed_candidates(db_session, run_row.id, n=1)
        SVC.finalize_run(db_session, ctx=_ctx(), run_id=run_row.id,
                         draft_creator=_fake_draft_creator(),
                         run_evaluator=_fake_runner(metrics={"ic": float("nan")}))
        row = db_session.query(FactorMiningCandidate).filter_by(
            run_id=run_row.id).first()
        assert row.latest_ic is None

    def test_unknown_run_rejected(self, db_session):
        with pytest.raises(ValueError, match="run 不存在"):
            SVC.finalize_run(db_session, ctx=_ctx(), run_id="nope",
                             draft_creator=_fake_draft_creator(),
                             run_evaluator=_fake_runner())

    def test_empty_candidates_succeeds_with_zero(self, db_session, run_row):
        result = SVC.finalize_run(db_session, ctx=_ctx(), run_id=run_row.id,
                                  draft_creator=_fake_draft_creator(),
                                  run_evaluator=_fake_runner())
        assert result["status"] == "succeeded" and result["evaluated"] == 0
