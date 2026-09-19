"""Task 9 — 后端流水线 train gates（4 条 TR 规则覆盖）。

规则映射：
  TR-9.1  train=true 不传 factor_set_id → HTTP 400 / FactorSevenError.error_code = PIPELINE_NO_FACTOR_SET
  TR-9.2  seed 未冻结集合 + train=true 传 fs_id → 400 error_code = PIPELINE_FACTOR_SET_NOT_FROZEN
  TR-9.3  train=true 时 payload.factor_set_id='A' 但 hyperparameters.factor_set_id='B' → PIPELINE_INCONSISTENT_FACTOR_SET
  TR-9.4  mock stage4 训练函数抛 RuntimeError('训练失败模拟') → 5 min 内响应；
          错误结构 detail.stage = 'training'；DB MAX(factor_model_runs.id) 前后保持不变。

仅使用 db_session 临时 SQLite；TR-9.3 通过 FastAPI TestClient 挂载 scoring_facade 薄封路由走前门校验。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── ORM / Schema ─────────────────────────────────────────────────────
from app.models.factor import Factor
from app.models.factor_model import FactorVersion, FactorModelRun
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_runtime import FactorAuditLog
from app.models.async_task import AsyncTaskRecord
from app.schemas.async_task import FactorPipelineCreate
from app.schemas.errors import FactorSevenError

# ── 被测试对象 ───────────────────────────────────────────────────────
from app.services.factors.pipeline_task import (
    _require_ready_training_factor_set,
    _run_factor_pipeline,
    _canonical_stage,
)


# ── 私有夹具 helper ──────────────────────────────────────────────────
def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed_factor_set(
    db_session,
    *,
    factorset_id: str,
    name: str,
    member_count: int = 2,
    feature_count: int | None = None,
    version_status: str = "valid",
    frozen: bool = False,
) -> FactorSet:
    """在 db_session 中构造 FactorSet + Factor + FactorVersion + FactorSetMember。"""
    if feature_count is None:
        feature_count = max(1, member_count - 1)
    fs = FactorSet(
        id=factorset_id,
        name=name,
        status="frozen" if frozen else "draft",
        description="t9 fixture",
        content_hash=("t9_dummy_content_hash_" + factorset_id) if frozen else None,
        frozen_at=_utcnow() if frozen else None,
        created_by="t9_fixture",
    )
    db_session.add(fs)
    db_session.flush()

    for idx in range(member_count):
        role = "feature" if idx < feature_count else "control"
        factor_code = f"{factorset_id}_F{idx}"
        factor = Factor(
            code=factor_code,
            name=f"Factor {idx}",
            category="T9",
            direction="higher_better",
            status="active",
            lifecycle_status="active",
            description="t9",
            default_missing_policy="exclude",
            is_active=1,
        )
        db_session.add(factor)
        db_session.flush()
        fv = FactorVersion(
            factor_id=int(factor.id),
            version=1,
            formula_expr=f"1 as {factor_code}",
            params_json="{}",
            direction="higher_better",
            source_mapping_json="{}",
            is_latest=1,
            validation_status=version_status,
            created_by="t9_fixture",
        )
        db_session.add(fv)
        db_session.flush()
        member = FactorSetMember(
            factor_set_id=factorset_id,
            factor_id=int(factor.id),
            factor_version_id=int(fv.id),
            factor_code=factor_code,
            factor_version=1,
            role=role,
            weight_constraint="positive",
            display_order=idx,
            missing_policy="exclude",
        )
        db_session.add(member)
    db_session.commit()
    db_session.refresh(fs)
    return fs


# ── 阶段映射健康检查 ─────────────────────────────────────────────────
class TestStageMappingSanity:
    def test_canonical_stage_5_values(self):
        """spec §需求 5 阶段映射完整，未知值兜底小写。"""
        assert _canonical_stage("mirror") == "data_sync"
        assert _canonical_stage("factors") == "factor_preprocessing"
        assert _canonical_stage("targets") == "target_generation"
        assert _canonical_stage("train") == "training"
        assert _canonical_stage("score") == "scoring"
        assert _canonical_stage("foobar") == "foobar"
        assert _canonical_stage("") == "unknown"


# ══════════════════════════════════════════════════════════════════════
# TR-9.1：train=true 不传 factor_set_id → PIPELINE_NO_FACTOR_SET
# ══════════════════════════════════════════════════════════════════════
class TestTR9_1_NoFactorSet:
    """rule TR-9.1：train=true 且 factor_set_id = None/''/'0'/未传 →
    FactorSevenError.error_code == 'PIPELINE_NO_FACTOR_SET'（7 要素齐全）。
    """

    @pytest.mark.parametrize("empty_value", [None, "", "   ", "0"])
    def test_tr_9_1_empty_factor_set_id_raises_pipeline_no_factor_set(
        self, db_session, empty_value
    ):
        payload = FactorPipelineCreate(
            train_model=True,
            factor_set_id=empty_value,  # type: ignore[arg-type]
            window_days=250,
            validation_days=50,
        )
        with pytest.raises(FactorSevenError) as ei:
            _require_ready_training_factor_set(payload)
        assert ei.value.error_code == "PIPELINE_NO_FACTOR_SET", (
            f"expected PIPELINE_NO_FACTOR_SET, got {ei.value.error_code}"
        )
        # 7 要素字段非空（BFG 格式）
        d7 = ei.value.to_7field()
        required7 = {
            "error_code", "title_zh", "detail_zh",
            "correlation_id", "impact", "fix_link", "retryable",
        }
        assert required7 <= set(d7.keys()), f"7 要素缺字段: {required7 - set(d7.keys())}"
        assert isinstance(d7["correlation_id"], str) and len(d7["correlation_id"]) >= 8
        assert isinstance(d7["title_zh"], str) and len(d7["title_zh"]) >= 4
        assert isinstance(d7["detail_zh"], str) and len(d7["detail_zh"]) >= 10
        assert d7["retryable"] is False
        assert d7["fix_link"] == "/settings/factors"

    def test_tr_9_1_train_false_empty_factor_set_id_does_not_raise(self, db_session):
        """train=false 时即使不传 factor_set_id，也不触发 gate（无写模型需求）。"""
        payload = FactorPipelineCreate(
            train_model=False,
            factor_set_id=None,
            window_days=250,
            validation_days=50,
        )
        # 不应抛任何异常
        _require_ready_training_factor_set(payload)


# ══════════════════════════════════════════════════════════════════════
# TR-9.2：未冻结集合 → PIPELINE_FACTOR_SET_NOT_FROZEN
# ══════════════════════════════════════════════════════════════════════
class TestTR9_2_NotFrozenFactorSet:
    """rule TR-9.2：seed status=draft 集合，train=true 传 fs_id →
    FactorSevenError.error_code = PIPELINE_FACTOR_SET_NOT_FROZEN。
    """

    def test_tr_9_2_draft_factor_set_raises_not_frozen(self, db_session):
        _seed_factor_set(
            db_session,
            factorset_id="t9_draft",
            name="t9 draft set",
            member_count=2,
            feature_count=1,
            version_status="valid",
            frozen=False,  # status=draft
        )
        payload = FactorPipelineCreate(
            train_model=True,
            factor_set_id="t9_draft",
            window_days=250,
            validation_days=50,
        )
        with pytest.raises(FactorSevenError) as ei:
            _require_ready_training_factor_set(payload)
        assert ei.value.error_code == "PIPELINE_FACTOR_SET_NOT_FROZEN", (
            f"expected PIPELINE_FACTOR_SET_NOT_FROZEN, got {ei.value.error_code}"
        )
        d7 = ei.value.to_7field()
        assert d7["retryable"] is True
        assert "frozen" in d7["detail_zh"] or "draft" in d7["detail_zh"].lower() or "冻结" in d7["detail_zh"]
        # extras 含 factor_set_id 调试信息
        extras = ei.value.extras or {}
        assert extras.get("factor_set_id") == "t9_draft"

    def test_tr_9_2_nonexistent_factor_set_raises_not_found(self, db_session):
        """额外校验：集合不存在时精确码是 PIPELINE_FACTOR_SET_NOT_FOUND（规则 5 错误码中的 ①）。"""
        payload = FactorPipelineCreate(
            train_model=True,
            factor_set_id="TOTALLY_NONEXISTENT_999",
            window_days=250,
            validation_days=50,
        )
        with pytest.raises(FactorSevenError) as ei:
            _require_ready_training_factor_set(payload)
        assert ei.value.error_code == "PIPELINE_FACTOR_SET_NOT_FOUND"

    def test_tr_9_2_empty_frozen_set_raises_empty(self, db_session):
        """额外校验：集合 frozen 但 0 成员 → PIPELINE_FACTOR_SET_EMPTY（规则 5 错误码④）。"""
        fs = FactorSet(
            id="t9_empty_frozen",
            name="t9 frozen empty",
            status="frozen",
            content_hash="t9_dummy_hash_empty",
            frozen_at=_utcnow(),
            created_by="t9",
        )
        db_session.add(fs)
        db_session.commit()
        payload = FactorPipelineCreate(
            train_model=True,
            factor_set_id="t9_empty_frozen",
            window_days=250,
            validation_days=50,
        )
        with pytest.raises(FactorSevenError) as ei:
            _require_ready_training_factor_set(payload)
        assert ei.value.error_code == "PIPELINE_FACTOR_SET_EMPTY"


# ══════════════════════════════════════════════════════════════════════
# TR-9.3：三处不一致 → PIPELINE_INCONSISTENT_FACTOR_SET
# ══════════════════════════════════════════════════════════════════════
class TestTR9_3_InconsistentFactorSetId:
    """rule TR-9.3：train=true 时 payload.factor_set_id='A' 但 hyperparameters.factor_set_id='B'
    → HTTP 400，detail.error_code == PIPELINE_INCONSISTENT_FACTOR_SET。

    通过 FastAPI TestClient 直接挂 scoring_facade 路由调 POST /scoring/tasks 走前门校验。
    """

    @pytest.fixture(scope="class")
    def scoring_client(self):
        """单独创建 FastAPI app，只挂载 scoring_facade 路由避免依赖 app.main。"""
        from app.api.routes.scoring_facade import router as scoring_router

        app = FastAPI()
        app.include_router(scoring_router)
        with TestClient(app) as tc:
            yield tc

    def test_tr_9_3_payload_a_hyperparameters_b_raises_inconsistent(self, scoring_client):
        resp = scoring_client.post(
            "/scoring/tasks",
            json={
                "train_model": True,
                "factor_set_id": "A",
                "hyperparameters": {"factor_set_id": "B"},
                # scope/actor 等其他字段走默认值；window_days/validation_days 不传用默认
                "window_days": 250,
                "validation_days": 50,
            },
        )
        assert resp.status_code == 400, (
            f"expected 400, got {resp.status_code}. body={resp.text[:800]}"
        )
        detail = resp.json().get("detail", {}) if resp.content else {}
        assert isinstance(detail, dict), (
            f"HTTP 400 detail 不是 7 要素字典（可能仍是 legacy 字符串）：detail={resp.text[:500]}"
        )
        assert detail.get("error_code") == "PIPELINE_INCONSISTENT_FACTOR_SET", (
            f"expected PIPELINE_INCONSISTENT_FACTOR_SET, actual error_code={detail.get('error_code')}"
        )
        # 7 要素齐全
        for k in ("error_code", "title_zh", "detail_zh", "correlation_id", "impact", "fix_link", "retryable"):
            assert k in detail, f"7 要素缺少字段 {k}. keys={list(detail.keys())}"
        assert detail["retryable"] is False

    def test_tr_9_3_payload_a_factor_scope_b_raises_inconsistent(self, scoring_client):
        """另一变体：顶层 A / factor_scope B → 也不一致。"""
        resp = scoring_client.post(
            "/scoring/tasks",
            json={
                "train_model": True,
                "factor_set_id": "A",
                "factor_scope": {"factor_set_id": "B"},
                "window_days": 250,
                "validation_days": 50,
            },
        )
        assert resp.status_code == 400
        detail = resp.json().get("detail", {})
        assert detail.get("error_code") == "PIPELINE_INCONSISTENT_FACTOR_SET"

    def test_tr_9_3_all_same_passes_front_door(self, scoring_client, monkeypatch, db_session):
        """三处完全一致时，前门校验通过；进入内部 create_scoring_task 后抛其他错误
        （如 factor_set 不存在）不应是 INCONSISTENT。
        """
        resp = scoring_client.post(
            "/scoring/tasks",
            json={
                "train_model": True,
                "factor_set_id": "SAME_X",
                "hyperparameters": {"factor_set_id": "SAME_X", "window_days": 250},
                "factor_scope": {"factor_set_id": "SAME_X"},
                "window_days": 250,
                "validation_days": 50,
            },
        )
        # 预期失败（系统开关 / 集合不存在等）但不是 PIPELINE_INCONSISTENT_FACTOR_SET
        assert resp.status_code in (400, 500) or (200 <= resp.status_code < 300)
        if resp.status_code == 400:
            detail = resp.json().get("detail", {}) if resp.content else {}
            if isinstance(detail, dict):
                assert detail.get("error_code") != "PIPELINE_INCONSISTENT_FACTOR_SET"


# ══════════════════════════════════════════════════════════════════════
# TR-9.4：stage4 训练抛错 → stage='training' + MAX(model_run.id) 不变
# ══════════════════════════════════════════════════════════════════════
class TestTR9_4_Stage4FailureIsolation:
    """rule TR-9.4：monkeypatch stage4 train_rolling_ridge 抛 RuntimeError('训练失败模拟')
    → 任务在 < 300s 内失败；errors_json[0].extras.stage == 'training'；
    DB 中 MAX(factor_model_runs.id) 前后保持不变（AC-14 失败隔离）。

    策略：不启动完整流水线（依赖 DuckDB 仓库等重量级资源），而是用同步方式
    直接调 _run_factor_pipeline，通过在 stage=train 之前 monkeypatch 内部依赖的
    train_rolling_ridge 使其立即抛 RuntimeError，并把 stage1..3 所有重量级
    步骤（mirror / factors / targets）monkeypatch 成空实现 30ms 返回，
    保证 wall-clock < 5s ≪ 5 min。
    """

    def test_tr_9_4_stage4_train_failure_isolation(self, db_session, monkeypatch):
        # ── 1. seed 一个合格 frozen 集合（写入 DB 供 readiness 通过） ──
        _seed_factor_set(
            db_session,
            factorset_id="t9_stg4",
            name="t9 stage4 failure",
            member_count=2,
            feature_count=1,
            version_status="valid",
            frozen=True,
        )
        db_session.commit()

        # ── 2. 预先记录 MAX(factor_model_runs.id) ──
        def _max_model_run_id() -> int:
            try:
                row = db_session.execute(select(func.max(FactorModelRun.id))).scalar()
            except Exception:
                row = None
            return int(row or 0)

        before_max = _max_model_run_id()

        # ── 3. 插入一条 AsyncTaskRecord（status=queued），payload 合法
        import app.services.factors.pipeline_task as pt_mod

        task_id = "t9_stg4_task_" + uuid4().hex[:12]
        payload_obj = FactorPipelineCreate(
            train_model=True,
            factor_set_id="t9_stg4",
            window_days=250,
            validation_days=50,
            materialize_scores=False,  # 跳过 score 阶段（更聚焦 stage4）
        )
        task_rec = AsyncTaskRecord(
            id=task_id,
            task_type="factor_pipeline",
            status="queued",
            stage="queued",
            percent=0,
            message="queued",
            total=0,
            processed=0,
            ok_count=0,
            failed_count=0,
            payload_json=json.dumps(payload_obj.model_dump(mode="json"), ensure_ascii=False),
            created_at=_utcnow(),
        )
        db_session.add(task_rec)
        db_session.commit()

        # ── 4. monkeypatch 所有重量级依赖 → stage1..3 快速通过，stage4 抛错 ──
        # (a) feature_enabled = True
        from app.services.factors import pipeline_task as pt_file
        def _fake_cfg(*a, **kw):
            class C:
                feature_enabled = True
                warehouse_path = ""
            return C()
        monkeypatch.setattr(pt_file, "get_current_factor_system_config", _fake_cfg, raising=False)
        monkeypatch.setattr(pt_file, "get_factor_system_config", lambda *a, **kw: _fake_cfg(), raising=False)

        # (b) FactorWarehouse：health() 返回空 latest_trade_date
        class FakeWarehouse:
            def __init__(self, *a, **kw):
                pass
            def health(self):
                class H:
                    latest_trade_date = None
                return H()
            def connection(self, *a, **kw):
                class Conn:
                    def __enter__(s):
                        return s
                    def __exit__(s, *e):
                        pass
                    def execute(s, *a, **kw):
                        return None
                    def fetchone(s):
                        return None
                    def fetchall(s):
                        return []
                return Conn()
            def prune_calculation_batches(self, *a, **kw):
                return {"status": "ok"}
        monkeypatch.setattr(pt_file, "FactorWarehouse", FakeWarehouse, raising=False)

        # (c) mirror_daily_bars / mirror_factor_inputs 快速返回
        # 注意：mirror_factor_inputs 返回值被 dataclasses.asdict() 调用，所以必须是 @dataclass
        from dataclasses import dataclass, field
        class FakeBars:
            def to_dict(self):
                return {"rows_written": 0}
        @dataclass
        class FakeInputs:
            rows_written: int = 0
            n_tables: int = 0
            n_errors: int = 0
        monkeypatch.setattr(pt_file, "mirror_daily_bars", lambda *a, **kw: FakeBars(), raising=False)
        monkeypatch.setattr(pt_file, "mirror_factor_inputs", lambda *a, **kw: FakeInputs(), raising=False)

        # (d) calculate_stock_factors / calculate_targets 快速返回（也是 asdict() 调用）
        @dataclass
        class FakeFactors:
            calc_batch_id: str = "fbatch"
            n_rows: int = 0
            n_stocks: int = 0
            n_factors: int = 0
        @dataclass
        class FakeTargets:
            calc_batch_id: str = "tbatch"
            n_rows: int = 0
            n_labels: int = 0
        monkeypatch.setattr(pt_file, "calculate_stock_factors", lambda *a, **kw: FakeFactors(), raising=False)
        monkeypatch.setattr(pt_file, "calculate_targets", lambda *a, **kw: FakeTargets(), raising=False)

        # (e) batch_context：空上下文管理器
        from contextlib import nullcontext
        monkeypatch.setattr(pt_file, "batch_context", lambda *a, **kw: nullcontext(), raising=False)

        # (f) get_factor_runtime_snapshot：跳过 score 阶段
        class FakeRuntime:
            weight_mode = "manual"
            active_model_run_id = None
            def to_dict(self):
                return {"weight_mode": "manual"}
        monkeypatch.setattr(pt_file, "get_factor_runtime_snapshot", lambda *a, **kw: FakeRuntime(), raising=False)

        # (g) heartbeat：monkeypatch 成非阻塞
        stop_event = threading.Event()
        fake_thread = threading.Thread(target=lambda: None, daemon=True)
        monkeypatch.setattr(
            pt_file, "_start_task_heartbeat",
            lambda *a, **kw: (stop_event, fake_thread),
            raising=False,
        )

        # (h) STAGE4 KEY：monkeypatch train_rolling_ridge → 立即抛 RuntimeError('训练失败模拟')
        def _train_fails(*a, **kw):
            raise RuntimeError("训练失败模拟")
        monkeypatch.setattr(pt_file, "train_rolling_ridge", _train_fails, raising=False)

        # (i) _cancelled 永远 False（不取消）
        monkeypatch.setattr(pt_file, "_cancelled", lambda *a, **kw: False, raising=False)

        # ── 5. 同步调用 _run_factor_pipeline（worker 函数），wall-clock 计时 ──
        t0 = time.time()
        # 直接同步执行（线程 worker 函数可同步调用，不依赖 _start_worker）
        try:
            _run_factor_pipeline(task_id)
        except Exception:
            # 理论上 _run_factor_pipeline 内部吞所有异常写 failed；这里兜底防止
            pass
        elapsed = time.time() - t0

        # ── 6. 断言 6-1：耗时 < 300s（5 分钟），且 < 30s 更优
        assert elapsed < 300.0, f"wall-clock {elapsed:.1f}s 超过 5 分钟阈值"

        # ── 7. 重读 AsyncTaskRecord → status=failed，errors_json 含 stage='training' ──
        db_session.expire_all()
        failed_task = db_session.get(AsyncTaskRecord, task_id)
        assert failed_task is not None, "task record missing"
        assert failed_task.status == "failed", (
            f"expected status=failed, actual={failed_task.status}. msg={failed_task.message}"
        )
        errors_raw = failed_task.errors_json or "[]"
        try:
            errs = json.loads(errors_raw)
        except (TypeError, json.JSONDecodeError):
            errs = []
        assert len(errs) >= 1, f"errors_json 为空或解析失败. raw={errors_raw[:300]}"
        err0 = errs[0]
        extras = err0.get("extras") or {}
        # 兼容：也允许 extras 中出现 stage 字段（build_user_error 写入 extras.stage）
        stage_val = (
            extras.get("stage")
            or err0.get("stage")
            or extras.get("attributes", {}).get("stage")
        )
        assert stage_val == "training", (
            f"预期 stage='training'，实际 stage={stage_val!r}. extras={extras}. err0.keys={list(err0.keys())}"
        )

        # ── 8. AC-14：MAX(factor_model_runs.id) 保持不变（不写新模型） ──
        after_max = _max_model_run_id()
        assert after_max == before_max, (
            f"失败隔离破坏：训练失败后 MAX(model_run.id) 从 {before_max} 变成 {after_max}"
        )
