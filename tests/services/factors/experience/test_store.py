"""T26/F1 store_experience 行为测试（db_session = tmp SQLite + auto-align）。

覆盖：4 表同事务落库 / 三层指纹去重幂等 / 负样本标记 /
db_numeric 纪律（NaN → None，不可归 0）/ 入参白名单校验 / 自动分类。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from app.models.factor_experience import (
    FactorExperience,
    FactorExperienceFieldDep,
    FactorExperienceMetric,
    FactorExperienceTag,
)
from app.services.factors.experience import service as svc


def _ast(field: str = "close", window: int = 20) -> dict:
    return {
        "type": "Expression",
        "body": {
            "type": "Call", "func": "mean",
            "args": [
                {"type": "Name", "id": field},
                {"type": "Constant", "value": window},
            ],
            "keywords": [],
        },
    }


def _payload(**overrides) -> dict:
    payload = {
        "formula_ast": _ast(),
        "source": "ai_generated",
        "category": "trend",
        "metrics": [
            {"metric_type": "icir", "value": 0.32, "period": "train"},
            {"metric_type": "icir", "value": 0.24, "period": "val", "is_oos": 1},
            {"metric_type": "coverage", "value": 0.91},
        ],
        "task_context": {"market_env": "bull", "stock_pool": "hs300"},
        "field_layers": {"close": "A"},
    }
    payload.update(overrides)
    return payload


class TestStoreExperience:
    def test_store_writes_four_tables(self, db_session):
        exp_id, status = svc.store_experience(db_session, payload=_payload())
        assert status == "created"
        assert len(exp_id) == 64

        exp = db_session.get(FactorExperience, exp_id)
        assert exp is not None
        assert exp.formula_template == "mean(close, {n1})"
        assert json.loads(exp.formula_ast)["body"]["func"] == "mean"
        assert exp.category == "trend"
        assert exp.source == "ai_generated"
        assert json.loads(exp.complexity_json) == {
            "operators": 1, "nesting_depth": 1, "field_refs": 1}
        phs = json.loads(exp.param_placeholders_json)
        assert phs == [{"name": "n1", "value": 20, "range": [10.0, 40.0]}]
        assert exp.status == "normal"
        assert exp.is_negative_sample == 0
        assert exp.schema_version == "v1"
        assert exp.use_count == 0 and exp.success_count == 0
        assert exp.avg_icir == pytest.approx(0.28)  # (0.32+0.24)/2

        tags = db_session.execute(
            select(FactorExperienceTag).where(
                FactorExperienceTag.experience_id == exp_id)
        ).scalars().all()
        tag_map = {(t.tag_key, t.tag_value): t.source for t in tags}
        assert ("op", "mean") in tag_map and ("field", "close") in tag_map
        assert ("complexity", "low") in tag_map
        assert ("market_env", "bull") in tag_map
        assert ("stock_pool", "hs300") in tag_map

        metrics = db_session.execute(
            select(FactorExperienceMetric).where(
                FactorExperienceMetric.experience_id == exp_id)
        ).scalars().all()
        assert len(metrics) == 3
        assert {m.metric_type for m in metrics} == {"icir", "coverage"}
        assert metrics[0].value == pytest.approx(0.32)

        deps = db_session.execute(
            select(FactorExperienceFieldDep).where(
                FactorExperienceFieldDep.experience_id == exp_id)
        ).scalars().all()
        assert [(d.field_code, d.field_layer) for d in deps] == [("close", "A")]

    def test_duplicate_fingerprint_returns_existing(self, db_session):
        id1, status1 = svc.store_experience(db_session, payload=_payload())
        # 同构不同参数（20 → 60）：三层指纹应命中同一条经验
        id2, status2 = svc.store_experience(
            db_session, payload=_payload(formula_ast=_ast(window=60)))
        assert status1 == "created" and status2 == "duplicate"
        assert id1 == id2
        total = db_session.execute(
            select(func.count()).select_from(FactorExperience)).scalar_one()
        assert total == 1

    def test_negative_sample_marked(self, db_session):
        exp_id, _ = svc.store_experience(
            db_session, payload=_payload(is_negative_sample=1))
        exp = db_session.get(FactorExperience, exp_id)
        assert exp.is_negative_sample == 1

    def test_metrics_nan_becomes_none_not_zero(self, db_session):
        """db_numeric 纪律：NaN → None（不可归 0）；无有效 icir → avg None。"""
        exp_id, _ = svc.store_experience(db_session, payload=_payload(
            metrics=[{"metric_type": "icir", "value": float("nan")}]))
        metrics = db_session.execute(
            select(FactorExperienceMetric).where(
                FactorExperienceMetric.experience_id == exp_id)
        ).scalars().all()
        assert metrics[0].value is None
        exp = db_session.get(FactorExperience, exp_id)
        assert exp.avg_icir is None

    def test_invalid_source_raises(self, db_session):
        with pytest.raises(ValueError, match="source"):
            svc.store_experience(db_session, payload=_payload(source="hacker"))

    def test_invalid_category_raises(self, db_session):
        with pytest.raises(ValueError, match="category"):
            svc.store_experience(db_session, payload=_payload(category="alpha"))

    def test_invalid_metric_type_raises(self, db_session):
        with pytest.raises(ValueError, match="metric_type"):
            svc.store_experience(db_session, payload=_payload(
                metrics=[{"metric_type": "sharpe", "value": 1.0}]))

    def test_category_inferred_when_missing(self, db_session):
        exp_id, _ = svc.store_experience(
            db_session,
            payload=_payload(category=None, formula_ast=_ast("pe_ttm", 20)),
        )
        exp = db_session.get(FactorExperience, exp_id)
        assert exp.category == "valuation"
