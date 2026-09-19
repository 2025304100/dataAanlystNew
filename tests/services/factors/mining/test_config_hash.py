"""T16 · config_hash + 草稿服务 契约测试（DoD）。

覆盖
====
- **哈希**：稳定性（跨调用/跨进程一致）、键序无关、None 无关、值相关、
  嵌套结构、Step4 不参与、分步哈希、差异摘要
- **parity**：与 T14 `validation_service.compute_config_hash` 逐例一致
  （两处实现必须锁死，否则幂等会静默失效）
- **草稿**：新建/部分更新/步骤钳位/非法状态/状态流转/待修复标记
- **校验运行**：同 draft+hash 活动唯一（复用）；终态不复用；有效期判定
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.services.factors.mining import config_hash as CH
from app.services.factors.mining import draft_service as DS


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ══════════════════════════════════════════════════════════
# 1. 哈希：稳定性与语义
# ══════════════════════════════════════════════════════════


class TestConfigHash:
    def test_stable_across_calls(self):
        cfg = {"markets": ["sh"], "pe": {"min": 1, "max": 20}}
        assert CH.compute_config_hash(cfg) == CH.compute_config_hash(cfg)
        assert len(CH.compute_config_hash(cfg)) == CH.HASH_LENGTH

    def test_key_order_insensitive(self):
        a = CH.compute_config_hash({"x": 1, "y": 2})
        b = CH.compute_config_hash({"y": 2, "x": 1})
        assert a == b

    def test_nested_order_insensitive(self):
        a = CH.compute_config_hash({"s": {"a": 1, "b": {"c": 2, "d": 3}}})
        b = CH.compute_config_hash({"s": {"b": {"d": 3, "c": 2}, "a": 1}})
        assert a == b

    def test_none_equivalent_to_absent(self):
        """`{"a":1}` 与 `{"a":1,"b":None}` 必须同哈希 ——
        否则前端「清空一个字段」会让幂等失效、重复建校验任务。"""
        assert CH.compute_config_hash({"a": 1}) == \
            CH.compute_config_hash({"a": 1, "b": None})

    def test_none_in_list_is_preserved(self):
        """列表里的 None **保留**（位置有语义）——
        `["close", None]` 与 `["close"]` 是不同配置，不能同哈希。
        这条由 T16 parity 测试发现：我第一版把列表内 None 也删了，
        与 T14 实现分叉。"""
        assert CH.compute_config_hash({"a": [1, None, 2]}) != \
            CH.compute_config_hash({"a": [1, 2]})
        assert CH.compute_config_hash({"a": [1, None, 2]}) == \
            CH.compute_config_hash({"a": [1, None, 2]})
        # 列表长度不同 → 不同哈希
        assert CH.compute_config_hash({"a": [1]}) != \
            CH.compute_config_hash({"a": [1, None]})

    def test_value_change_changes_hash(self):
        assert CH.compute_config_hash({"a": 1}) != CH.compute_config_hash({"a": 2})
        assert CH.compute_config_hash({"a": "1"}) != CH.compute_config_hash({"a": 1})

    def test_zero_and_false_are_kept(self):
        """0 / False / 空串**不是** None，必须参与哈希（对照 T11 的丢 None 教训）。"""
        base = CH.compute_config_hash({"a": 1})
        assert CH.compute_config_hash({"a": 1, "b": 0}) != base
        assert CH.compute_config_hash({"a": 1, "b": False}) != base
        assert CH.compute_config_hash({"a": 1, "b": ""}) != base

    def test_empty_config(self):
        assert CH.compute_config_hash(None) == CH.compute_config_hash({}) == \
            CH.compute_config_hash({})

    def test_strip_none_is_recursive_and_pure(self):
        """字典值去 None；列表内 None 保留（递归进列表内的字典）。"""
        src = {"a": None, "b": [1, None, {"c": None, "d": 2}], "e": {"f": None}}
        out = CH.strip_none(src)
        assert out == {"b": [1, None, {"d": 2}], "e": {}}
        assert src["a"] is None, "不得原地修改输入"

    def test_canonical_payload_is_sorted_and_compact(self):
        payload = CH.canonical_config_payload({"b": 1, "a": 2})
        assert payload == '{"a":2,"b":1}'

    def test_step4_not_hashed(self):
        """Step4（进化参数）不参与 —— 改参数不应导致字段校验重跑。"""
        h1 = CH.compute_draft_config_hash(step1={"p": 1}, step2={"t": 2},
                                          step3={"f": 3})
        h2 = CH.compute_draft_config_hash(
            step1={"p": 1}, step2={"t": 2}, step3={"f": 3},
            extra=None)
        assert h1 == h2
        # 只改 step1/2/3 才变
        assert CH.compute_draft_config_hash(step1={"p": 9}, step2={"t": 2},
                                            step3={"f": 3}) != h1

    def test_steps_to_config_filters_step4(self):
        steps = {"step1": {"a": 1}, "step4": {"g": 9}, "step2": None}
        assert CH.steps_to_config(steps) == {"step1": {"a": 1}}

    def test_step_hash_local(self):
        assert CH.compute_step_hash({"a": 1}) != CH.compute_step_hash({"a": 2})
        assert CH.compute_step_hash({"a": 1}) == CH.compute_step_hash({"a": 1})

    def test_hashes_differ_none_safe(self):
        assert CH.hashes_differ(None, None) is False
        assert CH.hashes_differ("a", "a") is False
        assert CH.hashes_differ("a", "b") is True
        assert CH.hashes_differ(None, "a") is True

    def test_summarize_differences(self):
        diff = CH.summarize_differences(
            {"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4})
        assert diff == ["b", "c"]
        # None 与缺失等价 → 不算差异
        assert CH.summarize_differences({"a": 1}, {"a": 1, "b": None}) == []

    def test_parity_with_validation_service(self):
        """🚨 与 T14 的临时实现逐例一致 —— 两处哈希必须锁死。

        若将来任一侧调整算法（例如决定保留 None），这条会红：
        那时必须把 T14 改为 import 本模块，而不是「两边都改一下」。
        """
        from app.services.factors.mining import validation_service as VS

        cases = [
            {},
            None,
            {"a": 1},
            {"a": 1, "b": None},
            {"markets": ["sh", "sz"], "boards": ["sh_main"]},
            {"nested": {"x": [1, None, 2], "y": {"z": None, "w": 3}}},
            {"zero": 0, "false": False, "empty": ""},
        ]
        for cfg in cases:
            assert CH.compute_config_hash(cfg) == VS.compute_config_hash(cfg), \
                f"哈希实现分叉: {cfg!r}"


# ══════════════════════════════════════════════════════════
# 2. 草稿 CRUD
# ══════════════════════════════════════════════════════════


class TestDraftService:
    def test_create_and_get(self, db_session):
        view = DS.save_draft(db_session, payload={
            "name": "我的实验", "current_step": 2,
            "step1": {"source_type": "filter"},
        })
        assert view.id
        assert view.status == DS.DRAFT_STATUS_DRAFT
        assert view.current_step == 2
        assert view.steps["step1"] == {"source_type": "filter"}

        got = DS.get_draft(db_session, draft_id=view.id)
        assert got is not None
        assert got.name == "我的实验"
        assert got.config_hash == view.config_hash

    def test_get_unknown_returns_none(self, db_session):
        assert DS.get_draft(db_session, draft_id="nope") is None

    def test_partial_update_keeps_other_steps(self, db_session):
        """暂存编辑（需求 §3.5）：只传 step2 不得清掉 step1。"""
        v1 = DS.save_draft(db_session, payload={
            "step1": {"a": 1}, "step2": {"b": 2}})
        v2 = DS.save_draft(db_session, payload={
            "draft_id": v1.id, "step2": {"b": 99}})
        assert v2.steps["step1"] == {"a": 1}
        assert v2.steps["step2"] == {"b": 99}
        assert v2.config_hash != v1.config_hash

    def test_flat_and_nested_payload_both_work(self, db_session):
        flat = DS.save_draft(db_session, payload={"step1": {"a": 1}})
        nested = DS.save_draft(db_session, payload={"steps": {"step1": {"a": 1}}})
        assert flat.steps["step1"] == nested.steps["step1"]
        assert flat.config_hash == nested.config_hash

    def test_current_step_clamped(self, db_session):
        v = DS.save_draft(db_session, payload={"current_step": 99})
        assert v.current_step == DS.STEP_MAX
        v2 = DS.save_draft(db_session, payload={"current_step": -5})
        assert v2.current_step == DS.STEP_MIN

    def test_invalid_status_rejected(self, db_session):
        with pytest.raises(ValueError):
            DS.save_draft(db_session, payload={"status": "bogus"})

    def test_status_transitions(self, db_session):
        v = DS.save_draft(db_session, payload={"step1": {"a": 1}})
        marked = DS.mark_waiting_data_recheck(db_session, draft_id=v.id)
        assert marked.status == DS.DRAFT_STATUS_WAITING_RECHECK
        back = DS.set_draft_status(db_session, draft_id=v.id,
                                   status=DS.DRAFT_STATUS_DRAFT)
        assert back.status == DS.DRAFT_STATUS_DRAFT
        inv = DS.set_draft_status(db_session, draft_id=v.id,
                                  status=DS.DRAFT_STATUS_INVALIDATED)
        assert inv.status == DS.DRAFT_STATUS_INVALIDATED

    def test_set_status_unknown_draft(self, db_session):
        with pytest.raises(ValueError):
            DS.set_draft_status(db_session, draft_id="nope",
                                status=DS.DRAFT_STATUS_DRAFT)

    def test_list_drafts_filters(self, db_session):
        DS.save_draft(db_session, payload={"owner": "alice", "step1": {"a": 1}})
        DS.save_draft(db_session, payload={"owner": "bob", "step1": {"a": 2}})
        alice = DS.list_drafts(db_session, owner="alice")
        assert len(alice) == 1 and alice[0].owner == "alice"

    def test_draft_config_hash_uses_steps_1_to_3_only(self, db_session):
        v = DS.save_draft(db_session, payload={
            "step1": {"a": 1}, "step2": {"b": 2}, "step3": {"f": ["close"]},
            "step4": {"generations": 5}})
        expected = CH.compute_draft_config_hash(
            step1={"a": 1}, step2={"b": 2}, step3={"f": ["close"]})
        assert DS.draft_config_hash(db_session, draft_id=v.id) == expected

    def test_draft_config_hash_unknown_draft(self, db_session):
        with pytest.raises(ValueError):
            DS.draft_config_hash(db_session, draft_id="nope")


# ══════════════════════════════════════════════════════════
# 3. 校验运行：幂等复用（任务卡唯一硬规则）
# ══════════════════════════════════════════════════════════


class TestValidationRunIdempotency:
    def _draft(self, db_session, **kw):
        return DS.save_draft(db_session, payload={"step1": {"a": 1}, **kw})

    def test_same_draft_and_hash_reuses_active_run(self, db_session):
        """🚨 同一草稿同配置哈希不得重复创建校验任务。"""
        draft = self._draft(db_session)
        h = draft.config_hash
        r1 = DS.create_or_reuse_validation_run(
            db_session, draft_id=draft.id, config_hash=h,
            fields=["close"], total_shards=2)
        assert r1.reused is False

        r2 = DS.create_or_reuse_validation_run(
            db_session, draft_id=draft.id, config_hash=h,
            fields=["close"], total_shards=2)
        assert r2.reused is True
        assert r2.id == r1.id

        # 库里只有一条活动运行
        rows = DS.find_active_validation_run(db_session, draft_id=draft.id,
                                             config_hash=h)
        assert rows is not None and rows.id == r1.id

    def test_different_hash_creates_new_run(self, db_session):
        draft = self._draft(db_session)
        h1 = draft.config_hash
        r1 = DS.create_or_reuse_validation_run(db_session, draft_id=draft.id,
                                               config_hash=h1)
        h2 = CH.compute_config_hash({"different": True})
        r2 = DS.create_or_reuse_validation_run(db_session, draft_id=draft.id,
                                               config_hash=h2)
        assert r2.reused is False and r2.id != r1.id
        assert r2.config_hash == h2

    def test_terminal_run_is_not_reused(self, db_session):
        """终态不复用：修复数据后重新校验是合法诉求（向导 §5.1）。"""
        draft = self._draft(db_session)
        h = draft.config_hash
        r1 = DS.create_or_reuse_validation_run(db_session, draft_id=draft.id,
                                               config_hash=h)
        DS.update_validation_run(db_session, run_id=r1.id, status=DS.RUN_BLOCKED,
                                 report={"verdict": "block"})
        r2 = DS.create_or_reuse_validation_run(db_session, draft_id=draft.id,
                                               config_hash=h)
        assert r2.reused is False and r2.id != r1.id

    def test_default_hash_derived_from_draft(self, db_session):
        draft = self._draft(db_session)
        run = DS.create_or_reuse_validation_run(db_session, draft_id=draft.id)
        assert run.config_hash == draft.config_hash

    def test_invalid_status_rejected(self, db_session):
        draft = self._draft(db_session)
        with pytest.raises(ValueError):
            DS.create_or_reuse_validation_run(db_session, draft_id=draft.id,
                                              status="bogus")

    def test_ttl_set_on_create(self, db_session):
        draft = self._draft(db_session)
        before = _utcnow()
        run = DS.create_or_reuse_validation_run(db_session, draft_id=draft.id)
        assert run.expires_at is not None
        delta = run.expires_at - before
        assert timedelta(hours=23) < delta <= timedelta(hours=24, seconds=5)


# ══════════════════════════════════════════════════════════
# 4. 校验运行：推进 / 查询 / 有效期
# ══════════════════════════════════════════════════════════


class TestValidationRunProgress:
    def _run(self, db_session):
        draft = DS.save_draft(db_session, payload={"step1": {"a": 1}})
        return DS.create_or_reuse_validation_run(
            db_session, draft_id=draft.id, fields=["close", "pe_ttm"],
            total_shards=2)

    def test_update_progress_and_shard_state(self, db_session):
        run = self._run(db_session)
        updated = DS.update_validation_run(
            db_session, run_id=run.id, status=DS.RUN_RUNNING, task_id="task-1",
            done_shards=1, shard_state={"close::all": {"status": "done"}})
        assert updated.status == DS.RUN_RUNNING
        assert updated.task_id == "task-1"
        assert updated.done_shards == 1
        assert updated.shard_state == {"close::all": {"status": "done"}}

    def test_report_and_verdict_persisted(self, db_session):
        run = self._run(db_session)
        DS.update_validation_run(
            db_session, run_id=run.id, status=DS.RUN_PASSED,
            done_shards=2, report={"verdict": "pass", "total_shards": 2},
            blockers=[], warnings=[{"code": "low_coverage"}])
        got = DS.get_validation_run(db_session, run_id=run.id)
        assert got.report["verdict"] == "pass"
        assert got.warnings[0]["code"] == "low_coverage"

    def test_invalid_status_rejected(self, db_session):
        run = self._run(db_session)
        with pytest.raises(ValueError):
            DS.update_validation_run(db_session, run_id=run.id, status="nope")

    def test_update_unknown_run(self, db_session):
        with pytest.raises(ValueError):
            DS.update_validation_run(db_session, run_id="nope", status=DS.RUN_PASSED)

    def test_latest_run(self, db_session):
        run = self._run(db_session)
        latest = DS.latest_validation_run(db_session, draft_id=run.draft_id)
        assert latest is not None and latest.id == run.id

    def test_validity_window(self, db_session):
        run = self._run(db_session)
        DS.update_validation_run(db_session, run_id=run.id, status=DS.RUN_PASSED)
        now = _utcnow()
        assert DS.is_validation_run_valid(db_session, run_id=run.id, now=now) is True
        assert DS.is_validation_run_valid(
            db_session, run_id=run.id,
            now=now + timedelta(hours=24, seconds=1)) is False

    def test_blocked_run_is_never_valid(self, db_session):
        run = self._run(db_session)
        DS.update_validation_run(db_session, run_id=run.id, status=DS.RUN_BLOCKED)
        assert DS.is_validation_run_valid(db_session, run_id=run.id) is False

    def test_unknown_run_not_valid(self, db_session):
        assert DS.is_validation_run_valid(db_session, run_id="nope") is False


# ══════════════════════════════════════════════════════════
# 5. 与 T14 的协作面（同一草稿 + 同哈希只有一条活动运行）
# ══════════════════════════════════════════════════════════


class TestCrossTaskContract:
    def test_t14_config_hash_matches_draft_hash_for_same_steps(self, db_session):
        """T14 校验任务用 `config` 算哈希；草稿用 step1~3 算哈希 ——
        只要 T14 传的是同一份 step1~3 内容，两者必须相等（幂等才成立）。"""
        from app.services.factors.mining import validation_service as VS

        steps = {"step1": {"markets": ["sh"]}, "step2": {"frequency": "daily"},
                 "step3": {"fields": ["close"]}}
        draft = DS.save_draft(db_session, payload=dict(steps))
        assert VS.compute_config_hash(CH.steps_to_config(steps)) == draft.config_hash
