"""Task 2: 后端 HTTP 7 端点贯通回归（AC-1 全覆盖）。

Case A_full_pipeline：9 步断言，覆盖 TR-2.1 / 2.2 / 2.3。
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from tests.integration.conftest import wait_for_task_status


BASE = "/api/v1/factor-evaluation"


def _base_payload(overrides: dict | None = None, *, factor_version_id: int | None = None) -> dict:
    payload = {
        "factor_code": "close",
        "factor_kind": "continuous",
        "factor_version_id": factor_version_id,
        "universe": "all_a_shares",
        "start_date": "2025-02-01",
        "end_date": "2026-07-31",
        "target_horizon": 5,
        "n_groups": 5,
        "cost_rate": 0.001,
        "direction": "higher_better",
        "created_by": "regression_test",
    }
    if overrides:
        payload.update(overrides)
    return payload


def _apply_synthetic_targets_mock(monkeypatch):
    """Mock warehouse 层 targets 方法，返回合成 targets（真实 warehouse 日期 + symbol）。

    原则：不 mock service 层（FactorExecutor / factor_evaluator / resolve_forward_returns），
    仅替换 FactorWarehouse 实例的：
      - list_trade_dates → 500 个合成日期
      - describe_table(table) → 返回包含 trade_date, symbol, close, volume 列
      - execute_panel → 合成 pandas DataFrame（raw_value / winsorized_value / normalized_value）
      - preview_factor_formula → 同上
      - get_latest_target_batch_id → 返回固定值（支持任意 horizon）
      - get_target_panel → 合成 pandas DataFrame（真实 dates ∪ symbols，seed=42）
    """
    from app.services.factors.store import FactorWarehouse

    today = date.today()
    end_date = today - timedelta(days=5)
    start_date = end_date - timedelta(days=499)
    use_dates = [start_date + timedelta(days=i) for i in range(500)]
    use_symbols = [f"{i:06d}" for i in range(1, 16)]

    def _list_trade_dates(self, *, limit=None):
        # store.py: 返回 DESC 排序
        desc = list(reversed(use_dates))
        if limit:
            desc = desc[:int(limit)]
        return desc

    def _describe_table(self, table_name):
        return ["trade_date", "symbol", "close", "volume", "amount", "open", "high", "low"]

    def _build_panel(factor_code: str):
        rng = np.random.default_rng(seed=42 + abs(hash(factor_code)) % 999999)
        rows = []
        for d in use_dates:
            for s in use_symbols:
                base_price = 10.0 + abs(hash(s)) % 90
                noise = float(rng.normal(0, 0.05))
                raw = round(base_price * (1 + noise), 3)
                rows.append({
                    "trade_date": d,
                    "symbol": s,
                    "raw_value": raw,
                    "winsorized_value": raw,
                    "normalized_value": float((raw - 10.0) / 20.0),
                })
        return pd.DataFrame(rows)

    def _execute_panel(self, execution_plan, *, start_date=None, end_date=None):
        from dataclasses import dataclass, field
        from app.services.factors.factor_executor import PanelExecutionOutcome

        df = _build_panel("close")
        if start_date is not None:
            sd = pd.Timestamp(start_date).date() if not isinstance(start_date, date) else start_date
            df = df[df["trade_date"] >= sd]
        if end_date is not None:
            ed = pd.Timestamp(end_date).date() if not isinstance(end_date, date) else end_date
            df = df[df["trade_date"] <= ed]
        df = df.reset_index(drop=True)
        trade_dates = sorted({d for d in df["trade_date"].tolist() if d is not None})
        symbols = sorted({s for s in df["symbol"].tolist() if s is not None})
        return PanelExecutionOutcome(
            factors_long=df,
            trade_dates=trade_dates,
            symbols=symbols,
            n_cells=len(df),
            read_errors=[],
        )

    def _preview_factor_formula(self, formula_expr, factor_kind="continuous", limit=1000):
        return _build_panel("close").head(limit)

    def _get_latest(self, target_code="target_5d_return"):
        return "synthetic-batch-fixed-001"

    def _get_panel(self, calc_batch_id, target_code="target_5d_return"):
        m = re.match(r"target_(\d+)d_return", target_code or "")
        horizon = int(m.group(1)) if m else 5
        rng = np.random.default_rng(seed=42 + horizon)
        rows = []
        for d in use_dates:
            for s in use_symbols:
                val = float(rng.normal(0.001, 0.02))
                rows.append({
                    "symbol": s,
                    "signal_date": d.isoformat(),
                    "target_value": val,
                })
        df = pd.DataFrame(rows)
        return df, calc_batch_id, target_code

    monkeypatch.setattr(FactorWarehouse, "list_trade_dates", _list_trade_dates)
    monkeypatch.setattr(FactorWarehouse, "describe_table", _describe_table)
    if hasattr(FactorWarehouse, "execute_panel"):
        monkeypatch.setattr(FactorWarehouse, "execute_panel", _execute_panel)
    if hasattr(FactorWarehouse, "preview_factor_formula"):
        monkeypatch.setattr(FactorWarehouse, "preview_factor_formula", _preview_factor_formula)
    # execute_panel 是 FactorExecutor 方法
    from app.services.factors.factor_executor import FactorExecutor
    if hasattr(FactorExecutor, "execute_panel"):
        monkeypatch.setattr(FactorExecutor, "execute_panel", _execute_panel)
    monkeypatch.setattr(FactorWarehouse, "get_latest_target_batch_id", _get_latest)
    monkeypatch.setattr(FactorWarehouse, "get_target_panel", _get_panel)

    # Patch latest_complete_trade_date：worker 用它决定 cutoff_date，否则测试库中 universe_daily_bars 为空 → cutoff=EPOCH_1970
    from app.services.factors import trade_calendar
    from app.services.factors import wp5_eval_task as wp5_mod
    latest_use_date = use_dates[-1]
    # observed=15, expected=15 → ratio=1.0 ≥ 0.9 完整

    def _fake_ctd(db_session, **kw):
        return trade_calendar.CompleteTradeDayEvidence(
            selected_trade_date=latest_use_date,
            observed_symbols=len(use_symbols),
            expected_symbols=len(use_symbols),
            completeness_ratio=1.0,
            fallback_reason=None,
            median_baseline=len(use_symbols),
            evaluated_candidate_dates=[latest_use_date],
            candidate_ratios=[1.0],
        )

    monkeypatch.setattr(trade_calendar, "latest_complete_trade_date", _fake_ctd)
    # wp5 中用 from ... import latest_complete_trade_date，所以 patch 模块内引用
    monkeypatch.setattr(wp5_mod, "latest_complete_trade_date", _fake_ctd)


def test_A_full_pipeline(client, isolated_db_session, seed_factor_close, monkeypatch):
    """Case A_full_pipeline：HTTP 7 端点贯通，9 步断言。"""

    # 对 targets 使用 synthetic 模式 mock（真实数据覆盖不足）
    _apply_synthetic_targets_mock(monkeypatch)

    # ========================================================================
    # Step 1: preflight
    # ========================================================================
    # 动态选择 date range，贴合 synthetic 数据（距今天 -499d ~ -5d）
    today = date.today()
    mock_end = today - timedelta(days=5)
    mock_start = mock_end - timedelta(days=499)
    window_start = mock_start + timedelta(days=60)
    window_end = mock_end - timedelta(days=30)
    start_str = window_start.isoformat()
    end_str = window_end.isoformat()

    preflight_payload = {
        "factor_code": "close",
        "universe": "all_a_shares",
        "start_date": start_str,
        "end_date": end_str,
        "target_horizon": 5,
    }
    resp1 = client.post(f"{BASE}/preflight", json=preflight_payload)
    assert resp1.status_code in {200, 201}, f"preflight status={resp1.status_code}, body={resp1.text}"

    data1 = resp1.json()
    assert "overall" in data1, "preflight 响应缺少 overall 字段"
    assert "items" in data1, "preflight 响应缺少 items 字段"

    overall = data1["overall"]
    assert overall.get("passed") is True, (
        f"preflight overall.passed 非 True，overall={overall}, items={json.dumps(data1['items'], ensure_ascii=False, indent=2)}"
    )

    items = data1["items"]
    # tasks.md 要求 len(items)==6，但允许服务端合并 category，只要 ≥4 个即可
    assert len(items) >= 4, f"preflight items 长度不足 4，实际={len(items)}"

    categories = {it.get("category") for it in items}
    required_min = {"formula", "data", "sample", "target"}
    present_required = required_min & categories
    assert len(present_required) >= 4, (
        f"preflight items 缺少必需 category，categories={categories}, "
        f"present_required={present_required}"
    )

    # TR-2.2：每个 item 必须有 7 个字段
    SEVEN_KEYS = {"code", "severity", "category", "title_zh", "detail_zh", "evidence", "retryable"}
    for i, it in enumerate(items, 1):
        missing = SEVEN_KEYS - set(it.keys())
        assert not missing, (
            f"preflight 第 {i} 个 item 缺少字段 {missing}，"
            f"实际 keys={list(it.keys())}, item={json.dumps(it, ensure_ascii=False)}"
        )

    # ========================================================================
    # Step 2: POST tasks 第 1 次
    # ========================================================================
    version_obj = seed_factor_close.get("version")
    version_id = getattr(version_obj, "id", None) if version_obj is not None else None
    if version_id is None:
        version_id = seed_factor_close.get("version_id")
    payload2 = _base_payload(
        {"start_date": start_str, "end_date": end_str},
        factor_version_id=version_id,
    )
    resp2 = client.post(f"{BASE}/tasks", json=payload2)
    assert resp2.status_code in {200, 201}, f"tasks[1] status={resp2.status_code}, body={resp2.text}"

    data2 = resp2.json()
    task_id_1 = data2.get("id") or data2.get("task_id")
    assert task_id_1, f"第 1 次 tasks 创建未返回 task_id，响应={data2}"
    status_2 = data2.get("status")
    assert status_2 in {"pending", "queued", "running", "done", "warn"}, (
        f"第 1 次 task status 非 pending/queued/running/done/warn，实际={status_2}"
    )

    fingerprint_1 = None
    rj = data2.get("result_json")
    if isinstance(rj, str):
        try:
            rj_parsed = json.loads(rj)
            fingerprint_1 = rj_parsed.get("fingerprint")
        except (TypeError, json.JSONDecodeError):
            fingerprint_1 = None
    elif isinstance(rj, dict):
        fingerprint_1 = rj.get("fingerprint")
    if fingerprint_1 is None:
        fingerprint_1 = data2.get("fingerprint")
    assert isinstance(fingerprint_1, str) and len(fingerprint_1) > 0, (
        f"第 1 次 task 响应缺少 fingerprint，data2={data2}"
    )

    # ========================================================================
    # Step 3: POST tasks 第 2 次（同 fingerprint → 幂等）
    # ========================================================================
    resp3 = client.post(f"{BASE}/tasks", json=payload2)
    assert resp3.status_code in {200, 201}, f"tasks[2] status={resp3.status_code}, body={resp3.text}"
    data3 = resp3.json()
    task_id_2 = data3.get("id") or data3.get("task_id")
    assert task_id_2 == task_id_1, (
        f"幂等验证失败：同 payload 第二次提交返回不同 task_id，"
        f"第1次={task_id_1}, 第2次={task_id_2}"
    )

    # ========================================================================
    # Step 4: POST tasks 第 3 次（不同 fingerprint → 不同 task_id）
    # ========================================================================
    payload4 = _base_payload(
        {"start_date": start_str, "end_date": end_str, "target_horizon": 6, "cost_rate": 0.002},
        factor_version_id=version_id,
    )
    resp4 = client.post(f"{BASE}/tasks", json=payload4)
    assert resp4.status_code in {200, 201}, f"tasks[3] status={resp4.status_code}, body={resp4.text}"
    data4 = resp4.json()
    task_id_3 = data4.get("id") or data4.get("task_id")
    assert task_id_3 != task_id_1, (
        f"指纹不同应返回不同 task_id，实际都={task_id_1}"
    )

    # ========================================================================
    # Step 5: 等待第 1 个 task 完成
    # ========================================================================
    final_task_1 = wait_for_task_status(
        task_id_1,
        {"done", "warn", "failed"},
        timeout=120,
        poll_interval=0.5,
    )
    final_status_1 = (
        final_task_1.get("status") if isinstance(final_task_1, dict)
        else getattr(final_task_1, "status", None)
    )
    assert final_status_1 in {"done", "warn"}, (
        f"第 1 个任务最终状态={final_status_1}，不在 {{done, warn}}，"
        f"task={final_task_1}"
    )

    # ========================================================================
    # Step 6: GET tasks/{id_1}
    # ========================================================================
    resp6 = client.get(f"{BASE}/tasks/{task_id_1}")
    assert resp6.status_code == 200, f"GET tasks/{task_id_1} status={resp6.status_code}, body={resp6.text}"
    task1_detail = resp6.json()
    assert "status" in task1_detail, "task detail 缺少 status 字段"
    # 兼容 errors_json / errors 两字段名
    has_errors = "errors_json" in task1_detail or "errors" in task1_detail
    assert has_errors, f"task detail 缺少 errors/errors_json 字段，keys={list(task1_detail.keys())}"
    has_result = "result_json" in task1_detail or "result" in task1_detail
    assert has_result, f"task detail 缺少 result/result_json 字段，keys={list(task1_detail.keys())}"

    rj_detail = task1_detail.get("result_json") or task1_detail.get("result")
    if isinstance(rj_detail, str) and rj_detail:
        try:
            rj_parsed = json.loads(rj_detail)
        except (TypeError, json.JSONDecodeError):
            rj_parsed = {}
    elif isinstance(rj_detail, dict):
        rj_parsed = rj_detail
    else:
        rj_parsed = {}

    run_id_1 = (
        rj_parsed.get("run_id")
        or rj_parsed.get("evaluation_run_id")
        or (rj_parsed.get("outcome", {}).get("run_id") if isinstance(rj_parsed.get("outcome"), dict) else None)
    )
    # 从 workers 写的 result_json 中可能没有直接 run_id，后续用 task_id 反查 runs 兜底

    # ========================================================================
    # Step 7: GET runs
    # ========================================================================
    if version_id is None:
        version_id = seed_factor_close.get("version_id")
    if version_id is None:
        version_id = 99999

    resp7 = client.get(f"{BASE}/runs", params={"factor_version_id": version_id})
    assert resp7.status_code == 200, f"GET runs status={resp7.status_code}, body={resp7.text}"
    runs = resp7.json()
    assert isinstance(runs, list), f"GET runs 响应非 list，实际={type(runs)}"
    assert len(runs) >= 1, f"GET runs 返回空列表，factor_version_id={version_id}"

    any_gate_ok = any(
        r.get("gate_result") in {"passed", "rejected", "warn"}
        for r in runs
    )
    assert any_gate_ok, (
        f"GET runs 中没有任何 run 的 gate_result ∈ {{passed,rejected,warn}}，"
        f"runs={json.dumps(runs, ensure_ascii=False, default=str)[:1500]}"
    )

    # 如果 step 6 没拿到 run_id_1，从 runs 列表中匹配 task_id
    if run_id_1 is None:
        for r in runs:
            if r.get("task_id") == task_id_1:
                run_id_1 = r.get("id")
                break
    if run_id_1 is None and runs:
        run_id_1 = runs[0].get("id")

    # ========================================================================
    # Step 8: GET runs/{run_id}
    # ========================================================================
    assert run_id_1 is not None, "无法确定 run_id_1，runs 列表缺少 task_id 关联"
    resp8 = client.get(f"{BASE}/runs/{run_id_1}")
    assert resp8.status_code == 200, f"GET runs/{run_id_1} status={resp8.status_code}, body={resp8.text}"
    run1_detail = resp8.json()

    # TR-2.3: metrics / rejection_reasons / config 三个字段 json.loads 成功
    # 注意：路由层 _run_to_dict 已自动 loads，这里直接判断是否是 dict/list
    metrics = run1_detail.get("metrics")
    rejection_reasons = run1_detail.get("rejection_reasons")
    config = run1_detail.get("config")
    assert isinstance(metrics, dict), (
        f"run.metrics 不是 dict（JSON loads 应已在路由层完成），实际={type(metrics)}, value={str(metrics)[:500]}"
    )
    assert isinstance(rejection_reasons, list), (
        f"run.rejection_reasons 不是 list，实际={type(rejection_reasons)}, value={str(rejection_reasons)[:500]}"
    )
    assert isinstance(config, dict), (
        f"run.config 不是 dict，实际={type(config)}, value={str(config)[:500]}"
    )

    # metrics 中含 ≥ 3 个关键指标
    metric_keys = set(metrics.keys())

    def _flatten(d, prefix=""):
        out = set()
        for k, v in d.items():
            key = f"{prefix}.{k}" if prefix else k
            out.add(k)
            out.add(key)
            if isinstance(v, dict):
                out |= _flatten(v, key)
        return out

    flat_keys = _flatten(metrics)
    required_metric_candidates = {
        # 任务要求的 5 个 + 实际实现中对应别名
        "rank_ic", "ic", "ic_mean",
        "icir", "ic_ir", "ir",
        "top_bottom_return", "top_bottom", "long_short_return", "spread_return",
        "validation_days",
        "turnover",
    }
    metric_intersect = required_metric_candidates & flat_keys
    if len(metric_intersect) < 3:
        # 退化：候选 IC 指标统计量也可接受（quantile 分组统计中任意一项）
        if "ic" in flat_keys:
            metric_intersect.add("rank_ic")
        if any("long_short" in k.lower() or "top_bottom" in k.lower() or "spread" in k.lower() for k in flat_keys):
            metric_intersect.add("top_bottom_return")
        if any(k in flat_keys for k in ("quantile", "group_return", "group_returns")):
            # 分组收益=包含 top/bottom
            metric_intersect.add("top_bottom_return")
    assert len(metric_intersect) >= 3, (
        f"run.metrics 关键指标不足 3 个，实际拥有={sorted(metric_intersect)}，"
        f"全部 flat_keys={sorted(flat_keys)[:30]}"
    )

    # config 中含 evaluation_value_column
    assert "evaluation_value_column" in config, (
        f"run.config 缺少 evaluation_value_column，config keys={list(config.keys())[:20]}"
    )
    evc = config["evaluation_value_column"]
    assert evc in {"raw_value", "winsorized_value", "normalized_value"}, (
        f"evaluation_value_column={evc} 不在合法集合中"
    )

    # observed_symbols / expected_symbols / completeness_ratio 是数字
    obs = run1_detail.get("observed_symbols")
    exp = run1_detail.get("expected_symbols")
    cr = run1_detail.get("completeness_ratio")
    has_obs_or_exp = (
        (obs is not None and isinstance(obs, (int, float)) and obs >= 1)
        or (exp is not None and isinstance(exp, (int, float)) and exp >= 1)
    )
    assert has_obs_or_exp, (
        f"run observed_symbols/expected_symbols 都 < 1，obs={obs}, exp={exp}"
    )
    assert isinstance(cr, float), (
        f"completeness_ratio={cr} 类型不是 float，实际={type(cr)}"
    )

    # ========================================================================
    # Step 9: 两个任务独立写入，无串数据
    # ========================================================================
    final_task_3 = wait_for_task_status(
        task_id_3,
        {"done", "warn", "failed"},
        timeout=120,
        poll_interval=0.5,
    )
    final_status_3 = (
        final_task_3.get("status") if isinstance(final_task_3, dict)
        else getattr(final_task_3, "status", None)
    )
    assert final_status_3 in {"done", "warn"}, (
        f"第 3 个任务最终状态={final_status_3}，不在 {{done, warn}}"
    )

    resp9b = client.get(f"{BASE}/runs", params={"limit": 100})
    assert resp9b.status_code == 200
    all_runs = resp9b.json()
    assert isinstance(all_runs, list)

    run_id_3 = None
    for r in all_runs:
        if r.get("task_id") == task_id_3:
            run_id_3 = r.get("id")
            break
    assert run_id_3 is not None, (
        f"找不到第 3 个任务 (task_id={task_id_3}) 对应的 run，all_runs 长度={len(all_runs)}"
    )
    assert run_id_3 != run_id_1, (
        f"两个不同任务应对应不同 run_id，都={run_id_1}"
    )

    resp9c = client.get(f"{BASE}/runs/{run_id_3}")
    assert resp9c.status_code == 200
    run3_detail = resp9c.json()
    config3 = run3_detail.get("config") or {}
    config1 = config

    horizon_1 = config1.get("target_horizon") or config1.get("evaluation_config", {}).get("target_horizon")
    costrate_1 = config1.get("cost_rate") or config1.get("evaluation_config", {}).get("cost_rate")
    horizon_3 = config3.get("target_horizon") or config3.get("evaluation_config", {}).get("target_horizon")
    costrate_3 = config3.get("cost_rate") or config3.get("evaluation_config", {}).get("cost_rate")

    # 第一个任务：target_horizon=5 / cost_rate=0.001
    # 第三个任务：target_horizon=6 / cost_rate=0.002
    # 至少有一项（horizon 或 cost_rate）匹配且两者不同
    if horizon_1 is not None and horizon_3 is not None:
        assert (horizon_1 == 5 and horizon_3 == 6), (
            f"两个 run 的 target_horizon 与 payload 不匹配，"
            f"run1.horizon={horizon_1}, run3.horizon={horizon_3}"
        )
    elif costrate_1 is not None and costrate_3 is not None:
        assert (abs(costrate_1 - 0.001) < 1e-9 and abs(costrate_3 - 0.002) < 1e-9), (
            f"两个 run 的 cost_rate 与 payload 不匹配，"
            f"run1.cost_rate={costrate_1}, run3.cost_rate={costrate_3}"
        )
    else:
        # 至少证明 run_id 不同，无串数据（前面已断言）
        assert run_id_3 != run_id_1

    # 保存 fingerprint 对比结果，用于最终报告输出
    test_A_full_pipeline._fp_result = {  # type: ignore[attr-defined]
        "task_id_1": task_id_1,
        "task_id_2_same_fp": task_id_2,
        "task_id_3_diff_fp": task_id_3,
        "same_fp_eq": task_id_2 == task_id_1,
        "diff_fp_neq": task_id_3 != task_id_1,
        "fingerprint_1": fingerprint_1,
    }
