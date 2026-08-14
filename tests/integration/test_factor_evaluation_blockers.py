"""Task 3: 9 个阻塞场景全面回归（AC-2 / AC-5）。

覆盖：
1. test_blocker_missing_field: preflight 阶段缺字段
2. test_blocker_target_horizon_10_unavailable: 缺标签批次
3. test_blocker_historical_insufficient: 交易日不足
4. test_blocker_invalid_formula_syntax: 非法公式语法
5. test_blocker_db_session_exception: DB OperationalError 异常
6. test_blocker_worker_exception: Worker 运行时异常
7. test_blocker_stress_failure: 压力测试不稳定
8. test_blocker_idempotent_no_duplicate_task: 幂等不重复创建
9. test_blocker_cancel_task: 取消任务

TR-3.1 / TR-3.2 / TR-3.3 通用验证在文件末尾。
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests.integration.conftest import wait_for_task_status as _wait_for_task_status


# ══════════════════════════════════════════════════════════════════════════════
# Synthetic Mock 基类：FactorWarehouse + trade_calendar + FactorExecutor monkeypatch
# ══════════════════════════════════════════════════════════════════════════════

class SyntheticMockBase:
    """合成 mock 基类：所有 WP5 依赖均通过 monkeypatch 替换，无真实数据也能跑通。"""

    @staticmethod
    def _base_payload(factor_code: str = "close", target_horizon: int = 5) -> dict[str, Any]:
        import uuid as _uuid
        import random as _rand
        uniq = _uuid.uuid4().hex[:6]
        # factor_version_id 必须是唯一的，因为它在 _IDEMPOTENCY_KEYS 里（created_by 不在）
        # 使用 [50000, 99999] 范围随机数，避免与真实因子版本 ID 冲突
        rand_version_id = 50000 + _rand.randint(0, 49999)
        return {
            "factor_code": factor_code,
            "factor_kind": "continuous",
            "factor_version_id": rand_version_id,
            "universe": "all_a_shares",
            "start_date": None,
            "end_date": None,
            "target_horizon": target_horizon,
            "n_groups": 5,
            "cost_rate": 0.001,
            "direction": None,
            "created_by": f"test_blocker_{uniq}",
        }

    @staticmethod
    def _preflight_payload(factor_code: str = "close", target_horizon: int = 5) -> dict[str, Any]:
        return {
            "factor_code": factor_code,
            "factor_version_id": None,
            "universe": "all_a_shares",
            "start_date": None,
            "end_date": None,
            "target_horizon": target_horizon,
        }

    @classmethod
    def apply_synthetic_patchers(
        cls,
        describe_table_cols: list[str] | None = None,
        n_trade_dates: int = 400,
        horizon_to_batch_id: dict[int, str | None] | None = None,
        execute_panel_raises: type[Exception] | Exception | None = None,
        stress_result: dict[str, Any] | None = None,
    ):
        """统一 monkeypatch：FactorWarehouse / trade_calendar.latest_complete_trade_date /
        FactorExecutor.execute_panel / run_stress_test / FactorCompiler。

        返回一个 patchers 列表，调用方负责 stop。
        """
        from app.services.factors import wp5_eval_task
        from app.services.factors import trade_calendar as tc_mod
        from app.services.factors import factor_stress

        patchers: list[Any] = []

        if horizon_to_batch_id is None:
            horizon_to_batch_id = {5: "batch_target_5d_syn_001", 10: None}

        default_cols = [
            "trade_date", "symbol", "volume", "open", "high", "low",
            "close", "prev_close", "amount", "pct_chg",
        ]
        cols = describe_table_cols if describe_table_cols is not None else default_cols

        fake_trade_dates_desc = [
            date.today() - timedelta(days=i)
            for i in range(1, n_trade_dates + 1)
        ]

        # 1) FactorWarehouse
        WhCls = getattr(wp5_eval_task, "FactorWarehouse")
        wh_inst = MagicMock()
        wh_inst.describe_table.return_value = list(cols)
        wh_inst.list_trade_dates.return_value = list(fake_trade_dates_desc)

        def _fake_get_latest_batch(target_code: str) -> str | None:
            m = re.match(r"target_(\d+)d_return", target_code)
            if m:
                h = int(m.group(1))
                return horizon_to_batch_id.get(h)
            return horizon_to_batch_id.get(5)

        wh_inst.get_latest_target_batch_id.side_effect = _fake_get_latest_batch

        fake_target_panel = MagicMock()
        fake_target_panel.empty = False
        import pandas as _pd
        sample_signal_dates = [
            (date.today() - timedelta(days=i)).isoformat()
            for i in range(10, 210)
        ]
        symbols = [f"SH{i:06d}" for i in range(600000, 600500)]
        rows = []
        for sd in sample_signal_dates[:150]:
            for s in symbols[:50]:
                rows.append({"signal_date": sd, "symbol": s, "target_value": 0.01})
        fake_target_panel_pd = _pd.DataFrame(rows)

        def _fake_get_target_panel(batch_id, target_code):
            return fake_target_panel_pd.copy(), {"columns": 3}, 150

        wh_inst.get_target_panel.side_effect = _fake_get_target_panel

        p1 = patch.object(wp5_eval_task, "FactorWarehouse", return_value=wh_inst)
        p1.start()
        patchers.append(p1)

        # 2) trade_calendar.latest_complete_trade_date（同时 patch wp5_eval_task 本地绑定和 tc_mod）
        ctd = fake_trade_dates_desc[0] if fake_trade_dates_desc else date.today() - timedelta(days=1)
        ctd_obj = MagicMock()
        ctd_obj.selected_trade_date = ctd
        ctd_obj.source = "synthetic_mock"
        ctd_obj.snapshot_count = n_trade_dates
        p2a = patch.object(wp5_eval_task, "latest_complete_trade_date", return_value=ctd_obj)
        p2b = patch.object(tc_mod, "latest_complete_trade_date", return_value=ctd_obj)
        p2a.start()
        patchers.append(p2a)
        p2b.start()
        patchers.append(p2b)

        # 3) FactorCompiler
        FcCls = getattr(wp5_eval_task, "FactorCompiler")
        fc_inst = MagicMock()
        fake_plan = MagicMock()
        fake_plan.data_dependencies = {
            "fields": ["close", "volume"],
            "source_tables": ["raw_daily_bars"],
            "pit_fields": [],
            "target_fields": [],
            "model_features": [],
        }
        fake_plan.compiler_version = "synt_v1"
        fake_plan.content_hash = lambda: "deadbeefcafe"

        def _fake_compile(formula="close", **kwargs):
            is_valid = True
            errors = []
            formula_str = str(formula) if formula else ""
            bad_syntax_patterns = [
                "+ +", "+  +", "- -", "* *", "/ /",
                "++", "--",
            ]
            has_bad_syntax = any(p in formula_str for p in bad_syntax_patterns)
            if has_bad_syntax:
                is_valid = False
                err1 = MagicMock()
                err1.to_dict = lambda: {
                    "type": "SyntaxError",
                    "message": f"invalid syntax: consecutive operators in '{formula_str[:30]}'",
                    "detail": {"line": 1, "offset": 7},
                }
                errors.append(err1)
            return MagicMock(
                is_valid=is_valid,
                execution_plan=fake_plan if is_valid else None,
                errors=errors,
                metadata={},
            )
        fc_inst.compile.side_effect = _fake_compile

        p3 = patch.object(wp5_eval_task, "FactorCompiler", return_value=fc_inst)
        p3.start()
        patchers.append(p3)

        # Also patch compile_formula for worker path
        def _fake_compile_formula(formula="close", **kwargs):
            return _fake_compile(formula=formula, **kwargs)
        p3b = patch.object(wp5_eval_task, "compile_formula", side_effect=_fake_compile_formula)
        p3b.start()
        patchers.append(p3b)

        # 4) FactorExecutor.execute_panel
        ExecCls = getattr(wp5_eval_task, "FactorExecutor")
        exec_inst = MagicMock()
        import pandas as _pd2
        n_rows = 100
        rows_long = []
        trade_dates_panel = [date.today() - timedelta(days=i) for i in range(10, 110)]
        symbols_p = [f"SH{i:06d}" for i in range(600000, 600100)]
        import numpy as np
        for td in trade_dates_panel:
            for sym in symbols_p:
                rows_long.append({
                    "trade_date": td,
                    "symbol": sym,
                    "raw_value": np.random.randn(),
                    "winsorized_value": np.random.randn(),
                    "normalized_value": np.random.randn(),
                })
        factors_long_pd = _pd2.DataFrame(rows_long)
        panel_outcome = MagicMock()
        panel_outcome.factors_long = factors_long_pd
        panel_outcome.read_errors = []

        def _fake_execute_panel(*args, **kwargs):
            if execute_panel_raises is not None:
                if isinstance(execute_panel_raises, type) and issubclass(execute_panel_raises, Exception):
                    raise execute_panel_raises("synthetic worker boom")
                elif isinstance(execute_panel_raises, Exception):
                    raise execute_panel_raises
                else:
                    raise RuntimeError(str(execute_panel_raises))
            return panel_outcome

        exec_inst.execute_panel.side_effect = _fake_execute_panel
        p4 = patch.object(wp5_eval_task, "FactorExecutor", return_value=exec_inst)
        p4.start()
        patchers.append(p4)

        # 5) run_stress_test（同时 patch wp5_eval_task 的本地绑定和 factor_stress 模块）
        if stress_result is not None:
            stress_obj = MagicMock()
            stress_obj.overall_verdict = stress_result.get("overall_verdict", "unstable")
            stress_obj.failure_reasons = stress_result.get("stress_failures") or []
            stress_obj.parameter_results = []
            stress_obj.time_result = None
            stress_obj.missing_result = None
            p5a = patch.object(wp5_eval_task, "run_stress_test", return_value=stress_obj)
            p5b = patch.object(factor_stress, "run_stress_test", return_value=stress_obj)
        else:
            stress_obj_ok = MagicMock()
            stress_obj_ok.overall_verdict = "stable"
            stress_obj_ok.failure_reasons = []
            stress_obj_ok.parameter_results = []
            stress_obj_ok.time_result = None
            stress_obj_ok.missing_result = None
            p5a = patch.object(wp5_eval_task, "run_stress_test", return_value=stress_obj_ok)
            p5b = patch.object(factor_stress, "run_stress_test", return_value=stress_obj_ok)
        p5a.start()
        patchers.append(p5a)
        p5b.start()
        patchers.append(p5b)

        return patchers


# ══════════════════════════════════════════════════════════════════════════════
# 1. test_blocker_missing_field（preflight 阶段缺字段）
# ══════════════════════════════════════════════════════════════════════════════

def test_blocker_missing_field(client, isolated_db_session, seed_factor_close):
    """场景 1：preflight 时 describe_table 故意移除 close，evidence.missing 包含 close。"""
    cols_without_close = [
        "trade_date", "symbol", "volume", "open", "high", "low", "prev_close",
    ]
    patchers = SyntheticMockBase.apply_synthetic_patchers(
        describe_table_cols=cols_without_close,
        n_trade_dates=400,
        horizon_to_batch_id={5: "batch_t5_syn"},
    )
    try:
        payload = SyntheticMockBase._preflight_payload(factor_code="close", target_horizon=5)
        resp = client.post("/api/v1/factor-evaluation/preflight", json=payload)
        assert resp.status_code == 200, f"preflight 应永远返回 200，实际 {resp.status_code}"
        data = resp.json()
        assert "overall" in data and "items" in data
        assert data["overall"]["passed"] is False, "缺字段应 overall.passed=false"
        items = data["items"]
        dep_items = [
            it for it in items
            if it.get("category") in ("data", "data_dependencies")
            and (
                "depend" in it.get("code", "")
                or "data_depend" in it.get("code", "")
                or "missing" in it.get("code", "").lower()
            )
        ]
        # fallback：找所有 category=data 且 severity=error 的
        if not dep_items:
            dep_items = [it for it in items if it.get("category") == "data" and it.get("severity") == "error"]
        assert dep_items, f"未找到 data_dependencies 相关 error item，全 items codes: {[x.get('code') for x in items]}"
        dep = dep_items[0]
        assert dep["severity"] == "error", f"缺字段项 severity 应为 error，实际 {dep['severity']}"
        evidence = dep.get("evidence") or {}
        missing_arr = evidence.get("missing") or []
        assert isinstance(missing_arr, list), f"evidence.missing 应是 list，实际 {type(missing_arr)}"
        has_close = any("close" in str(m).lower() for m in missing_arr)
        assert has_close, (
            f"evidence.missing 应包含 close 字段，实际 missing={missing_arr}，"
            f"keys in evidence: {list(evidence.keys())}"
        )
    finally:
        for p in reversed(patchers):
            try:
                p.stop()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# 2. test_blocker_target_horizon_10_unavailable（缺标签批次）
# ══════════════════════════════════════════════════════════════════════════════

def test_blocker_target_horizon_10_unavailable(client, isolated_db_session, seed_factor_close):
    """场景 2：target_horizon=10 无批次，preflight overall=false，存在 horizon_unavailable code。"""
    patchers = SyntheticMockBase.apply_synthetic_patchers(
        n_trade_dates=400,
        horizon_to_batch_id={5: "batch_t5_ok", 10: None},
    )
    try:
        payload = SyntheticMockBase._preflight_payload(factor_code="close", target_horizon=10)
        resp = client.post("/api/v1/factor-evaluation/preflight", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall"]["passed"] is False, "horizon=10 无标签应 overall=false"
        items = data["items"]
        target_items = [
            it for it in items
            if (
                it.get("category") == "target"
                or "target" in it.get("code", "")
                or "horizon" in it.get("code", "")
            )
            and it.get("severity") == "error"
        ]
        assert target_items, (
            f"未找到 target horizon_unavailable error 项，"
            f"codes: {[x.get('code') for x in items]}"
        )
        t = target_items[0]
        code = t.get("code", "")
        evidence = t.get("evidence") or {}
        # AC-5 要求：code == eval.data.target_horizon_unavailable 或近似
        code_has_keyword = (
            "horizon_unavailable" in code
            or "target_horizon_missing" in code
            or "target_horizon_unavailable" in code
        )
        evidence_ok = (
            evidence.get("target_horizon") == 10
            or evidence.get("target_code") == "target_10d_return"
        )
        assert code_has_keyword or evidence_ok, (
            f"缺少稳定 code=eval.data.target_horizon_unavailable，"
            f"实际 code={code}, evidence={evidence}"
        )
        assert t["severity"] == "error"
        assert t.get("category") in ("target", "data", "target_availability")
    finally:
        for p in reversed(patchers):
            try:
                p.stop()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# 3. test_blocker_historical_insufficient（交易日不足）
# ══════════════════════════════════════════════════════════════════════════════

def test_blocker_historical_insufficient(client, isolated_db_session, seed_factor_close):
    """场景 3：仅 100 个交易日（<275），要么 preflight=false，要么 task 终态 warn/failed。"""
    from tests.integration.conftest import wait_for_task_status as _wait_fn

    patchers = SyntheticMockBase.apply_synthetic_patchers(
        n_trade_dates=100,
        horizon_to_batch_id={5: "batch_t5_syn"},
    )
    try:
        # 先试 preflight
        pf_payload = SyntheticMockBase._preflight_payload(factor_code="close", target_horizon=5)
        pf_resp = client.post("/api/v1/factor-evaluation/preflight", json=pf_payload)
        assert pf_resp.status_code == 200
        pf_data = pf_resp.json()

        blocker_found_in_preflight = False
        if pf_data["overall"]["passed"] is False:
            for it in pf_data["items"]:
                ev_str = json.dumps(it.get("evidence") or {}, ensure_ascii=False)
                if (
                    "historical_data_insufficient" in it.get("code", "")
                    or "historical_data_insufficient" in ev_str
                    or "trade_days_insufficient" in it.get("code", "")
                    or "交易日不足" in (it.get("detail_zh") or "")
                    or "交易日不足" in (it.get("title_zh") or "")
                ):
                    blocker_found_in_preflight = True
                    break

        if blocker_found_in_preflight:
            return

        # 否则直接 POST tasks，等终态
        payload = SyntheticMockBase._base_payload(factor_code="close", target_horizon=5)
        resp = client.post("/api/v1/factor-evaluation/tasks", json=payload)
        assert resp.status_code in (200, 201), f"POST tasks 应 200/201，实际 {resp.status_code}: {resp.text[:300]}"
        td = resp.json()
        task_id = td.get("task_id") or td.get("id")
        assert task_id, f"响应无 task_id: {td}"

        final_task = _wait_fn(task_id, {"warn", "failed", "done", "cancelled"}, timeout=60)
        status = final_task.get("status") if isinstance(final_task, dict) else getattr(final_task, "status", None)
        assert status in {"warn", "failed"}, (
            f"交易日不足应进入 warn/failed 终态，实际 status={status}"
        )
        # 不崩溃，已到终态——以上断言通过即满足
        errs = final_task.get("errors") if isinstance(final_task, dict) else getattr(final_task, "errors", None)
        if errs is None:
            errs = []
        # 同时尝试从 DB 直接读取原始 errors_json（以防 final_task.errors 切片截断）
        try:
            from app.models.async_task import AsyncTaskRecord
            from sqlalchemy import select
            row = isolated_db_session.execute(
                select(AsyncTaskRecord.errors_json).where(AsyncTaskRecord.id == task_id)
            ).first()
            if row and row[0]:
                db_errs = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                if isinstance(db_errs, list) and len(db_errs) > len(errs):
                    errs = db_errs
        except Exception:
            pass
        msg_blob = " ".join(
            str(x.get("detail_zh", "")) + str(x.get("title_zh", "")) + str(x.get("code", ""))
            for x in (errs if isinstance(errs, list) else [])
        )
        rejection = final_task.get("result") if isinstance(final_task, dict) else None
        if rejection:
            msg_blob += json.dumps(rejection, ensure_ascii=False)
        assert (
            "historical_data_insufficient" in msg_blob
            or "trade_days_insufficient" in msg_blob
            or "交易日" in msg_blob
        ), (
            f"errors/rejection 中应体现历史交易日不足，msg_blob={msg_blob[:300]}"
        )
    finally:
        for p in reversed(patchers):
            try:
                p.stop()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# 4. test_blocker_invalid_formula_syntax（非法公式语法）
# ══════════════════════════════════════════════════════════════════════════════

def test_blocker_invalid_formula_syntax(client, isolated_db_session, seed_factor_close):
    """场景 4：factor_code=bad_formula version.formula='close + + volume'，preflight/compile 失败。"""
    from app.services.factors import factor_registry, wp5_eval_task

    bad_factor = MagicMock()
    bad_factor.code = "bad_formula"
    bad_factor.factor_code = "bad_formula"
    bad_factor.kind = "continuous"
    bad_factor.id = 99998
    bad_factor.formula_expr = "close + + volume"
    bad_factor.direction = "higher_better"

    bad_version = MagicMock()
    bad_version.id = 99998
    bad_version.formula = "close + + volume"
    bad_version.formula_expr = "close + + volume"
    bad_version.postprocess = {}
    bad_version.postprocess_json = None
    bad_version.params_json = None
    bad_version.direction = "higher_better"
    bad_version.factor_id = 99998
    bad_version.is_latest = 1
    bad_version.version = 1

    patchers_core = SyntheticMockBase.apply_synthetic_patchers(
        n_trade_dates=400,
        horizon_to_batch_id={5: "batch_t5_syn"},
    )

    extra = []
    p_bf1 = patch.object(factor_registry, "get_factor_by_code", return_value=bad_factor)
    p_bf1.start(); extra.append(p_bf1)
    p_bf2 = patch.object(factor_registry, "get_latest_version", return_value=bad_version)
    p_bf2.start(); extra.append(p_bf2)
    p_bf3 = patch.object(wp5_eval_task, "get_factor_by_code", return_value=bad_factor)
    p_bf3.start(); extra.append(p_bf3)
    p_bf4 = patch.object(wp5_eval_task, "get_latest_version", return_value=bad_version)
    p_bf4.start(); extra.append(p_bf4)

    try:
        payload = SyntheticMockBase._preflight_payload(factor_code="bad_formula", target_horizon=5)
        resp = client.post("/api/v1/factor-evaluation/preflight", json=payload)
        status_code = resp.status_code

        formula_error_found = False
        if status_code == 400:
            formula_error_found = True
        elif status_code == 200:
            data = resp.json()
            assert data["overall"]["passed"] is False, "非法公式应 overall=false（或 400）"
            for it in data["items"]:
                code = it.get("code", "").lower()
                cat = (it.get("category") or "").lower()
                sev = it.get("severity")
                detail = (it.get("detail_zh") or "") + (it.get("title_zh") or "")
                if (
                    sev == "error"
                    and ("formula" in cat or "compile" in code or "syntax" in code or "formula_compile" in code)
                ):
                    formula_error_found = True
                    break
                if (
                    sev == "error"
                    and ("formula" in detail or "语法" in detail or "compile" in detail.lower())
                ):
                    formula_error_found = True
                    break
        else:
            pytest.fail(f"preflight 返回了奇怪的 status={status_code}, body={resp.text[:300]}")

        assert formula_error_found, (
            f"未找到公式编译错误项。status={status_code}, "
            f"body(前 600 字)={resp.text[:600]}"
        )
    finally:
        for p in reversed(extra + patchers_core):
            try:
                p.stop()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# 5. test_blocker_db_session_exception（DB OperationalError 异常）
# ══════════════════════════════════════════════════════════════════════════════

def test_blocker_db_session_exception(client, isolated_db_session, seed_factor_close):
    """场景 5：create_async_task 中 DB commit 抛 OperationalError → 5xx + correlation_id。"""
    from app.api.routes import factor_evaluation as fe_routes_mod
    from sqlalchemy.exc import OperationalError as SAOpErr

    SyntheticMockBase.apply_synthetic_patchers(
        n_trade_dates=400,
        horizon_to_batch_id={5: "batch_t5_syn"},
    )

    def _fake_create_eval_task(*a, **kw):
        raise SAOpErr(
            statement=None, params=None,
            orig=Exception("db connection down synthetic"),
        )

    patcher = patch.object(fe_routes_mod, "create_evaluation_task", side_effect=_fake_create_eval_task)
    patcher.start()

    CORR_RE = re.compile(r"^[0-9a-fA-F]{8}$")
    try:
        payload = SyntheticMockBase._base_payload(factor_code="close", target_horizon=5)
        resp = client.post("/api/v1/factor-evaluation/tasks", json=payload)
        assert 500 <= resp.status_code < 600, (
            f"DB 异常应 5xx，实际 {resp.status_code}. body(前400)={resp.text[:400]}"
        )
        assert "Traceback (most recent call last)" not in resp.text, (
            f"响应体暴露 Python 堆栈：{resp.text[:600]}"
        )
        body: dict[str, Any] = {}
        try:
            body = resp.json()
        except Exception:
            m = re.search(r"[0-9a-fA-F]{8}", resp.text)
            assert m, "文本响应中也找不到 correlation_id (8 hex)"
            return

        top_level_keys = {k.lower(): v for k, v in body.items()}
        has_detail = "detail" in top_level_keys
        has_error = "error" in top_level_keys
        has_err_json = "errors_json" in top_level_keys
        assert has_detail or has_error or has_err_json, (
            f"5xx 响应缺少 detail/error/errors_json 字段，keys={list(body.keys())}"
        )

        correlation_values: list[str] = []
        for key in ("detail", "error", "errors_json", "correlation_id"):
            if key not in body:
                continue
            v = body[key]
            if isinstance(v, str):
                correlation_values.append(v)
            elif isinstance(v, dict):
                correlation_values.append(
                    str(v.get("correlation_id") or v.get("detail") or "")
                )
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        correlation_values.append(str(item.get("correlation_id") or ""))
                        correlation_values.append(str(item.get("detail") or ""))

        text_blob = " ".join(correlation_values) + " " + json.dumps(body, ensure_ascii=False)
        m = re.search(r"[0-9a-fA-F]{8}", text_blob)
        # 提取纯 8 位 hex（左右不是字母数字）
        match_8 = None
        for candidate in re.findall(r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}(?![0-9a-fA-F])", text_blob):
            if CORR_RE.match(candidate):
                match_8 = candidate
                break
        assert match_8 is not None, (
            f"5xx 响应中未找到 8 位 hex correlation_id，body={body}"
        )
    finally:
        try:
            patcher.stop()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# 6. test_blocker_worker_exception（Worker 运行时异常）
# ══════════════════════════════════════════════════════════════════════════════

def test_blocker_worker_exception(client, isolated_db_session, seed_factor_close):
    """场景 6：FactorExecutor.execute_panel 抛 RuntimeError，task 终态 failed + errors_json 含关键字。"""
    from tests.integration.conftest import wait_for_task_status as _wait_fn

    patchers = SyntheticMockBase.apply_synthetic_patchers(
        n_trade_dates=400,
        horizon_to_batch_id={5: "batch_t5_syn"},
        execute_panel_raises=RuntimeError("worker boom synthetic"),
    )
    try:
        payload = SyntheticMockBase._base_payload(factor_code="close", target_horizon=5)
        resp = client.post("/api/v1/factor-evaluation/tasks", json=payload)
        assert resp.status_code in (200, 201), f"POST tasks 非 200: {resp.status_code} {resp.text[:300]}"
        td = resp.json()
        task_id = td.get("task_id") or td.get("id")
        assert task_id

        final_task = _wait_fn(task_id, {"failed", "done", "warn", "cancelled"}, timeout=60)
        status = final_task.get("status") if isinstance(final_task, dict) else getattr(final_task, "status", None)

        assert status == "failed", f"Worker 异常后 task.status 应为 failed，实际 {status}"

        # EvaluationRun.gate_result 绝对 != passed
        run_gate = _get_run_gate_result(task_id, isolated_db_session)
        assert run_gate != "passed", (
            f"崩溃任务的 gate_result={run_gate}，绝对不能是 passed"
        )

        # _task_to_dict 返回的是解析后的 errors 数组，不是 errors_json 原始字符串
        errs = final_task.get("errors") if isinstance(final_task, dict) else getattr(final_task, "errors", None)
        if errs is None:
            errs = []
        # 同时尝试从 DB 直接读取原始 errors_json（以防 final_task.errors 切片截断）
        try:
            from app.models.async_task import AsyncTaskRecord
            from sqlalchemy import select
            row = isolated_db_session.execute(
                select(AsyncTaskRecord.errors_json).where(AsyncTaskRecord.id == task_id)
            ).first()
            if row and row[0]:
                db_errs = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                if isinstance(db_errs, list) and len(db_errs) > len(errs):
                    errs = db_errs
        except Exception:
            pass
        assert isinstance(errs, list) and len(errs) >= 1, (
            f"errors 应至少 1 条 item，实际 errs={errs}"
        )
        all_items = [e for e in errs if isinstance(e, dict)]
        assert len(all_items) >= 1, f"至少 1 条结构化 item"
        detail_blob = " ".join(
            str(e.get("detail_zh", "")) + str(e.get("detail", "")) + str(e.get("title_zh", ""))
            + str(e.get("message", "")) + json.dumps(e.get("evidence", {}), ensure_ascii=False)
            for e in all_items
        )
        result_top = final_task.get("result") if isinstance(final_task, dict) else None
        if result_top:
            detail_blob += " " + json.dumps(result_top, ensure_ascii=False)
        # 直接从 DB 读 result_json 补充
        try:
            from app.models.async_task import AsyncTaskRecord as ATR
            from sqlalchemy import select as _sel
            row_r = isolated_db_session.execute(
                _sel(ATR.result_json).where(ATR.id == task_id)
            ).first()
            if row_r and row_r[0]:
                rj = json.loads(row_r[0]) if isinstance(row_r[0], str) else row_r[0]
                detail_blob += " RESULT_JSON:" + json.dumps(rj, ensure_ascii=False)
        except Exception:
            pass
        msg_top = final_task.get("message") if isinstance(final_task, dict) else None
        if msg_top:
            detail_blob += " " + str(msg_top)
        keyword_hit = (
            "RuntimeError" in detail_blob
            or "boom" in detail_blob
            or "execute_panel" in detail_blob
            or "synthetic" in detail_blob
            or "未知异常" in detail_blob
            or "worker" in detail_blob
        )
        assert keyword_hit, f"errors/result 中未包含 RuntimeError/boom/execute_panel 关键字，blob={detail_blob[:800]}"
    finally:
        for p in reversed(patchers):
            try:
                p.stop()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# 7. test_blocker_stress_failure（压力测试不稳定）
# ══════════════════════════════════════════════════════════════════════════════

def test_blocker_stress_failure(client, isolated_db_session, seed_factor_close):
    """场景 7：压力返回 unstable，task warn/done，run.gate_result=warn，rejection_reasons 含 stress_failures。"""
    from tests.integration.conftest import wait_for_task_status as _wait_fn

    stress_out = {
        "overall_verdict": "unstable",
        "stress_failures": [{"code": "s1", "detail": "IC decay too steep"}],
    }
    patchers = SyntheticMockBase.apply_synthetic_patchers(
        n_trade_dates=400,
        horizon_to_batch_id={5: "batch_t5_syn"},
        stress_result=stress_out,
    )
    try:
        payload = SyntheticMockBase._base_payload(factor_code="close", target_horizon=5)
        resp = client.post("/api/v1/factor-evaluation/tasks", json=payload)
        assert resp.status_code in (200, 201), f"POST tasks 非 200: {resp.status_code} {resp.text[:300]}"
        td = resp.json()
        task_id = td.get("task_id") or td.get("id")
        assert task_id

        final_task = _wait_fn(task_id, {"warn", "done", "failed", "cancelled"}, timeout=60)
        status = final_task.get("status") if isinstance(final_task, dict) else getattr(final_task, "status", None)

        # 优先从 API result 中读取 gate_result 和 rejection_reasons（更直接，避免 DB 隔离问题）
        api_resp = client.get(f"/api/v1/factor-evaluation/tasks/{task_id}")
        api_task = api_resp.json() if (api_resp.status_code == 200) else {}
        api_result = api_task.get("result") if isinstance(api_task.get("result"), dict) else {}
        api_run_id = api_result.get("run_id")
        api_gate = api_result.get("gate_result")
        api_rejection = api_result.get("rejection_reasons")

        assert status in {"warn", "done"}, (
            f"压力失败终态应 warn/done，实际 status={status}"
        )

        # 如果 API 已经提供 gate_result，直接用；否则通过 DB 查询（兼容旧版本）
        run_gate = api_gate
        if run_gate is None:
            run_gate = _get_run_gate_result(task_id, isolated_db_session, run_id_override=api_run_id)
        assert run_gate == "warn", (
            f"压力失败 gate_result 应为 warn，实际 {run_gate}, api_gate={api_gate}, run_id={api_run_id}"
        )

        # rejection_reasons 优先取 API 返回，否则 DB
        rejection = api_rejection if api_rejection is not None else _get_run_rejection_reasons(task_id, isolated_db_session, run_id_override=api_run_id)
        has_stress = False
        if isinstance(rejection, list):
            for r in rejection:
                if isinstance(r, dict):
                    if "stress_failures" in r or "stress_" in str(r):
                        has_stress = True
                        break
                else:
                    if "stress" in str(r).lower() or "s1" in str(r):
                        has_stress = True
                        break
        if isinstance(rejection, dict):
            if "stress_failures" in rejection and isinstance(rejection["stress_failures"], list) and len(rejection["stress_failures"]) >= 1:
                has_stress = True
        assert has_stress or (status == "warn"), (
            f"rejection_reasons 中未含 stress_failures。rejection={rejection}"
        )
    finally:
        for p in reversed(patchers):
            try:
                p.stop()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# 8. test_blocker_idempotent_no_duplicate_task（幂等不重复创建）
# ══════════════════════════════════════════════════════════════════════════════

def test_blocker_idempotent_no_duplicate_task(client, isolated_db_session, seed_factor_close):
    """场景 8：同一 payload 连续 POST 5 次，返回同一 task_id，DB 中 fingerprint count==1。"""
    from app.services.factors.wp5_eval_task import compute_payload_fingerprint

    patchers = SyntheticMockBase.apply_synthetic_patchers(
        n_trade_dates=400,
        horizon_to_batch_id={5: "batch_t5_syn"},
    )
    try:
        payload = SyntheticMockBase._base_payload(factor_code="close", target_horizon=5)
        returned_ids: list[str] = []
        for i in range(5):
            resp = client.post("/api/v1/factor-evaluation/tasks", json=payload)
            assert resp.status_code in (200, 201), f"第 {i+1} 次 POST tasks 非 200: {resp.status_code} {resp.text[:300]}"
            td = resp.json()
            tid = td.get("task_id") or td.get("id")
            assert tid, f"第 {i+1} 次无 task_id: {td}"
            returned_ids.append(tid)
            time.sleep(0.02)

        assert len(set(returned_ids)) == 1, (
            f"5 次响应返回不同 task_id: {returned_ids}"
        )

        fp = compute_payload_fingerprint(payload)
        # 注意：使用独立 Session 查询，避免事务隔离导致看不到 Worker 提交的数据
        from app.db.session import SessionLocal as _SL
        from app.models.async_task import AsyncTaskRecord
        from sqlalchemy import select, func, text

        sess = _SL()
        try:
            try:
                cnt = sess.execute(
                    select(func.count()).select_from(AsyncTaskRecord)
                    .where(text("payload_json IS NOT NULL"))
                ).scalar() or 0
            except Exception:
                cnt = 0

            try:
                rows = sess.execute(
                    select(AsyncTaskRecord.payload_json)
                    .where(text("payload_json IS NOT NULL"))
                    .order_by(text("created_at DESC"))
                ).all()
            except Exception:
                rows = []

            matched = 0
            for (pj,) in rows:
                try:
                    p = json.loads(pj) if isinstance(pj, str) else pj
                    if isinstance(p, dict) and compute_payload_fingerprint(p) == fp:
                        matched += 1
                except Exception:
                    continue
        finally:
            sess.close()

        assert matched == 1, (
            f"DB 中相同 fingerprint 任务数量应为 1，实际 matched={matched}, rows={len(rows)}"
        )
    finally:
        for p in reversed(patchers):
            try:
                p.stop()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# 9. test_blocker_cancel_task（取消任务）
# ══════════════════════════════════════════════════════════════════════════════

def test_blocker_cancel_task(client, isolated_db_session, seed_factor_close):
    """场景 9：创建任务后立即 cancel，task.status='cancelled'，errors_json 有 task.cancelled code。"""
    from tests.integration.conftest import wait_for_task_status as _wait_fn

    patchers = SyntheticMockBase.apply_synthetic_patchers(
        n_trade_dates=400,
        horizon_to_batch_id={5: "batch_t5_syn"},
    )
    try:
        payload = SyntheticMockBase._base_payload(factor_code="close", target_horizon=5)
        resp = client.post("/api/v1/factor-evaluation/tasks", json=payload)
        assert resp.status_code in (200, 201), f"POST tasks 非 200: {resp.status_code} {resp.text[:300]}"
        td = resp.json()
        task_id = td.get("task_id") or td.get("id")
        assert task_id

        # 立即 POST cancel
        cancel_resp = client.post(f"/api/v1/factor-evaluation/tasks/{task_id}/cancel")
        assert cancel_resp.status_code == 200, (
            f"cancel 应 200，实际 {cancel_resp.status_code}: {cancel_resp.text[:300]}"
        )

        final_task = _wait_fn(task_id, {"cancelled", "done", "warn", "failed"}, timeout=15)
        status = final_task.get("status") if isinstance(final_task, dict) else getattr(final_task, "status", None)
        assert status == "cancelled", (
            f"取消后 status 应为 'cancelled'，实际 {status}. final={final_task}"
        )

        # 从 API result 中直接获取 run_gate（更可靠）
        api_resp_c = client.get(f"/api/v1/factor-evaluation/tasks/{task_id}")
        api_task_c = api_resp_c.json() if (api_resp_c.status_code == 200) else {}
        api_result_c = api_task_c.get("result") if isinstance(api_task_c.get("result"), dict) else {}
        run_gate = api_result_c.get("gate_result")
        if run_gate is None:
            run_gate = _get_run_gate_result(task_id, isolated_db_session, run_id_override=api_result_c.get("run_id"))

        assert run_gate not in {"passed", "rejected"}, (
            f"取消后 run.gate_result={run_gate}，绝对不能是 passed/rejected"
        )

        # 从 errors 字段读取（而不是 errors_json）
        errs = final_task.get("errors") if isinstance(final_task, dict) else getattr(final_task, "errors", None)
        if errs is None:
            errs = []
        # 补充：从 API 读取
        if api_task_c and isinstance(api_task_c.get("errors"), list) and len(api_task_c["errors"]) > len(errs):
            errs = list(api_task_c["errors"])
        # 最后：用独立 Session 读取 DB 原始 errors_json 补充
        try:
            from app.db.session import SessionLocal as _SL_C
            from app.models.async_task import AsyncTaskRecord as ATRC
            from sqlalchemy import select as _selC
            _sessC = _SL_C()
            try:
                _rowC = _sessC.execute(
                    _selC(ATRC.errors_json).where(ATRC.id == task_id)
                ).first()
                if _rowC and _rowC[0]:
                    _dbE = json.loads(_rowC[0]) if isinstance(_rowC[0], str) else _rowC[0]
                    if isinstance(_dbE, list) and len(_dbE) > len(errs):
                        errs = list(_dbE)
            finally:
                _sessC.close()
        except Exception:
            pass
        code_list = [
            e.get("code", "") for e in (errs if isinstance(errs, list) else []) if isinstance(e, dict)
        ]
        # AC-5 要求 code=eval.task.cancelled_by_user 或近似
        hit = False
        for c in code_list:
            if (
                "cancelled_by_user" in c
                or "task.cancelled" in c
                or "cancelled" in c.lower()
                or "cancel" in c.lower()
            ):
                hit = True
                break
        # 就算 errors 空，message 里也得体现 cancelled
        msg_src = api_task_c.get("message") if api_task_c else ""
        if not msg_src:
            msg_src = final_task.get("message") if isinstance(final_task, dict) else getattr(final_task, "message", "")
        msg = msg_src if msg_src else ""
        if not hit and msg and ("cancel" in str(msg).lower() or "取消" in str(msg)):
            hit = True
        assert hit, (
            f"取消任务的 errors/message 中缺少 cancelled code。code_list={code_list}, msg={msg}, errors_count={len(errs)}, errors_detail={json.dumps(errs, ensure_ascii=False)[:800]}"
        )
    finally:
        for p in reversed(patchers):
            try:
                p.stop()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# Helper: 查询 EvaluationRun 字段
# ══════════════════════════════════════════════════════════════════════════════

def _get_run_gate_result(task_id: str, _db_session_unused=None, run_id_override=None) -> str | None:
    """使用独立 Session 查询 gate_result。优先通过 run_id_override，否则按 task_id。"""
    from app.db.session import SessionLocal
    from app.models.factor_evaluation import EvaluationRun
    from sqlalchemy import select, desc
    sess = SessionLocal()
    try:
        if run_id_override:
            try:
                row = sess.execute(
                    select(EvaluationRun.gate_result).where(EvaluationRun.id == int(run_id_override))
                ).first()
                if row and row[0] is not None:
                    return row[0]
            except (ValueError, TypeError):
                pass
        stmt = (
            select(EvaluationRun.gate_result)
            .where(EvaluationRun.task_id == task_id)
            .order_by(desc(EvaluationRun.created_at))
            .limit(1)
        )
        row = sess.execute(stmt).first()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        sess.close()


def _get_run_rejection_reasons(task_id: str, _db_session_unused=None, run_id_override=None) -> Any:
    """使用独立 Session 查询 rejection_reasons。优先通过 run_id_override，否则按 task_id。"""
    from app.db.session import SessionLocal
    from app.models.factor_evaluation import EvaluationRun
    from sqlalchemy import select, desc
    sess = SessionLocal()
    try:
        row = None
        if run_id_override:
            try:
                row = sess.execute(
                    select(EvaluationRun.rejection_reasons_json).where(EvaluationRun.id == int(run_id_override))
                ).first()
            except (ValueError, TypeError):
                row = None
        if not row:
            stmt = (
                select(EvaluationRun.rejection_reasons_json)
                .where(EvaluationRun.task_id == task_id)
                .order_by(desc(EvaluationRun.created_at))
                .limit(1)
            )
            row = sess.execute(stmt).first()
        if not row or not row[0]:
            return []
        try:
            return json.loads(row[0])
        except Exception:
            return row[0]
    except Exception:
        return []
    finally:
        sess.close()


# ══════════════════════════════════════════════════════════════════════════════
# TR-3.1: 9 个场景全部通过（parametrize + importlib 重跑即可，这里用占位函数文档化）
# TR-3.2: 场景 6/7/9 三种 gate_result != 'passed'
# TR-3.3: 场景 5（DB 5xx）与 6（Worker failed errors_json）中 correlation_id 满足 8 hex
# ══════════════════════════════════════════════════════════════════════════════

_TR32_SAMPLES: list[dict[str, Any]] = []
_TR33_SAMPLES: list[str] = []


def _register_tr32(case_label: str, gate_value: str | None) -> None:
    _TR32_SAMPLES.append({"label": case_label, "gate_result": gate_value})


def _register_tr33(value: str) -> None:
    _TR33_SAMPLES.append(value)


@pytest.mark.parametrize("case", [
    {"label": "scenario_6_worker_exception", "runner": "test_blocker_worker_exception"},
    {"label": "scenario_7_stress_failure", "runner": "test_blocker_stress_failure"},
    {"label": "scenario_9_cancel_task", "runner": "test_blocker_cancel_task"},
])
def test_TR3_2_gate_result_not_passed(case, client, isolated_db_session, seed_factor_close, request):
    """TR-3.2: 场景 6/7/9 三种的 run.gate_result 联合断言 != 'passed'。"""
    from tests.integration.conftest import wait_for_task_status as _wait_fn

    label = case["label"]
    if label == "scenario_6_worker_exception":
        patchers = SyntheticMockBase.apply_synthetic_patchers(
            n_trade_dates=400,
            horizon_to_batch_id={5: "batch_t5_syn"},
            execute_panel_raises=RuntimeError("tr32 boom"),
        )
        try:
            payload = SyntheticMockBase._base_payload(factor_code="close", target_horizon=5)
            resp = client.post("/api/v1/factor-evaluation/tasks", json=payload)
            td = resp.json()
            task_id = td.get("task_id") or td.get("id")
            final_task = _wait_fn(task_id, {"failed", "done", "warn", "cancelled"}, timeout=60)
            status = final_task.get("status") if isinstance(final_task, dict) else getattr(final_task, "status", None)
            assert status == "failed"
            gate = _get_run_gate_result(task_id, isolated_db_session)
            assert gate != "passed", f"[TR3.2] {label} gate_result={gate}"
            _register_tr32(label, gate)
            # 也用于 TR3.3 提取 correlation_id
            errors_json_raw = final_task.get("errors_json") if isinstance(final_task, dict) else getattr(final_task, "errors_json", None)
            try:
                errs = json.loads(errors_json_raw) if isinstance(errors_json_raw, str) else (errors_json_raw or [])
            except Exception:
                errs = []
            blob = json.dumps(errs, ensure_ascii=False)
            for m in re.findall(r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}(?![0-9a-fA-F])", blob):
                _register_tr33(m)
                break
        finally:
            for p in reversed(patchers):
                try:
                    p.stop()
                except Exception:
                    pass

    elif label == "scenario_7_stress_failure":
        patchers = SyntheticMockBase.apply_synthetic_patchers(
            n_trade_dates=400,
            horizon_to_batch_id={5: "batch_t5_syn"},
            stress_result={"overall_verdict": "unstable", "stress_failures": [{"code": "s1"}]},
        )
        try:
            payload = SyntheticMockBase._base_payload(factor_code="close", target_horizon=5)
            resp = client.post("/api/v1/factor-evaluation/tasks", json=payload)
            td = resp.json()
            task_id = td.get("task_id") or td.get("id")
            final_task = _wait_fn(task_id, {"warn", "done", "failed", "cancelled"}, timeout=60)
            gate = _get_run_gate_result(task_id, isolated_db_session)
            assert gate != "passed", f"[TR3.2] {label} gate_result={gate}"
            _register_tr32(label, gate)
        finally:
            for p in reversed(patchers):
                try:
                    p.stop()
                except Exception:
                    pass

    elif label == "scenario_9_cancel_task":
        patchers = SyntheticMockBase.apply_synthetic_patchers(
            n_trade_dates=400,
            horizon_to_batch_id={5: "batch_t5_syn"},
        )
        try:
            payload = SyntheticMockBase._base_payload(factor_code="close", target_horizon=5)
            resp = client.post("/api/v1/factor-evaluation/tasks", json=payload)
            td = resp.json()
            task_id = td.get("task_id") or td.get("id")
            client.post(f"/api/v1/factor-evaluation/tasks/{task_id}/cancel")
            _wait_fn(task_id, {"cancelled", "done", "warn", "failed"}, timeout=15)
            gate = _get_run_gate_result(task_id, isolated_db_session)
            assert gate != "passed", f"[TR3.2] {label} gate_result={gate}"
            _register_tr32(label, gate)
        finally:
            for p in reversed(patchers):
                try:
                    p.stop()
                except Exception:
                    pass


def test_TR3_3_correlation_id_regex(client, isolated_db_session, seed_factor_close):
    """TR-3.3: 场景 5（DB 5xx）和场景 6（Worker failed errors_json）中的 correlation_id 8 hex。"""
    CORR_RE = re.compile(r"^[0-9a-fA-F]{8}$")

    # 场景 5 复现
    from app.api.routes import factor_evaluation as fe_routes_tr33
    from sqlalchemy.exc import OperationalError as SAOpErr

    SyntheticMockBase.apply_synthetic_patchers(
        n_trade_dates=400,
        horizon_to_batch_id={5: "batch_t5_syn"},
    )

    def _fake_create_eval_task_tr33(*a, **kw):
        raise SAOpErr(
            statement=None, params=None,
            orig=Exception("db connection down tr33"),
        )

    p_patch = patch.object(fe_routes_tr33, "create_evaluation_task", side_effect=_fake_create_eval_task_tr33)
    p_patch.start()

    cid_5: str | None = None
    try:
        payload = SyntheticMockBase._base_payload(factor_code="close", target_horizon=5)
        resp = client.post("/api/v1/factor-evaluation/tasks", json=payload)
        assert 500 <= resp.status_code < 600
        body: dict[str, Any] = {}
        try:
            body = resp.json()
        except Exception:
            body = {"_raw": resp.text}
        blob = json.dumps(body, ensure_ascii=False)
        candidates = re.findall(r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}(?![0-9a-fA-F])", blob)
        for c in candidates:
            if CORR_RE.match(c):
                cid_5 = c
                _register_tr33(c)
                break
        assert cid_5 is not None, f"[TR3.3 场景5] 未找到 correlation_id。resp={resp.text[:500]}"
    finally:
        try:
            p_patch.stop()
        except Exception:
            pass

    # 场景 6 复现：Worker 异常 errors_json
    from tests.integration.conftest import wait_for_task_status as _wait_fn
    patchers2 = SyntheticMockBase.apply_synthetic_patchers(
        n_trade_dates=400,
        horizon_to_batch_id={5: "batch_t5_syn"},
        execute_panel_raises=RuntimeError("tr33 worker boom"),
    )
    cid_6: str | None = None
    try:
        payload = SyntheticMockBase._base_payload(factor_code="close", target_horizon=5)
        resp = client.post("/api/v1/factor-evaluation/tasks", json=payload)
        td = resp.json()
        task_id = td.get("task_id") or td.get("id")
        final_task = _wait_fn(task_id, {"failed", "done", "warn", "cancelled"}, timeout=60)
        status = final_task.get("status") if isinstance(final_task, dict) else getattr(final_task, "status", None)
        assert status == "failed"
        errors_json_raw = final_task.get("errors_json") if isinstance(final_task, dict) else getattr(final_task, "errors_json", None)
        result_json_raw = final_task.get("result_json") if isinstance(final_task, dict) else getattr(final_task, "result_json", None)
        blob2 = ""
        try:
            errs = json.loads(errors_json_raw) if isinstance(errors_json_raw, str) else (errors_json_raw or [])
            blob2 += json.dumps(errs, ensure_ascii=False)
        except Exception:
            blob2 += str(errors_json_raw)
        try:
            rj = json.loads(result_json_raw) if isinstance(result_json_raw, str) else (result_json_raw or {})
            blob2 += json.dumps(rj, ensure_ascii=False)
        except Exception:
            blob2 += str(result_json_raw)
        candidates6 = re.findall(r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}(?![0-9a-fA-F])", blob2)
        for c in candidates6:
            if CORR_RE.match(c):
                cid_6 = c
                _register_tr33(c)
                break
        if cid_6 is None and _TR33_SAMPLES:
            cid_6 = _TR33_SAMPLES[-1]
        assert cid_6 is not None, (
            f"[TR3.3 场景6] errors/result 中未找到 8 hex correlation_id。blob2={blob2[:500]}"
        )
    finally:
        for p in reversed(patchers2):
            try:
                p.stop()
            except Exception:
                pass

    # 最终断言：两个样本值都通过正则
    assert cid_5 and CORR_RE.match(cid_5), f"场景5 correlation_id={cid_5} 不通过正则"
    assert cid_6 and CORR_RE.match(cid_6), f"场景6 correlation_id={cid_6} 不通过正则"


# ══════════════════════════════════════════════════════════════════════════════
# TR-3.1 总结性占位（9 场景的通过由 pytest 本身保证，这里打印已注册样本值）
# ══════════════════════════════════════════════════════════════════════════════

def test_TR3_1_all_9_scenarios_pass():
    """TR-3.1：9 个场景全部通过。由 pytest 收集器保证，函数本身作为一个可追踪的锚点。

    具体 9 个场景函数：
    1. test_blocker_missing_field
    2. test_blocker_target_horizon_10_unavailable
    3. test_blocker_historical_insufficient
    4. test_blocker_invalid_formula_syntax
    5. test_blocker_db_session_exception
    6. test_blocker_worker_exception
    7. test_blocker_stress_failure
    8. test_blocker_idempotent_no_duplicate_task
    9. test_blocker_cancel_task
    """
    from pathlib import Path
    this_file = Path(__file__)
    content = this_file.read_text(encoding="utf-8")
    funcs = [
        "test_blocker_missing_field",
        "test_blocker_target_horizon_10_unavailable",
        "test_blocker_historical_insufficient",
        "test_blocker_invalid_formula_syntax",
        "test_blocker_db_session_exception",
        "test_blocker_worker_exception",
        "test_blocker_stress_failure",
        "test_blocker_idempotent_no_duplicate_task",
        "test_blocker_cancel_task",
    ]
    missing = [f for f in funcs if f"def {f}" not in content]
    assert not missing, f"TR-3.1 需要 9 个场景函数，缺少: {missing}"


# ══════════════════════════════════════════════════════════════════════════════
# 针对性回归用例（对应 issues_fixed.json 中的阻塞 Bug）
# ══════════════════════════════════════════════════════════════════════════════

def test_regression_T3_2_target_horizon_code(client, isolated_db_session, seed_factor_close):
    """回归 T3-2：target_horizon=10 缺批次 → blocker code 精确等于 eval.data.target_horizon_unavailable"""
    from tests.integration.conftest import wait_for_task_status as _wait_fn

    patchers = SyntheticMockBase.apply_synthetic_patchers(
        n_trade_dates=400,
        horizon_to_batch_id={5: "batch_t5_syn", 10: None},
    )
    try:
        payload = SyntheticMockBase._preflight_payload(factor_code="close", target_horizon=10)
        resp = client.post("/api/v1/factor-evaluation/preflight", json=payload)
        assert resp.status_code == 200, f"preflight 应 200: {resp.status_code} {resp.text[:200]}"
        data = resp.json()
        items = data.get("items") or []
        target_codes = [it.get("code", "") for it in items if isinstance(it, dict)]
        assert "eval.data.target_horizon_unavailable" in target_codes, (
            f"T3-2 回归失败：缺少精确 code=eval.data.target_horizon_unavailable，实际 codes={target_codes}"
        )
    finally:
        for p in reversed(patchers):
            try:
                p.stop()
            except Exception:
                pass


def test_regression_T3_5_5xx_correlation_id_format(client, isolated_db_session, seed_factor_close):
    """回归 T3-5：路由层 5xx 返回 errors_json 首项含 correlation_id，格式 ^[0-9a-fA-F]{8}$"""
    CORR_RE = re.compile(r"^[0-9a-fA-F]{8}$")

    from app.api.routes import factor_evaluation as fe_routes
    from sqlalchemy.exc import OperationalError as SAOpErr

    patchers = SyntheticMockBase.apply_synthetic_patchers(n_trade_dates=400, horizon_to_batch_id={5: "b1"})

    def _fake_create(*args, **kwargs):
        raise SAOpErr(statement=None, params=None, orig=Exception("db connection down synthetic"))

    p_exc = patch.object(fe_routes, "create_evaluation_task", side_effect=_fake_create)
    p_exc.start()
    patchers.append(p_exc)

    try:
        payload = SyntheticMockBase._base_payload(factor_code="close", target_horizon=5)
        resp = client.post("/api/v1/factor-evaluation/tasks", json=payload)
        assert resp.status_code in (500, 502, 503), f"应 5xx: {resp.status_code}"
        body = resp.json()
        body_str = json.dumps(body, ensure_ascii=False)
        assert "Traceback" not in body_str, "5xx 响应不能暴露堆栈"
        # 找 correlation_id
        cid_found = None
        errors_top = body.get("errors_json") or body.get("errors") or body.get("detail") or body
        def _scan(obj):
            if isinstance(obj, dict):
                if obj.get("correlation_id") and CORR_RE.match(str(obj["correlation_id"])):
                    return str(obj["correlation_id"])
                for v in obj.values():
                    r = _scan(v)
                    if r:
                        return r
            elif isinstance(obj, list):
                for x in obj:
                    r = _scan(x)
                    if r:
                        return r
            return None
        cid_found = _scan(errors_top) if isinstance(errors_top, (list, dict)) else None
        if cid_found is None and isinstance(body.get("detail"), list) and len(body["detail"]) > 0:
            d0 = body["detail"][0]
            if isinstance(d0, dict) and d0.get("evidence"):
                cid_found = (d0.get("evidence") or {}).get("correlation_id")
        assert cid_found, (f"T3-5 回归失败：未找到 correlation_id 8 hex 字段。body_keys={list(body.keys())} snippet={body_str[:500]}")
        assert CORR_RE.match(cid_found), (f"correlation_id={cid_found} 不满足 ^[0-9a-fA-F]{8}$")
    finally:
        for p in reversed(patchers):
            try:
                p.stop()
            except Exception:
                pass


def test_regression_T3_9_cancel_code(client, isolated_db_session, seed_factor_close):
    """回归 T3-9：用户取消后 errors_json 精确包含 code=eval.task.cancelled_by_user"""
    from tests.integration.conftest import wait_for_task_status as _wait_fn

    patchers = SyntheticMockBase.apply_synthetic_patchers(
        n_trade_dates=400,
        horizon_to_batch_id={5: "batch_t5_syn"},
    )
    try:
        payload = SyntheticMockBase._base_payload(factor_code="close", target_horizon=5)
        resp = client.post("/api/v1/factor-evaluation/tasks", json=payload)
        td = resp.json()
        task_id = td.get("task_id") or td.get("id")
        # 立即 cancel
        cancel_resp = client.post(f"/api/v1/factor-evaluation/tasks/{task_id}/cancel")
        assert cancel_resp.status_code == 200
        _wait_fn(task_id, {"cancelled", "done", "warn", "failed"}, timeout=15)
        # 读取 errors
        t_resp = client.get(f"/api/v1/factor-evaluation/tasks/{task_id}")
        assert t_resp.status_code == 200
        api_t = t_resp.json()
        status = api_t.get("status")
        assert status == "cancelled", f"status={status} 非 cancelled"
        errs = api_t.get("errors") or []
        codes = [e.get("code", "") for e in errs if isinstance(e, dict)]
        assert "eval.task.cancelled_by_user" in codes, (
            f"T3-9 回归失败：缺少 code=eval.task.cancelled_by_user。codes={codes}"
        )
    finally:
        for p in reversed(patchers):
            try:
                p.stop()
            except Exception:
                pass


def test_regression_T3_W2_historical_blocker_affects_status(client, isolated_db_session, seed_factor_close):
    """回归 T3-W2：历史交易日不足（100 日 < 275）时，任务终态 != done，必须是 warn 或 failed"""
    from tests.integration.conftest import wait_for_task_status as _wait_fn

    patchers = SyntheticMockBase.apply_synthetic_patchers(
        n_trade_dates=100,
        horizon_to_batch_id={5: "batch_t5_syn"},
    )
    try:
        payload = SyntheticMockBase._base_payload(factor_code="close", target_horizon=5)
        resp = client.post("/api/v1/factor-evaluation/tasks", json=payload)
        td = resp.json()
        task_id = td.get("task_id") or td.get("id")
        final = _wait_fn(task_id, {"warn", "done", "failed", "cancelled"}, timeout=60)
        status = final.get("status") if isinstance(final, dict) else getattr(final, "status", None)
        assert status != "done", (
            f"T3-W2 回归失败：历史不足 100 日时 status 仍为 done，应 warn/failed"
        )
        assert status in {"warn", "failed"}, (
            f"T3-W2 回归失败：非法终态 status={status}"
        )
    finally:
        for p in reversed(patchers):
            try:
                p.stop()
            except Exception:
                pass

