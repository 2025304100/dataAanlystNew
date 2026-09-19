"""T26/F1 抽取与相关经验测试（db_session）。

覆盖硬要求：is_negative_sample=1 抽取自动规避 / archived 规避 /
字段硬过滤 / 场景匹配打分 / 加权不放回随机 / 参数实例化 / use_count 回写。
"""
from __future__ import annotations

import json
import random

import pytest

from app.models.factor_experience import FactorExperience
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


def _store(db, *, field="close", category="trend", window=20, **overrides):
    payload = {
        "formula_ast": _ast(field, window),
        "source": "enumerated",
        "category": category,
        **overrides,
    }
    exp_id, status = svc.store_experience(db, payload=payload)
    assert status == "created"
    return exp_id


class TestSampleExperiences:
    def test_negative_sample_excluded(self, db_session):
        """★ 硬要求：is_negative_sample=1 抽取时自动规避。"""
        good = _store(db_session)
        bad = _store(db_session, field="volume", is_negative_sample=1)
        items = svc.sample_experiences(
            db_session, count=10, rng=random.Random(7))
        ids = [i["experience_id"] for i in items]
        assert good in ids and bad not in ids

    def test_archived_excluded(self, db_session):
        alive = _store(db_session)
        _store(db_session, field="volume", status="archived")
        items = svc.sample_experiences(
            db_session, count=10, rng=random.Random(7))
        assert [i["experience_id"] for i in items] == [alive]

    def test_field_scope_hard_filter(self, db_session):
        close_only = _store(db_session, field="close")
        _store(db_session, field="volume")
        items = svc.sample_experiences(
            db_session, field_scope=["close"], count=10,
            rng=random.Random(7))
        assert [i["experience_id"] for i in items] == [close_only]

    def test_env_match_boosts_score(self, db_session):
        """场景匹配打分契约：env/pool 命中 +2/+2（与 service docstring 一致）。"""
        matched = _store(
            db_session, task_context={"market_env": "bull"})
        _store(db_session, field="volume")  # 无场景标签
        items = svc.sample_experiences(
            db_session, market_env="bull", count=10, rng=random.Random(7))
        by_id = {i["experience_id"]: i["score"] for i in items}
        assert by_id[matched] >= svc.SCORE_ENV_MATCH
        assert by_id[matched] > next(
            v for k, v in by_id.items() if k != matched)

    def test_weighted_random_no_replacement(self, db_session):
        # 结构指纹按字段区分：5 个不同字段 = 5 条独立经验
        for field in ("close", "volume", "open", "high", "low"):
            _store(db_session, field=field)
        items = svc.sample_experiences(
            db_session, count=3, rng=random.Random(7))
        assert len(items) == 3
        assert len({i["experience_id"] for i in items}) == 3

    def test_insufficient_candidates_returns_all(self, db_session):
        _store(db_session)
        _store(db_session, field="volume")
        items = svc.sample_experiences(
            db_session, count=10, rng=random.Random(7))
        assert len(items) == 2

    def test_empty_db_returns_empty(self, db_session):
        assert svc.sample_experiences(
            db_session, count=5, rng=random.Random(7)) == []

    def test_placeholders_instantiated(self, db_session):
        _store(db_session, window=20)
        (item,) = svc.sample_experiences(
            db_session, count=1, rng=random.Random(7))
        assert item["template"] == "mean(close, {n1})"
        assert "{n" not in item["formula"]
        ph = item["placeholders"][0]
        lo, hi = ph["range"]
        assert lo <= ph["value"] <= hi
        assert str(ph["value"]) in item["formula"]

    def test_use_count_and_last_used_at_written(self, db_session):
        exp_id = _store(db_session)
        svc.sample_experiences(db_session, count=10, rng=random.Random(7))
        exp = db_session.get(FactorExperience, exp_id)
        assert exp.use_count == 1
        assert exp.last_used_at is not None

    def test_related_experiences_param_excludes(self, db_session):
        keep = _store(db_session)
        given = _store(db_session, field="volume")
        items = svc.sample_experiences(
            db_session, count=10,
            related_experiences=[given], rng=random.Random(7))
        ids = [i["experience_id"] for i in items]
        assert keep in ids and given not in ids


class TestRelatedExperiences:
    def test_same_category_ranked(self, db_session):
        base = _store(db_session, category="trend")
        high = _store(db_session, field="volume", category="trend")
        _store(db_session, field="open", category="volatility")
        # 手工拉开 success_rate / use_count
        row = db_session.get(FactorExperience, high)
        row.success_rate = 0.8
        row.use_count = 5
        db_session.commit()

        items = svc.related_experiences(db_session, experience_id=base)
        assert [i["experience_id"] for i in items] == [high]
        assert items[0]["success_rate"] == pytest.approx(0.8)

    def test_unknown_id_returns_empty(self, db_session):
        assert svc.related_experiences(
            db_session, experience_id="no-such-id") == []

    def test_limit_respected(self, db_session):
        base = _store(db_session)
        for field in ("volume", "open", "high"):
            _store(db_session, field=field)
        items = svc.related_experiences(
            db_session, experience_id=base, limit=2)
        assert len(items) == 2

    def test_payload_json_roundtrip(self, db_session):
        exp_id = _store(db_session, task_context={"stock_pool": "hs300"})
        exp = db_session.get(FactorExperience, exp_id)
        assert json.loads(exp.param_placeholders_json)[0]["name"] == "n1"
