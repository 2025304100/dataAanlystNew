"""T22 · AI 骨架生成 契约测试（DoD）。

覆盖
====
- **自由度约束盒子**：3 档预设、边界校验、Prompt 注入（与校验同源）
- **Prompt**：约束/配额/Few-shot/已存在公式/去重反馈 全部注入
- **解析**：Markdown 围栏、顶层数组、`candidates` 包裹、空/非法响应
- **系统校验层**：强制四字段、category、禁止结构、算子白名单、复杂度、
  窗口、字段白名单、编译校验 → 各类丢弃原因
- **并发与线程池**：`call_llm_with_failover` 是同步函数 → 用
  **显式 `ThreadPoolExecutor`（线程名 `ai-gen*`）** 执行，**不阻塞事件循环**；
  并发上限 = 4（行为验证：观测同时在跑的调用数）
- **降级（DoD 第二条）**：调用失败/抛异常/超时/解析失败 → **永不抛**，
  只体现为 `shortfall`；配合 T18 随机生成器可把缺口补足，进化不受阻
- **预算**：token / 成本 / 总超时 / 调用次数上限 全部生效
"""
from __future__ import annotations

import threading
import time

import pytest

from app.services.factors.mining import ai_generator as AG


# ══════════════════════════════════════════════════════════
# 假 LLM 调用器（**离线**：本任务不直连任何外部服务）
# ══════════════════════════════════════════════════════════


class _FakeResult:
    def __init__(self, *, success: bool = True, raw_response: str = "",
                 total_tokens: int = 100, error_type: str | None = None,
                 error_message: str | None = None) -> None:
        self.success = success
        self.raw_response = raw_response
        self.error_type = error_type
        self.error_message = error_message
        self.total_tokens = total_tokens
        self.prompt_tokens = total_tokens // 2
        self.completion_tokens = total_tokens - self.prompt_tokens
        self.failover_happened = False
        self.latency_ms = 1


def _skeletons(formulas: list[str], *, category: str = "trend") -> str:
    """构造模型风格的 JSON 响应。"""
    import json

    return json.dumps({"candidates": [
        {"formula_ast": f, "category": category,
         "economic_logic": f"经济逻辑：{f}", "expected_direction": "positive"}
        for f in formulas]}, ensure_ascii=False)


def _fake_caller(*, formulas: list[str] | None = None,
                 category: str = "trend",
                 fail: bool = False, raw: str | None = None,
                 sleep: float = 0.0, tokens: int = 100,
                 record: list | None = None,
                 concurrency_probe: dict | None = None):
    """构造假调用器；可选记录线程名与并发水位。"""
    counter = {"now": 0, "max": 0}
    lock = threading.Lock()

    def _call(db, messages, **kwargs):
        if record is not None:
            record.append({"thread": threading.current_thread().name,
                           "kwargs": kwargs, "messages": messages})
        if concurrency_probe is not None:
            with lock:
                counter["now"] += 1
                counter["max"] = max(counter["max"], counter["now"])
            try:
                time.sleep(sleep)
            finally:
                with lock:
                    counter["now"] -= 1
        elif sleep:
            time.sleep(sleep)
        if fail:
            return _FakeResult(success=False, error_type="connection",
                               error_message="all profiles down",
                               total_tokens=tokens)
        body = raw if raw is not None else _skeletons(
            formulas or ["mean(close,20)/mean(close,60)-1"], category=category)
        return _FakeResult(raw_response=body, total_tokens=tokens)

    _call.probe = counter
    return _call


def _cfg(**overrides) -> AG.AIGeneratorConfig:
    base = {
        "target_count": 4,
        "selected_fields": ("close", "volume", "high", "low", "pe_ttm",
                            "roe_ttm", "turnover_rate", "amount"),
        "batch_size": AG.BATCH_MIN,
        "max_calls": 3,
        "call_timeout_s": 5.0,
        "total_timeout_s": 60.0,
        "freedom": AG.FreedomBox.from_preset("aggressive"),
    }
    base.update(overrides)
    return AG.AIGeneratorConfig(**base)


# ══════════════════════════════════════════════════════════
# 1. 自由度约束盒子
# ══════════════════════════════════════════════════════════


class TestFreedomBox:
    def test_three_presets_match_spec(self):
        """向导 §6.3.9：保守 3/1/1、平衡 5/2/2、激进 8/3/4。"""
        c = AG.FreedomBox.from_preset("conservative")
        assert (c.max_operators, c.max_nesting, c.max_field_refs) == (3, 1, 1)
        b = AG.FreedomBox.from_preset("balanced")
        assert (b.max_operators, b.max_nesting, b.max_field_refs) == (5, 2, 2)
        a = AG.FreedomBox.from_preset("aggressive")
        assert (a.max_operators, a.max_nesting, a.max_field_refs) == (8, 3, 4)

    def test_preset_overrides(self):
        f = AG.FreedomBox.from_preset("balanced", max_operators=7)
        assert f.max_operators == 7 and f.max_nesting == 2

    def test_unknown_preset_rejected(self):
        with pytest.raises(ValueError, match="未知预设"):
            AG.FreedomBox.from_preset("nope")

    @pytest.mark.parametrize("kw", [
        {"max_operators": 2}, {"max_operators": 9},
        {"max_nesting": 0}, {"max_nesting": 4},
        {"max_field_refs": 0}, {"max_field_refs": 5},
        {"window_min": 0}, {"window_max": 300},
        {"window_min": 100, "window_max": 50},
        {"innovation": "bogus"}, {"type_strictness": "bogus"},
        {"banned_structures": ("nope",)},
    ])
    def test_validate_rejects_out_of_range(self, kw):
        with pytest.raises(ValueError):
            AG.FreedomBox(**kw).validate()

    def test_replace_and_to_dict_roundtrip(self):
        f = AG.FreedomBox.from_preset("balanced")
        g = f.replace(max_operators=6)
        assert g.max_operators == 6 and f.max_operators == 5, "replace 不得改原对象"
        assert AG.FreedomBox(**g.to_dict()).to_dict() == g.to_dict()

    def test_prompt_lines_cover_constraints_and_drop_warning(self):
        f = AG.FreedomBox.from_preset("balanced", banned_structures=("divide",))
        text = "\n".join(f.prompt_lines())
        for token in ("算子数量 ≤ 5", "嵌套深度 ≤ 2", "字段引用数 ≤ 2",
                      "窗口期", "禁止除法", "违反以上任何约束的公式将被系统直接丢弃"):
            assert token in text, token

    def test_prompt_lines_with_whitelists(self):
        f = AG.FreedomBox(allowed_functions=("mean", "stddev"),
                          allowed_fields=("close",))
        text = "\n".join(f.prompt_lines())
        assert "只允许使用这些算子：mean, stddev" in text
        assert "只允许引用这些字段：close" in text


# ══════════════════════════════════════════════════════════
# 2. 配置
# ══════════════════════════════════════════════════════════


class TestConfig:
    @pytest.mark.parametrize("size", [7, 13, 0])
    def test_batch_size_must_be_in_range(self, size):
        """需求 §3.3：每次批量 8~12 个。"""
        with pytest.raises(ValueError, match="batch_size"):
            _cfg(batch_size=size)

    def test_batch_bounds_are_spec(self):
        assert (AG.BATCH_MIN, AG.BATCH_MAX) == (8, 12)
        assert AG.MAX_CALLS == 10
        assert AG.DEFAULT_CONCURRENCY == 4
        assert AG.CALL_TIMEOUT_S == 30.0
        assert AG.TOTAL_TIMEOUT_S == 300.0
        assert (AG.TOKEN_BUDGET, AG.COST_BUDGET_CNY) == (500_000, 10.0)

    def test_planned_calls_capped_by_max_calls(self):
        assert _cfg(target_count=57, batch_size=10, max_calls=10).planned_calls == 6
        assert _cfg(target_count=500, batch_size=10, max_calls=10).planned_calls == 10
        assert _cfg(target_count=0).planned_calls == 0

    def test_category_quota(self):
        cfg = _cfg(target_count=57)
        quota = cfg.category_quota
        assert set(quota) == set(AG.ALL_CATEGORIES)
        assert all(AG.CATEGORY_QUOTA_MIN <= n <= AG.CATEGORY_QUOTA_MAX
                   for n in quota.values())

    def test_negative_target_rejected(self):
        with pytest.raises(ValueError, match="target_count"):
            _cfg(target_count=-1)

    def test_bad_concurrency_rejected(self):
        with pytest.raises(ValueError, match="concurrency"):
            _cfg(concurrency=0)


# ══════════════════════════════════════════════════════════
# 3. Prompt 构造
# ══════════════════════════════════════════════════════════


class TestBuildPrompt:
    def test_injects_constraints_fewshot_existing_and_quota(self):
        cfg = _cfg(few_shot_formulas=("mean(close,20)/mean(close,60)-1",),
                   existing_formulas=("cs_rank(pe_ttm)",))
        msgs = AG.build_prompt(cfg, batch_index=0)
        assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
        text = msgs[0]["content"] + "\n" + msgs[1]["content"]
        assert "违反以上任何约束的公式将被系统直接丢弃" in text
        assert "mean(close,20)/mean(close,60)-1" in text, "Few-shot 经典模板要注入"
        assert "cs_rank(pe_ttm)" in text, "已存在公式要注入（去重规避）"
        assert "类型配额" in text
        assert "只允许引用用户已选字段" in text

    def test_feedback_injected(self):
        cfg = _cfg()
        text = "\n".join(m["content"] for m in
                         AG.build_prompt(cfg, batch_index=1,
                                         feedback=("xxx 已存在，请换结构",)))
        assert "上一批被丢弃的原因" in text
        assert "xxx 已存在，请换结构" in text

    def test_batch_index_mentioned(self):
        text = "\n".join(m["content"] for m in
                         AG.build_prompt(_cfg(), batch_index=2))
        assert "第 3 批" in text

    def test_required_four_fields_declared(self):
        text = "\n".join(m["content"] for m in AG.build_prompt(_cfg(), batch_index=0))
        for token in ("formula_ast", "category", "economic_logic",
                      "expected_direction"):
            assert token in text


# ══════════════════════════════════════════════════════════
# 4. 解析
# ══════════════════════════════════════════════════════════


class TestParse:
    def test_plain_json(self):
        items, err = AG.parse_ai_payload(_skeletons(["mean(close,20)"]))
        assert err is None and len(items) == 1

    def test_markdown_fence(self):
        raw = "```json\n" + _skeletons(["close+volume"]) + "\n```"
        items, err = AG.parse_ai_payload(raw)
        assert err is None and len(items) == 1

    def test_top_level_list(self):
        raw = '[{"formula_ast": "close", "category": "trend", "economic_logic": "x", "expected_direction": "positive"}]'
        items, err = AG.parse_ai_payload(raw)
        assert err is None and len(items) == 1

    def test_text_wrapped_json(self):
        raw = "这是结果：\n" + _skeletons(["close"]) + "\n以上。"
        items, err = AG.parse_ai_payload(raw)
        assert err is None and len(items) == 1

    @pytest.mark.parametrize("raw", ["", None, "   ", "not json at all"])
    def test_bad_payload_returns_reason(self, raw):
        items, err = AG.parse_ai_payload(raw)
        assert items == [] and err, raw

    def test_non_dict_items_filtered(self):
        items, err = AG.parse_ai_payload('{"candidates": [1, "x", {"a": 1}]}')
        assert err is None and items == [{"a": 1}]


# ══════════════════════════════════════════════════════════
# 5. 系统校验层
# ══════════════════════════════════════════════════════════


def _item(**overrides) -> dict:
    base = {"formula_ast": "mean(close,20)-mean(close,60)",
            "category": "trend",
            "economic_logic": "均线差捕捉趋势",
            "expected_direction": "positive"}
    base.update(overrides)
    return base


class TestValidateSkeleton:
    @pytest.mark.parametrize("missing", ["formula_ast", "category",
                                         "economic_logic", "expected_direction"])
    def test_missing_required_field_dropped(self, missing):
        """需求 §3.3：四字段缺一即丢弃（M1 验收：缺逻辑的不入库）。"""
        item = _item(**{missing: ""})
        cand, reason = AG.validate_skeleton(item, cfg=_cfg())
        assert cand is None and reason == AG.DROP_MISSING_FIELDS

    def test_bad_category_dropped(self):
        cand, reason = AG.validate_skeleton(_item(category="nope"), cfg=_cfg())
        assert cand is None and reason == AG.DROP_BAD_CATEGORY

    def test_category_not_enabled_dropped(self):
        cfg = _cfg(enabled_categories=("trend",))
        cand, reason = AG.validate_skeleton(_item(category="valuation"), cfg=cfg)
        assert cand is None and reason == AG.DROP_BAD_CATEGORY

    def test_banned_divide(self):
        cfg = _cfg(freedom=AG.FreedomBox.from_preset(
            "aggressive", banned_structures=("divide",)))
        cand, reason = AG.validate_skeleton(
            _item(formula_ast="close/volume"), cfg=cfg)
        assert cand is None and reason == AG.DROP_STRUCTURE_BANNED

    def test_banned_log(self):
        cfg = _cfg(freedom=AG.FreedomBox.from_preset(
            "aggressive", banned_structures=("log",)))
        cand, reason = AG.validate_skeleton(
            _item(formula_ast="log(close)"), cfg=cfg)
        assert cand is None and reason == AG.DROP_STRUCTURE_BANNED

    def test_operator_whitelist(self):
        cfg = _cfg(freedom=AG.FreedomBox.from_preset(
            "aggressive", allowed_functions=("mean",)))
        cand, reason = AG.validate_skeleton(
            _item(formula_ast="stddev(close,20)"), cfg=cfg)
        assert cand is None and reason == AG.DROP_CONSTRAINT

    def test_max_operators(self):
        cfg = _cfg(freedom=AG.FreedomBox.from_preset("conservative"))  # ≤3
        cand, reason = AG.validate_skeleton(
            _item(formula_ast="mean(close,5)+mean(close,10)+mean(close,20)"
                              "+mean(volume,5)"), cfg=cfg)
        assert cand is None and reason == AG.DROP_CONSTRAINT

    def test_max_nesting(self):
        cfg = _cfg(freedom=AG.FreedomBox.from_preset("conservative"))  # ≤1
        cand, reason = AG.validate_skeleton(
            _item(formula_ast="cs_rank(mean(stddev(close,20),20))"), cfg=cfg)
        assert cand is None and reason == AG.DROP_CONSTRAINT

    def test_max_field_refs(self):
        cfg = _cfg(freedom=AG.FreedomBox.from_preset("conservative"))  # ≤1
        cand, reason = AG.validate_skeleton(
            _item(formula_ast="close+volume"), cfg=cfg)
        assert cand is None and reason == AG.DROP_CONSTRAINT

    def test_field_not_allowed(self):
        cfg = _cfg(freedom=AG.FreedomBox.from_preset(
            "aggressive", allowed_fields=("close",)))
        cand, reason = AG.validate_skeleton(
            _item(formula_ast="pe_ttm+close"), cfg=cfg)
        assert cand is None and reason == AG.DROP_CONSTRAINT

    def test_window_out_of_range(self):
        cfg = _cfg(freedom=AG.FreedomBox(window_min=5, window_max=60,
                                         max_operators=8, max_nesting=3,
                                         max_field_refs=4))
        cand, reason = AG.validate_skeleton(
            _item(formula_ast="mean(close,200)"), cfg=cfg)
        assert cand is None and reason == AG.DROP_CONSTRAINT

    def test_compile_failure(self):
        cand, reason = AG.validate_skeleton(
            _item(formula_ast="unknown_field_xyz+1"), cfg=_cfg())
        assert cand is None and reason in (AG.DROP_COMPILE_FAILED,
                                           AG.DROP_CONSTRAINT)

    def test_valid_candidate_contract(self):
        cand, reason = AG.validate_skeleton(_item(), cfg=_cfg())
        assert reason is None and cand is not None
        assert cand["source"] == "ai"
        assert cand["operation"] == "ai_generated"
        assert cand["logic_source"] == "ai"
        assert cand["generation"] == 0
        assert cand["compile_ok"] is True
        assert cand["economic_logic"] and cand["expected_direction"]
        assert "formula_hash" not in cand, "hash 由主流程统一补（避免双实现）"

    def test_reason_codes_have_chinese(self):
        for code in (AG.DROP_MISSING_FIELDS, AG.DROP_BAD_CATEGORY,
                     AG.DROP_COMPILE_FAILED, AG.DROP_CONSTRAINT,
                     AG.DROP_DUPLICATE, AG.DROP_PARSE_FAILED,
                     AG.DROP_STRUCTURE_BANNED):
            assert AG.DROP_REASON_ZH[code]


# ══════════════════════════════════════════════════════════
# 6. 端到端（注入假调用器，离线）
# ══════════════════════════════════════════════════════════


class TestGenerateEndToEnd:
    def test_happy_path(self):
        res = AG.generate_ai_candidates(
            _cfg(target_count=2, max_calls=1),
            llm_caller=_fake_caller(formulas=[
                "mean(close,20)-mean(close,60)",
                "cs_rank(pe_ttm)"]))
        assert res.shortfall == 0
        assert len(res.candidates) == 2
        assert all(c["source"] == "ai" for c in res.candidates)
        assert res.stats["calls_made"] == 1

    def test_dedup_against_existing(self):
        """与已存在公式相撞 → 记 DROP_DUPLICATE（不产出重复候选）。"""
        existing = ("mean(close,20)-mean(close,60)",)
        res = AG.generate_ai_candidates(
            _cfg(target_count=1, max_calls=1, existing_formulas=existing),
            llm_caller=_fake_caller(formulas=["mean(close,20)-mean(close,60)"]))
        assert res.candidates == []
        assert res.drops[0]["reason"] == AG.DROP_DUPLICATE
        assert res.shortfall == 1

    def test_duplicate_feedback_carried_to_next_batch(self):
        """去重反馈重试：撞车的公式要出现在后续批次的 prompt 里（向导 §6.3.8）。"""
        seen: list = []

        def caller(db, messages, **kwargs):
            seen.append(messages)
            if len(seen) == 1:
                return _FakeResult(raw_response=_skeletons(
                    ["mean(close,20)-mean(close,60)"]))
            return _FakeResult(raw_response=_skeletons(["cs_rank(volume)"],
                                                       category="volume_price"))

        res = AG.generate_ai_candidates(
            _cfg(target_count=16, max_calls=2, concurrency=1,
                 existing_formulas=("mean(close,20)-mean(close,60)",)),
            llm_caller=caller)
        assert len(seen) >= 2
        second = "\n".join(m["content"] for m in seen[1])
        assert "上一批被丢弃的原因" in second
        assert res.candidates, "第二批应产出候选"

    def test_runs_in_thread_pool_not_event_loop(self):
        """🚨 任务卡坑 1 的**行为**证据：同步调用被丢到 `ai-gen*` 线程执行。"""
        record: list = []
        AG.generate_ai_candidates(
            _cfg(target_count=1, max_calls=1),
            llm_caller=_fake_caller(record=record))
        assert record, "调用器应被调用"
        for entry in record:
            assert entry["thread"] != threading.main_thread().name
            assert entry["thread"].startswith("ai-gen"), entry["thread"]

    def test_concurrency_capped_at_four(self):
        """并发 4 路：观测同时进行的调用数 ≤ 4，且 > 1（证明真并发）。"""
        probe = _fake_caller(
            formulas=["mean(close,20)"], sleep=0.15,
            concurrency_probe={"max": 0})
        cfg = _cfg(target_count=40, batch_size=8, max_calls=10,
                   concurrency=4, total_timeout_s=30.0)
        AG.generate_ai_candidates(cfg, llm_caller=probe)
        assert 1 < probe.probe["max"] <= 4, probe.probe

    def test_llm_failure_degrades_without_raising(self):
        """🚨 DoD 第二条基础：主备全挂 → 不抛异常，缺口如实上报。"""
        res = AG.generate_ai_candidates(
            _cfg(target_count=8, max_calls=2),
            llm_caller=_fake_caller(fail=True))
        assert res.candidates == []
        assert res.shortfall == 8
        assert res.stats["calls_failed"] == res.stats["calls_made"]
        assert any("回退受约束随机" in n for n in res.notes_zh)

    def test_caller_exception_degrades(self):
        def boom(db, messages, **kwargs):
            raise RuntimeError("network exploded")

        res = AG.generate_ai_candidates(_cfg(target_count=1, max_calls=1),
                                        llm_caller=boom)
        assert res.shortfall == 1
        assert res.drops[0]["reason"] == "call_error"

    def test_call_timeout(self):
        res = AG.generate_ai_candidates(
            _cfg(target_count=1, max_calls=1, call_timeout_s=0.05),
            llm_caller=_fake_caller(formulas=["mean(close,20)"], sleep=0.5))
        assert res.drops[0]["reason"] == "call_timeout"
        assert res.shortfall == 1

    def test_parse_failure_recorded(self):
        res = AG.generate_ai_candidates(
            _cfg(target_count=1, max_calls=1),
            llm_caller=_fake_caller(raw="完全不是 JSON"))
        assert res.drops[0]["reason"] == AG.DROP_PARSE_FAILED

    def test_token_budget_stops_further_calls(self):
        """成本上限（¥10/500k token）：超限后不再调用。"""
        res = AG.generate_ai_candidates(
            _cfg(target_count=100, batch_size=8, max_calls=10, concurrency=1,
                 token_budget=100),
            llm_caller=_fake_caller(formulas=["mean(close,20)"], tokens=100))
        assert res.stats["stopped_reason"] == "token_budget"
        assert res.stats["calls_made"] == 1, \
            "第 1 次调用后已超预算，第 2 次派发前必须被拦下"
        assert res.stats["tokens_used"] == 100

    def test_total_timeout_stops_further_calls(self):
        """AI 环节总超时 5 分钟（测试用小值）：超时后剩余名额回退随机。"""
        res = AG.generate_ai_candidates(
            _cfg(target_count=100, batch_size=8, max_calls=5, concurrency=1,
                 total_timeout_s=0.05),
            llm_caller=_fake_caller(formulas=["mean(close,20)"], sleep=0.12))
        assert res.stats["stopped_reason"] == "total_timeout"
        assert res.stats["calls_made"] <= 2
        assert any("提前停止" in n for n in res.notes_zh)

    def test_target_reached_stops(self):
        """目标达成后**停止派发**（planned=5 但只用到 2）。

        ⚠️ 两批必须返回**不同**的骨架：相同骨架会被第 2 层去重丢弃（DROP_DUPLICATE），
        `accepted` 就停在 8 < 10，target_reached 永不触发（首版测试即此错）。
        """
        import json as _json

        state = {"calls": 0}

        def caller(db, messages, **kwargs):
            base = 10 + state["calls"] * 8       # 每批偏移，保证骨架互不相同
            state["calls"] += 1
            body = _json.dumps({"candidates": [
                {"formula_ast": f"mean(close,{base + i})", "category": "trend",
                 "economic_logic": f"逻辑 {base + i}",
                 "expected_direction": "positive"} for i in range(8)]},
                ensure_ascii=False)
            return _FakeResult(raw_response=body)

        res = AG.generate_ai_candidates(
            _cfg(target_count=10, batch_size=8, max_calls=5, concurrency=1),
            llm_caller=caller)
        assert res.stats["stopped_reason"] == "target_reached"
        assert res.stats["calls_made"] == 2, "planned=5 但目标在第 2 批后达成"
        # 🚨 **超额是设计**（需求 §3.3）：10 次调用 × 8~12 → 合计 80~120，
        # 经去重/筛选后才裁到目标（~57）。生成器**不截断**，冗余交给下游。
        assert len(res.candidates) == 16
        assert res.shortfall == 0

    def test_stats_shape(self):
        res = AG.generate_ai_candidates(
            _cfg(target_count=1, max_calls=1),
            llm_caller=_fake_caller(formulas=["mean(close,20)"]))
        for key in ("target", "planned_calls", "calls_made", "calls_failed",
                    "accepted", "tokens_used", "cost_cny", "elapsed_ms",
                    "drops_by_reason"):
            assert key in res.stats, key
        assert res.to_dict()["stats"]["accepted"] == len(res.candidates)

    def test_cost_is_computed_from_tokens(self):
        res = AG.generate_ai_candidates(
            _cfg(target_count=1, max_calls=1),
            llm_caller=_fake_caller(formulas=["mean(close,20)"], tokens=1000))
        assert res.stats["tokens_used"] == 1000
        assert res.stats["cost_cny"] == pytest.approx(
            1000 * AG.COST_PER_TOKEN_CNY, rel=1e-6)


# ══════════════════════════════════════════════════════════
# 7. DoD 第二条：AI 全失败时**进化仍能跑完**
# ══════════════════════════════════════════════════════════


class TestEvolutionSurvivesAiOutage:
    def test_shortfall_filled_by_constrained_random(self):
        """AI 全挂 → 缺口由 T18 受约束随机补足 → 总数达标，进化不受阻。"""
        from app.services.factors.mining import random_generator as RG

        target_ai = 10
        res = AG.generate_ai_candidates(
            _cfg(target_count=target_ai, max_calls=2),
            llm_caller=_fake_caller(fail=True))
        assert res.shortfall == target_ai and res.candidates == []

        rnd = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=res.shortfall),
            selected_fields=("close", "volume", "pe_ttm"))
        total = len(res.candidates) + len(rnd.candidates)
        assert total >= target_ai or rnd.stats["selected"] > 0
        assert all(c["source"] == "random" for c in rnd.candidates)

    def test_partial_ai_then_random_topup(self):
        """部分成功：AI 产出若干 + 随机补足缺口；两来源标记正确可被 T19 区分。"""
        from app.services.factors.mining import random_generator as RG

        res = AG.generate_ai_candidates(
            _cfg(target_count=2, max_calls=1),
            llm_caller=_fake_caller(formulas=["mean(close,20)"]))
        assert len(res.candidates) == 1 and res.shortfall == 1
        rnd = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=res.shortfall),
            selected_fields=("close", "volume"))
        assert all(c["source"] in ("ai", "random")
                   for c in res.candidates + rnd.candidates)

    def test_generated_candidates_survive_dedup_pipeline(self):
        """产出的候选能进 T19 去重而不崩（跨任务契约）。"""
        from app.services.factors.mining import dedup as DD

        res = AG.generate_ai_candidates(
            _cfg(target_count=2, max_calls=1),
            llm_caller=_fake_caller(formulas=["mean(close,20)",
                                              "cs_rank(pe_ttm)"]))
        deduped = DD.dedup_across_sources(classic=[], ai=res.candidates,
                                          random=[])
        assert deduped.kept_count == len(res.candidates)
        assert deduped.stats["by_source"].get("ai") == len(res.candidates)

    def test_never_raises_on_broken_caller_object(self):
        class Weird:
            success = True              # 非 bool
            raw_response = None
            total_tokens = "abc"        # 非法类型

        res = AG.generate_ai_candidates(_cfg(target_count=1, max_calls=1),
                                        llm_caller=lambda *a, **k: Weird())
        assert isinstance(res, AG.AIResult)
        assert res.shortfall >= 0
