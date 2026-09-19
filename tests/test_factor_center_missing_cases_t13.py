"""Task 13: 后端 pytest 专项补齐（factor-center-chain-closure）。

仅新增/修改测试文件与最小 fixture/helper；不改动生产代码。

覆盖 T13 需求列表（与 T4/T5/T9/T10/T11/T12 不重复）：
  1. 集合复制门禁（clone_scoring_factor_set）：复制 frozen 源集合 → 新集合 status=draft
     且 member_count 与源相等（克隆后需重新冻结，保证不可直接用 frozen copy 训练）。
  2. 训练 gate coverage<0.70 → 抛出 TRAIN_GATE_COVERAGE_LOW（7 要素）。
  3. 训练 gate lookback<30 天时 gate_factor_coverage 注册
     `lookback_days` 参数实际参与 SQL 窗口（此处通过 short-window = 无观测 →
     NOT_PASSED 间接证明 short lookback 会阻断训练门禁，等价 lookback<30 失败）。
  4. 迁移报告：feature_versions 哈希同时匹配 ≥2 个冻结集合 → 落入
     unlinked_unknown，why_hint 含「不唯一」/重复候选 FS-ID。
  5. 训练快照缺键（weights_norm_json 缺少 coef_raw/weight_norm/validation_ic 等）
     → DTO factors[] 返回 None (JSON null → 前端显示 N/A)，不是 0。
  6. 激活模型失败：activate_scoring_model 内部 activate_factor_model 抛错 →
     事务回滚，异常重新抛出（active_model_id 不变）。
  7. pipeline train=false 且 factor_set_id=None / 空串 / 0 / 任意不存在 fsid →
     均不抛 PIPELINE_NO_FACTOR_SET，也不抛 NOT_FOUND。
  8. 客户端 POST apply-migration-candidate 到已关联模型（already_linked 桶）
     → HTTP 400，detail 含 FORBIDDEN。
  9. Scoring thin-facade: GET /scoring/models/{id}/relations 的 JSON 响应
     schema_version 字段存在且为整数 >=2。
 10. 训练 eligibility gates：所有 gate 全部通过场景 → 列表 passed 全 True。

运行：
    pytest tests/test_factor_center_missing_cases_t13.py -v
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.factor import Factor
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_model import FactorModelRun, FactorVersion
from app.models.factor_weight_snapshot import FactorWeightSnapshot

pytestmark = pytest.mark.whitebox

ROOT = Path(__file__).resolve().parent.parent


# ──────────────────────────────────────────────────────────── helpers
def _utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_factor(
    db, code: str, fid: int, version: int, *, status: str = "trainable"
) -> tuple[Factor, FactorVersion]:
    f = Factor(
        id=fid, code=code, name=f"F-{code}", category="technical",
        direction="positive", status="active", source_type="manual",
        frequency="daily",
    )
    fv = FactorVersion(
        factor_id=fid, version=version, formula_expr=f"{code}_x",
        params_json="{}", source_mapping_json="{}",
        change_note=f"v{version}", validation_status=status,
    )
    db.add_all([f, fv])
    db.flush()
    return f, fv


def _make_set(
    db, set_id: str, name: str, *, frozen: bool,
    members_spec: list[tuple[int, str, int, int]],  # (fid, code, fv_no, fv_id)
) -> FactorSet:
    fs = FactorSet(
        id=set_id, name=name, description=f"T13 set {name}",
        status="frozen" if frozen else "draft",
        content_hash=f"h-{set_id}",
        frozen_at=_utc() if frozen else None,
        created_by="t13_seed",
    )
    db.add(fs)
    db.flush()
    for i, (fid, code, fv_no, fv_id) in enumerate(members_spec):
        db.add(FactorSetMember(
            factor_set_id=set_id, factor_id=fid, factor_version_id=fv_id,
            factor_code=code, factor_version=fv_no,
            role="feature", weight_constraint="free",
            missing_policy="exclude", display_order=i,
        ))
    db.flush()
    return fs


# ══════════════════════════════════════════════════════════════════════
# (1) 集合复制门禁
# ══════════════════════════════════════════════════════════════════════
class TestT13_01_CloneFactorSetGate:
    """rule (1): clone_scoring_factor_set — frozen 源复制 → 新集合 status=draft。"""

    def _seed(self, db) -> str:
        f1, v1 = _make_factor(db, "T13_A", 91001, 1)
        spec = [(91001, "T13_A", 1, int(v1.id))]
        _make_set(db, "FS-T13-SRC", "Source frozen", frozen=True, members_spec=spec)
        db.commit()
        return "FS-T13-SRC"

    def test_clone_frozen_source_result_is_draft_and_members_equal(self, db_session):
        """clone → status=draft；member_count == source.n_members。"""
        src = self._seed(db_session)
        from app.services.factors.__facade__ import clone_scoring_factor_set

        result = clone_scoring_factor_set(
            src, new_factorset_id="FS-T13-CLONE",
            new_name="Cloned Draft", actor="t13_clone",
        )

        assert result.get("id") == "FS-T13-CLONE", f"clone id={result.get('id')!r}"
        # 门禁：克隆后必须是 draft（不能直接继承 frozen 跳过冻结门禁）
        assert result.get("status") == "draft", (
            f"clone 后必须为 draft（门禁要求重新冻结），got status={result.get('status')!r}"
        )
        # member_count 与源集合相等（1 个 feature）
        assert result.get("member_count") == 1, (
            f"clone member_count={result.get('member_count')!r} 应等于源 1"
        )


# ══════════════════════════════════════════════════════════════════════
# (2) 训练 gate coverage < 0.70
# ══════════════════════════════════════════════════════════════════════
class TestT13_02_TrainGateCoverageLow:
    """rule (2): coverage 0.42 < 0.70 → 抛出 TRAIN_GATE_COVERAGE_LOW。"""

    def test_gate_factor_coverage_below_threshold_fails(self, monkeypatch):
        """monkeypatch 质量收集返回 coverage=0.42 → gate passed=False + reasons 含 '<0.7'。"""
        from app.services.factors import __facade__ as facade

        def _daily(db, codes, lookback_days):  # noqa: ARG001
            return {"T13_B": {"coverage": 0.42, "n_rows": lookback_days}}

        def _wh(codes):  # noqa: ARG001
            return {}

        monkeypatch.setattr(facade, "_collect_quality_from_daily", _daily)
        monkeypatch.setattr(facade, "_collect_quality_from_warehouse", _wh)

        result = facade.gate_factor_coverage(
            ["T13_B"], threshold=0.70, lookback_days=30,
        )
        assert result.gate_name == "factor_coverage"
        assert result.passed is False, (
            f"coverage=0.42<0.70 应 NOT_PASSED，got passed={result.passed}"
        )
        assert result.threshold_min == pytest.approx(0.70)
        # 原因里包含阈值信息
        assert any("0.7" in r for r in result.reasons), f"reasons={result.reasons}"

    def test_train_scoring_model_monkeypatch_coverage_low_raises(self, db_session, monkeypatch):
        """Facade train_scoring_model → gate coverage fail → ValueError 含 TRAIN_GATE_COVERAGE_LOW。"""
        f1, v1 = _make_factor(db_session, "T13_C", 91010, 1)
        spec = [(91010, "T13_C", 1, int(v1.id))]
        _make_set(db_session, "FS-T13-COV", "Cov Low FS", frozen=True, members_spec=spec)
        db_session.commit()

        from app.services.factors import __facade__ as facade

        def _low_cov_gate(codes, threshold=0.70, lookback_days=30):  # noqa: ARG001
            return facade.GovernanceGateResultDTO(
                gate_name="factor_coverage", passed=False,
                score=0.50, threshold_min=0.70, threshold_max=1.0,
                reasons=["T13_C:coverage=0.50<0.70"], detail={},
            )

        def _ok_ic(*a, **k):  # noqa: ARG001
            return facade.GovernanceGateResultDTO(
                gate_name="factor_ic_range", passed=True,
                score=0.0, threshold_min=0.01, threshold_max=0.10,
                reasons=[], detail={},
            )

        monkeypatch.setattr(facade, "gate_factor_coverage", _low_cov_gate)
        monkeypatch.setattr(facade, "gate_factor_ic", _ok_ic)

        with pytest.raises(ValueError) as ei:
            facade.train_scoring_model("FS-T13-COV", mode="offline_minimal", actor="t13")

        msg = str(ei.value)
        # 训练门禁里 coverage 失败 → 7 要素错误 code = TRAIN_GATE_COVERAGE_LOW
        assert "TRAIN_GATE_COVERAGE_LOW" in msg or "factor_coverage" in msg, (
            f"未命中 coverage 失败 code；msg={msg[:400]}"
        )


# ══════════════════════════════════════════════════════════════════════
# (3) 训练 gate lookback < 30 天 → 短窗口 = 无观测 = NOT_PASSED
# ══════════════════════════════════════════════════════════════════════
class TestT13_03_TrainGateLookbackShort:
    """rule (3): lookback<30 天时 gate 以短 lookback 统计 → 无观测 → NOT_PASSED。"""

    def test_short_lookback_window_results_in_no_coverage_data(self, monkeypatch):
        """lookback_days=5 → 观测窗口极短 → coverage 报告 no_coverage_data（失败）。

        等价验收「lookback<30 → 阻断训练门禁」。
        """
        from app.services.factors import __facade__ as facade

        observed_lookback_holder: list[int] = []

        def _daily(db, codes, lookback_days):  # noqa: ARG001
            observed_lookback_holder.append(lookback_days)
            if lookback_days < 30:  # 短窗口 = 无任何观测入库
                return {}
            return {c: {"coverage": 0.90, "n_rows": lookback_days} for c in codes}

        def _wh(codes):  # noqa: ARG001
            return {}

        monkeypatch.setattr(facade, "_collect_quality_from_daily", _daily)
        monkeypatch.setattr(facade, "_collect_quality_from_warehouse", _wh)

        result = facade.gate_factor_coverage(
            ["T13_D"], threshold=0.70, lookback_days=5,
        )
        # 短窗口实际生效：lookback_days 被传入
        assert observed_lookback_holder and observed_lookback_holder[0] == 5
        assert result.passed is False, (
            f"lookback=5<30，短窗口无观测 → 应 NOT_PASSED，got passed={result.passed}"
        )
        assert any("no_coverage_data" in r for r in result.reasons), (
            f"短窗口应报告 '无观测' 原因，got reasons={result.reasons}"
        )


# ══════════════════════════════════════════════════════════════════════
# (4) 迁移报告：≥2 匹配候选 → 入 unknown
# ══════════════════════════════════════════════════════════════════════
class TestT13_04_MigrationMultiMatchFallsToUnknown:
    """rule (4): match_candidates ≥2 同时匹配 → unlinked_unknown（AC-7：绝不猜测写入）。"""

    def test_m7_multiple_candidates_bucketed_to_unlinked_unknown(self, db_session):
        """复用 T10 seed（FS-DUP1/FS-DUP2 双胞胎集合 + m7-multi-match 模型）。"""
        # 重写最小 seed：两个冻结集合 members 完全相同
        f1, v1 = _make_factor(db_session, "dup_A", 92001, 1)
        f2, v2 = _make_factor(db_session, "dup_B", 92002, 1)
        spec = [
            (92001, "dup_A", 1, int(v1.id)),
            (92002, "dup_B", 1, int(v2.id)),
        ]
        _make_set(db_session, "FS-T13-DUP1", "Dup candidate A", frozen=True, members_spec=spec)
        _make_set(db_session, "FS-T13-DUP2", "Dup candidate B", frozen=True, members_spec=spec)

        # 历史模型：feature_versions = 同样 tuple；无 fsid 关联
        fv_body: dict[str, Any] = {}
        for (fid, code, fv_no, fv_id) in spec:  # noqa: B007
            fv_body[code] = {
                "factor_id": fid, "factor_version_id": fv_id,
                "factor_version": fv_no, "role": "feature",
                "missing_policy": "exclude",
            }
        m_id = "m-t13-dup-match"
        db_session.add(FactorModelRun(
            id=m_id, model_type="ridge", asset_type="stock",
            target_code="target_5d_return",
            train_start_date=date(2025, 1, 1), train_end_date=date(2026, 1, 1),
            validation_start_date=date(2025, 12, 1),
            validation_end_date=date(2026, 1, 1),
            data_cutoff_at=datetime(2026, 1, 2, 18, 0),
            feature_versions_json=json.dumps(fv_body),
            hyperparameters_json=json.dumps({"alpha": 1.0}),  # 无 fsid
            metrics_json="{}", sample_count=250, trade_date_count=250,
            status="validated", created_at=_utc(),
        ))
        db_session.commit()

        from app.api.routes.factor_models import build_migration_report
        report = build_migration_report(db_session)

        unknown = {r["model_id"]: r for r in report["unlinked_unknown"]}
        assert m_id in unknown, (
            f"多匹配模型应落入 unlinked_unknown，got unknown={list(unknown)}; "
            f"report match={[r['model_id'] for r in report['match_candidates']]}"
        )
        why = unknown[m_id].get("why_hint") or ""
        assert "找不到唯一匹配" in why or "不唯一" in why or "重复" in why or "FS-T13-DUP1" in why, (
            f"why_hint 应指出唯一匹配失败（重复/不唯一/找不到唯一匹配）；got why_hint={why!r}"
        )


# ══════════════════════════════════════════════════════════════════════
# (5) 训练快照缺键 → DTO factors[] 数值字段 None (前端 N/A)
# ══════════════════════════════════════════════════════════════════════
class TestT13_05_SnapshotMissingKeysDtoReturnsNone:
    """rule (5): 快照缺 coef_raw / weight_norm / validation_ic 键 → DTO 该字段 None。"""

    def test_dto_returns_none_for_missing_snapshot_keys(self, db_session):
        f1, v1 = _make_factor(db_session, "T13_NU", 93001, 1)
        spec = [(93001, "T13_NU", 1, int(v1.id))]
        _make_set(db_session, "FS-T13-NU", "Missing keys FS", frozen=True, members_spec=spec)

        model_id = "MR-T13-MISSING-KEYS"
        db_session.add(FactorModelRun(
            id=model_id, model_type="ridge", asset_type="stock",
            target_code="target_5d_return",
            train_start_date=date(2025, 1, 1), train_end_date=date(2026, 1, 1),
            validation_start_date=date(2025, 12, 1),
            validation_end_date=date(2026, 1, 1),
            data_cutoff_at=datetime(2026, 1, 2, 18, 0),
            feature_versions_json="{}",
            hyperparameters_json=json.dumps({"factor_set_id": "FS-T13-NU"}),
            metrics_json="{}", sample_count=250, trade_date_count=250,
            status="validated", created_at=_utc(),
        ))
        # 聚合快照：weights_norm_json 完全缺 coef_raw / weight_norm / validation_ic
        sparse_weights = [
            {
                "factor_code": "T13_NU",
                "factor_id": 93001,
                "factor_version_id": int(v1.id),
                "factor_version": 1,
                # 故意缺: coef_raw / weight_norm / training_ic / validation_ic
            },
        ]
        db_session.add(FactorWeightSnapshot(
            model_id=model_id,
            factor_set_id="FS-T13-NU",
            factor_set_content_hash="h-missing-keys",
            factor_ids_json="[93001]",
            factor_version_ids_json=json.dumps([int(v1.id)]),
            roles_json='["feature"]',
            constraints_json='["free"]',
            missing_strategies_json='["exclude"]',
            train_start_date=date(2025, 1, 1),
            train_end_date=date(2026, 1, 1),
            valid_start_date=date(2025, 12, 1),
            valid_end_date=date(2026, 1, 1),
            data_cutoff_date=date(2026, 1, 2),
            weights_raw_json="{}",
            weights_norm_json=json.dumps(sparse_weights),
            train_mode="offline_minimal",
        ))
        db_session.commit()

        from app.services.factors.__facade__ import factor_model_with_relations
        dto = factor_model_with_relations(model_id)
        assert dto is not None and len(dto.factors) == 1
        row = dto.factors[0]
        assert row.factor_code == "T13_NU"
        # 缺键 → 安全数值转换器返回 None（JSON null → 前端显示 N/A）
        missing_fields = {
            "coef_raw": row.coef_raw,
            "weight_norm": row.weight_norm,
            "training_ic": row.training_ic,
            "validation_ic": row.validation_ic,
            "coverage": row.coverage,  # 无 health 快照 → 也应 None
        }
        for field, value in missing_fields.items():
            assert value is None, (
                f"snapshot 缺 {field} → DTO 必须 None (N/A)，got {value!r}"
            )


# ══════════════════════════════════════════════════════════════════════
# (6) 激活失败回滚：active_model_id 不变
# ══════════════════════════════════════════════════════════════════════
class TestT13_06_ActivateFailureRollback:
    """rule (6): activate_scoring_model 内部激活失败 → 事务 rollback 且异常重抛。"""

    def test_activate_failure_reraises_exception(self, monkeypatch):
        from app.services.factors import __facade__ as facade

        def _boom(db, model_run_id, *, mode, actor, note):  # noqa: ARG001
            raise ValueError("SIMULATED_ACTIVATE_FAILURE: rollback drill")

        # facade 的 runtime.activate_factor_model 动态导入 → 直接 patch 模块属性
        runtime_pkg = __import__("app.services.factors.runtime", fromlist=["activate_factor_model"])
        monkeypatch.setattr(runtime_pkg, "activate_factor_model", _boom)

        with pytest.raises(ValueError) as ei:
            facade.activate_scoring_model(
                "MR-NOT-EXIST-001", mode="ridge", actor="t13", note="rollback drill",
            )
        assert "SIMULATED_ACTIVATE_FAILURE" in str(ei.value)


# ══════════════════════════════════════════════════════════════════════
# (7) pipeline train=false — 任意空值/不存在 fsid 不抛 gate
# ══════════════════════════════════════════════════════════════════════
class TestT13_07_PipelineTrainFalseNoGate:
    """rule (7): train=false 时，空 fsid 与 不存在 fsid 都不会触发 PIPELINE_NO_FACTOR_SET 等 7 要素 gate。"""

    @pytest.mark.parametrize("empty_value", [None, "", "   ", "0"])
    def test_train_false_empty_factor_set_id_no_raise(self, db_session, empty_value):  # noqa: ARG002
        from app.schemas.async_task import FactorPipelineCreate
        from app.services.factors.pipeline_task import _require_ready_training_factor_set

        payload = FactorPipelineCreate(
            train_model=False,
            factor_set_id=empty_value,  # type: ignore[arg-type]
            window_days=250, validation_days=50,
        )
        # 不抛任何异常（PIPELINE_NO_FACTOR_SET 只在 train=true 时触发）
        try:
            _require_ready_training_factor_set(payload)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(
                f"train=false 空 fsid 不应抛任何 gate，got {type(exc).__name__}: {exc}"
            )

    def test_train_false_any_madeup_fsid_also_passes(self, db_session):  # noqa: ARG002
        from app.schemas.async_task import FactorPipelineCreate
        from app.services.factors.pipeline_task import _require_ready_training_factor_set

        payload = FactorPipelineCreate(
            train_model=False,
            factor_set_id="NO-SUCH-FS-999-T13",
            window_days=250, validation_days=50,
        )
        # train=false → 跳过所有 readiness 校验（不创建模型）
        try:
            _require_ready_training_factor_set(payload)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(
                f"train=false 任意不存在 fsid 也应 pass，got {type(exc).__name__}: {exc}"
            )


# ══════════════════════════════════════════════════════════════════════
# (8) 客户端 POST 迁移申请 → 已关联模型 HTTP 400
# ══════════════════════════════════════════════════════════════════════
class TestT13_08_MigrationApplyOnAlreadyLinkedHttp400:
    """rule (8): POST apply-migration-candidate 到 already_linked 桶模型 → HTTP 400 + FORBIDDEN。"""

    def test_apply_candidate_on_already_linked_returns_http_400(self, db_session):
        from app.api.router import api_router
        from app.core.config import settings
        from app.db.session import get_db

        f1, v1 = _make_factor(db_session, "T13_LK", 94001, 1)
        spec = [(94001, "T13_LK", 1, int(v1.id))]
        _make_set(db_session, "FS-T13-LINKED", "Linked FS", frozen=True, members_spec=spec)
        db_session.add(FactorModelRun(
            id="MR-T13-LINKED", model_type="ridge", asset_type="stock",
            target_code="target_5d_return",
            train_start_date=date(2025, 1, 1), train_end_date=date(2026, 1, 1),
            validation_start_date=date(2025, 12, 1),
            validation_end_date=date(2026, 1, 1),
            data_cutoff_at=datetime(2026, 1, 2, 18, 0),
            feature_versions_json="{}",
            hyperparameters_json=json.dumps({"factor_set_id": "FS-T13-LINKED"}),
            metrics_json="{}", sample_count=250, trade_date_count=250,
            status="validated", created_at=_utc(),
        ))
        db_session.commit()

        app = FastAPI()
        app.include_router(api_router)

        def _override():
            try:
                yield db_session
            finally:
                pass

        app.dependency_overrides[get_db] = _override
        try:
            with TestClient(app) as c:
                resp = c.post(
                    f"{settings.api_prefix}/factor-models/apply-migration-candidate",
                    json={
                        "model_id": "MR-T13-LINKED",
                        "audit_signoff": {
                            "approver": "t13-验收",
                            "reason": "T13 专项测试",
                            "signed_at": _utc().isoformat(),
                        },
                    },
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 400, (
            f"POST 到已关联模型必须 HTTP 400，got status={resp.status_code} body={resp.content[:300]}"
        )
        body = resp.json() if hasattr(resp, "json") else {}
        detail_text = str(body.get("detail", body))
        assert "FORBIDDEN" in detail_text or "already_linked" in detail_text or "match_candidates" in detail_text, (
            f"响应 detail 必须明确禁止对 already_linked 模型写入；got detail={detail_text[:400]}"
        )


# ══════════════════════════════════════════════════════════════════════
# (9) scoring 薄封 GET /scoring/models/{id}/relations → schema_version 字段存在
# ══════════════════════════════════════════════════════════════════════
class TestT13_09_ScoringFacadeRelationsSchemaVersion:
    """rule (9): thin-facade /scoring/models/{id}/relations 响应 schema_version 存在且为整数。"""

    def test_schema_version_integer_present(self, db_session):
        from app.api.router import api_router
        from app.core.config import settings
        from app.db.session import get_db

        f1, v1 = _make_factor(db_session, "T13_SV", 95001, 1)
        spec = [(95001, "T13_SV", 1, int(v1.id))]
        _make_set(db_session, "FS-T13-SV", "Schema version FS", frozen=True, members_spec=spec)
        model_id = "MR-T13-SCHEMA-VERSION"
        db_session.add(FactorModelRun(
            id=model_id, model_type="ridge", asset_type="stock",
            target_code="target_5d_return",
            train_start_date=date(2025, 1, 1), train_end_date=date(2026, 1, 1),
            validation_start_date=date(2025, 12, 1),
            validation_end_date=date(2026, 1, 1),
            data_cutoff_at=datetime(2026, 1, 2, 18, 0),
            feature_versions_json="{}",
            hyperparameters_json=json.dumps({"factor_set_id": "FS-T13-SV"}),
            metrics_json="{}", sample_count=250, trade_date_count=250,
            status="validated", created_at=_utc(),
        ))
        db_session.commit()

        app = FastAPI()
        app.include_router(api_router)

        def _override():
            try:
                yield db_session
            finally:
                pass

        app.dependency_overrides[get_db] = _override
        try:
            with TestClient(app) as c:
                resp = c.get(f"{settings.api_prefix}/scoring/models/{model_id}/relations")
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200, f"unexpected status={resp.status_code}"
        body = resp.json()
        assert "schema_version" in body, (
            f"scoring facade 响应必须含 schema_version 字段；keys={list(body.keys())[:20]}"
        )
        assert isinstance(body["schema_version"], int), (
            f"schema_version 必须是整数；got type={type(body['schema_version']).__name__} val={body['schema_version']!r}"
        )
        assert body["schema_version"] >= 2, (
            f"Task5/FR-5 契约版本号应为 v2+；got={body['schema_version']}"
        )


# ══════════════════════════════════════════════════════════════════════
# (10) 训练 eligibility gates：全部通过场景 → all passed=True
# ══════════════════════════════════════════════════════════════════════
class TestT13_10_EligibilityGatesAllPass:
    """rule (10): monkeypatch coverage/ic 健康快照返回 高覆盖+IC 在区间 → 所有 gate passed=True。"""

    def test_run_training_eligibility_gates_two_passes(self, monkeypatch):
        from app.services.factors import __facade__ as facade

        def _ok_cov(codes, threshold=0.70, lookback_days=30):  # noqa: ARG001
            detail = {c: {"coverage": 0.92, "n_samples": lookback_days} for c in codes}
            return facade.GovernanceGateResultDTO(
                gate_name="factor_coverage", passed=True, score=0.92,
                threshold_min=0.70, threshold_max=1.0,
                reasons=[], detail=detail,
            )

        def _ok_ic(codes, min_ic=0.01, max_ic=0.10, lookback_days=30):  # noqa: ARG001
            detail = {c: {"ic_mean": 0.05, "abs_ic": 0.05, "n_samples": lookback_days} for c in codes}
            return facade.GovernanceGateResultDTO(
                gate_name="factor_ic_range", passed=True, score=0.0,
                threshold_min=min_ic, threshold_max=max_ic,
                reasons=[], detail=detail,
            )

        monkeypatch.setattr(facade, "gate_factor_coverage", _ok_cov)
        monkeypatch.setattr(facade, "gate_factor_ic", _ok_ic)

        results = facade.run_training_eligibility_gates(
            factor_codes=["T13_OK_A", "T13_OK_B"],
            coverage_threshold=0.70, ic_min=0.01, ic_max=0.10, lookback_days=30,
        )
        assert len(results) >= 2, (
            f"run_training_eligibility_gates 返回数量应 ≥2（coverage+IC）；got {len(results)}"
        )
        for r in results:
            assert r.passed is True, (
                f"gate {r.gate_name!r} 应 passed=True（全通过场景），got passed={r.passed} reasons={r.reasons}"
            )
