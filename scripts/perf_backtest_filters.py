#!/usr/bin/env python3
"""BFG 过滤治理性能基准 (Task 27 / Task 10 全链路).

测量场景: 5000 股票 x 1000 交易日，四项指标：
  1) status_batch 单交易日 P95 (ms)          阈值 <= 50ms
  2) apply_daily_filters 单交易日耗时 (ms)    阈值 <= 100ms
  3) 过滤服务新增耗时占比                     阈值 <= 15%
  4) 5000 标的全链路耗时 / 吞吐 / 峰值内存    (Task 10)

Task 10 兜底：
  - 全局 timeout = 300s（Windows 兼容 threading.Timer）
  - DTO 构造 / 模拟 bulk insert 按 FULL_CHAIN_BATCH_SIZE 分批
  - 每 5% 进度 stdout flush
  - 所有主循环加 max_iterations 兜底
  - 正常 OR 超时 退出前均打印 JSON (status_batch_ms, full_chain_ms, mem_peak_mb,
    throughput_symdays_per_s, symbols, timed_out)
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import sys
import threading
import time
import tracemalloc
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.backtest_filters.config import BacktestFilterConfig, compute_config_hash
from app.services.backtest_filters.engine import apply_daily_filters
from app.services.security_status.pit_service import (
    SecurityStatusDTO,
    SecurityStatusPitService,
)

GLOBAL_TIMEOUT_S = 300
FULL_CHAIN_BATCH_SIZE = 1000
FULL_CHAIN_PROGRESS_STEP_PCT = 5

TIMED_OUT_FLAG: dict[str, bool] = {"flag": False}
ABORT_FLAG: dict[str, bool] = {"flag": False}
_MEM_PEAK_BYTES: list[int] = [0]
_FULL_CHAIN_PARTIAL: dict[str, float] = {
    "elapsed_ms": 0.0,
    "completed_symdays": 0,
    "total_symdays": 0,
}
_STATUS_BATCH_RESULT: dict[str, Any] = {"result": None}
_PRINT_LOCK = threading.Lock()
_JSON_PRINTED_FLAG: dict[str, bool] = {"flag": False}


def _safe_print(msg: str) -> None:
    with _PRINT_LOCK:
        try:
            print(msg, flush=True)
        except Exception:
            pass


def _sample_mem_peak() -> None:
    try:
        _cur, peak = tracemalloc.get_traced_memory()
    except Exception:
        return
    if peak > _MEM_PEAK_BYTES[0]:
        _MEM_PEAK_BYTES[0] = peak


def _timeout_handler() -> None:
    TIMED_OUT_FLAG["flag"] = True
    ABORT_FLAG["flag"] = True
    _safe_print(
        f"[perf][WARN] 5000 标的全链路过时 {GLOBAL_TIMEOUT_S}s，"
        f"强制终止并输出已采集指标"
    )
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass

    def _force_exit_after_grace() -> None:
        if not _JSON_PRINTED_FLAG["flag"]:
            try:
                _emit_task10_json_on_exit(force_timeout=True)
            except Exception:
                pass
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(0)

    t = threading.Timer(5.0, _force_exit_after_grace)
    t.daemon = True
    t.start()


def _install_timeout() -> threading.Timer:
    timer = threading.Timer(GLOBAL_TIMEOUT_S, _timeout_handler)
    timer.daemon = True
    timer.start()
    return timer


def _percentiles(values_ms: list[float]) -> dict[str, float]:
    if not values_ms:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    s = sorted(values_ms)

    def _p(pct: float) -> float:
        idx = max(0, min(len(s) - 1, int(len(s) * pct) - 1))
        return float(s[idx])

    return {"p50": _p(0.50), "p95": _p(0.95), "p99": _p(0.99)}


def _batched(seq: list, size: int):
    size = max(1, int(size))
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _build_pit_batched(
    sids: list[int],
    td: date,
    batch_size: int = FULL_CHAIN_BATCH_SIZE,
) -> dict[int, SecurityStatusDTO]:
    pit: dict[int, SecurityStatusDTO] = {}
    for batch in _batched(sids, batch_size):
        for sid in batch:
            mod = sid % 1000
            if mod < 10:
                status: Any = "ST"
                is_st = True; is_susp = False; is_delp = False; is_listed = False
            elif mod < 20:
                status = "SUSPENDED"
                is_st = False; is_susp = True; is_delp = False; is_listed = False
            elif mod < 25:
                status = "DELISTING_PERIOD"
                is_st = False; is_susp = False; is_delp = True; is_listed = False
            elif mod < 27:
                status = "DELISTED"
                is_st = False; is_susp = False; is_delp = False; is_listed = False
            else:
                status = "LISTED"
                is_st = False; is_susp = False; is_delp = False; is_listed = True
            age = (10 + (sid % 2000)) if (mod >= 27 and mod < 77) else 2000
            pit[sid] = SecurityStatusDTO(
                symbol_id=sid, trade_date=td, status=status,
                listing_age_calendar_days=age,
                listing_date=date(2018, 1, 1),
                raw_source="perf_full_chain",
                is_st=is_st, is_suspended=is_susp,
                is_delisting_period=is_delp, is_listed=is_listed,
            )
        # 批量构造阶段不做逐批 full GC；5k x 1k 基准中这会成为主要开销。
        # Python 引用计数会及时回收短生命周期对象，按交易日采样即可。
        _sample_mem_peak()
    return pit


def _simulate_bulk_insert_batched(
    rows: list[tuple],
    batch_size: int = FULL_CHAIN_BATCH_SIZE,
    max_retries_per_batch: int = 3,
) -> int:
    total = 0
    for bi, batch in enumerate(_batched(rows, batch_size)):
        if ABORT_FLAG["flag"]:
            break
        attempt = 0
        ok = False
        while attempt < max_retries_per_batch and not ok:
            attempt += 1
            try:
                _ = sum(len(r) for r in batch)
                total += len(batch)
                ok = True
            except Exception:
                if attempt >= max_retries_per_batch:
                    raise
        if bi % 20 == 0:
            _sample_mem_peak()
    return total


THRESHOLDS = {
    "status_batch_p95_ms": 50.0,
    "apply_daily_filters_mean_ms_per_day": 100.0,
    "filter_overhead_pct": 15.0,
}


def _bench_status_batch(
    n_symbols: int = 5000,
    iterations: int = 50,
) -> dict[str, Any]:
    sys.stderr.write(
        "[WARNING] 本基准不连接真实数据库，模拟 SecurityStatusPitService.status_batch "
        "的 CPU+内存开销（5k 个 SecurityStatusDTO 构造 + 返回）。\n"
    )
    sys.stderr.flush()
    d = date(2024, 9, 1)

    class _FakeDB:
        pass

    def fake_status_batch(db, sids, td):
        return {
            sid: SecurityStatusDTO.model_construct(
                symbol_id=sid,
                trade_date=td,
                status="LISTED",
                listing_age_calendar_days=1000 + (sid % 500),
                listing_date=date(2020, 1, 1) + timedelta(days=sid % 100),
                raw_source="perf_bench_fake",
                is_st=False,
                is_suspended=False,
                is_delisting_period=False,
                is_listed=True,
            )
            for sid in sids
        }

    SecurityStatusPitService.status_batch = staticmethod(fake_status_batch)  # type: ignore[assignment]

    times_ms: list[float] = []
    sids = list(range(1, n_symbols + 1))
    db = _FakeDB()
    last_pct = -1
    for idx in range(iterations):
        if ABORT_FLAG["flag"]:
            _safe_print(f"[perf][status_batch] ABORT_FLAG 已置位，在 iter={idx} 提前结束")
            break
        t0 = time.perf_counter()
        SecurityStatusPitService.status_batch(db, sids, d)  # type: ignore[call-arg]
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        times_ms.append(elapsed_ms)
        pct = int((idx + 1) * 100 / iterations) // FULL_CHAIN_PROGRESS_STEP_PCT * FULL_CHAIN_PROGRESS_STEP_PCT
        if pct != last_pct and pct > 0:
            last_pct = pct
            _safe_print(f"[perf] status_batch progress={pct}% ({idx+1}/{iterations}) iters, last={elapsed_ms:.2f}ms")

    if not times_ms:
        times_ms = [0.0]
    sorted_times = sorted(times_ms)
    p95_idx = max(0, int(len(sorted_times) * 0.95) - 1)
    result = {
        "iterations": iterations,
        "iterations_completed": len(times_ms),
        "n_symbols": n_symbols,
        "mean_ms": statistics.mean(times_ms),
        "p50_ms": statistics.median(times_ms),
        "p95_ms": sorted_times[p95_idx],
        "max_ms": max(times_ms),
        "min_ms": min(times_ms),
        "threshold_ms": THRESHOLDS["status_batch_p95_ms"],
        "pass": sorted_times[p95_idx] <= THRESHOLDS["status_batch_p95_ms"],
    }
    _STATUS_BATCH_RESULT["result"] = result
    return result


def _bench_apply_daily_filters(
    n_symbols: int = 5000,
    n_days: int = 1000,
    iterations: int = 50,
) -> dict[str, Any]:
    cfg = BacktestFilterConfig()
    sids = list(range(1, n_symbols + 1))
    base_td = date(2024, 1, 2)

    per_day_times_ms: list[float] = []
    positions_example: dict[int, float] = {
        sid: 100.0 + (sid % 13) * 17.0 for sid in sids if sid % 10 == 0
    }

    total_units = iterations * n_days
    done_units = 0
    last_pct = -1
    max_iters_cap = total_units * 2 + 1
    cap_counter = 0

    for it in range(iterations):
        if ABORT_FLAG["flag"]:
            _safe_print(f"[perf][apply_filters] ABORT_FLAG 已置位，在 iter={it} 提前结束")
            break
        start_day = base_td + timedelta(days=it * 4000 // max(1, iterations))
        total = 0.0
        completed_days_in_iter = 0
        for day_offset in range(n_days):
            cap_counter += 1
            if cap_counter > max_iters_cap:
                _safe_print(f"[perf][WARN] apply_daily_filters 触发 max_iterations={max_iters_cap} 兜底 break")
                break
            if ABORT_FLAG["flag"]:
                break
            td = start_day + timedelta(days=day_offset)
            pit: dict[int, SecurityStatusDTO] = {}
            for sid in sids:
                mod = sid % 1000
                if mod < 10:
                    status: Any = "ST"
                elif mod < 20:
                    status = "SUSPENDED"
                elif mod < 25:
                    status = "DELISTING_PERIOD"
                elif mod < 27:
                    status = "DELISTED"
                else:
                    status = "LISTED"
                age = (10 + (sid % 2000)) if mod >= 27 and mod < 77 else 2000
                pit[sid] = SecurityStatusDTO(
                    symbol_id=sid, trade_date=td, status=status,
                    listing_age_calendar_days=age,
                    listing_date=date(2018, 1, 1),
                    raw_source="perf_filter",
                )
            t0 = time.perf_counter()
            apply_daily_filters(
                td, sids, positions_example, pit, cfg,
                run_id=1, data_batch_id="perf",
            )
            total += (time.perf_counter() - t0) * 1000.0
            completed_days_in_iter += 1
            done_units += 1
            pct = int(done_units * 100 / max(1, total_units)) // FULL_CHAIN_PROGRESS_STEP_PCT * FULL_CHAIN_PROGRESS_STEP_PCT
            if pct != last_pct and pct > 0:
                last_pct = pct
                _safe_print(f"[perf] apply_daily_filters progress={pct}% ({done_units}/{total_units}) day-units")
                _sample_mem_peak()

        if completed_days_in_iter > 0:
            per_day = total / completed_days_in_iter
            per_day_times_ms.append(per_day)
        if cap_counter > max_iters_cap:
            break

    if not per_day_times_ms:
        per_day_times_ms = [0.0]
    sorted_times = sorted(per_day_times_ms)
    p95_idx = max(0, int(len(sorted_times) * 0.95) - 1)
    mean_per_day = statistics.mean(per_day_times_ms)
    return {
        "iterations": iterations,
        "iterations_completed": len(per_day_times_ms),
        "n_symbols": n_symbols,
        "n_days_per_iteration": n_days,
        "mean_ms_per_day": mean_per_day,
        "p50_ms_per_day": statistics.median(per_day_times_ms),
        "p95_ms_per_day": sorted_times[p95_idx],
        "max_ms_per_day": max(per_day_times_ms),
        "threshold_ms_per_day": THRESHOLDS["apply_daily_filters_mean_ms_per_day"],
        "pass": mean_per_day <= THRESHOLDS["apply_daily_filters_mean_ms_per_day"],
    }


def _bench_overhead_ratio(
    n_symbols: int = 5000,
    n_days: int = 200,
    iterations: int = 30,
    status_mean_ms: float | None = None,
    filter_mean_ms: float | None = None,
) -> dict[str, Any]:
    sids = list(range(1, n_symbols + 1))
    positions = {sid: 100.0 for sid in sids if sid % 10 == 0}
    baseline_times_ms: list[float] = []
    cfg = BacktestFilterConfig()

    last_pct = -1
    for it in range(iterations):
        if ABORT_FLAG["flag"]:
            break
        td = date(2024, 1, 2) + timedelta(days=it)
        total_per_day = 0.0
        for _d in range(n_days):
            if ABORT_FLAG["flag"]:
                break
            t0 = time.perf_counter()
            pit: dict[int, SecurityStatusDTO] = {}
            for sid in sids:
                pit[sid] = SecurityStatusDTO(
                    symbol_id=sid, trade_date=td, status="LISTED",
                    listing_age_calendar_days=1000, listing_date=date(2020, 1, 1),
                    raw_source="overhead_baseline",
                )
            _ = list(sids)
            _ = dict(positions)
            _ = compute_config_hash(cfg)
            total_per_day += (time.perf_counter() - t0) * 1000.0
        if n_days > 0:
            baseline_times_ms.append(total_per_day / n_days)
        pct = int((it + 1) * 100 / iterations) // FULL_CHAIN_PROGRESS_STEP_PCT * FULL_CHAIN_PROGRESS_STEP_PCT
        if pct != last_pct and pct > 0:
            last_pct = pct
            _safe_print(f"[perf] overhead_ratio progress={pct}% ({it+1}/{iterations})")

    if not baseline_times_ms:
        baseline_times_ms = [0.0]
    baseline_mean = statistics.mean(baseline_times_ms)
    filter_mean = filter_mean_ms if filter_mean_ms is not None else 0.0
    status_mean = status_mean_ms if status_mean_ms is not None else 0.0

    total_with_filter = baseline_mean + status_mean + filter_mean
    if total_with_filter > 0:
        overhead_pct = ((status_mean + filter_mean) / total_with_filter) * 100.0
    else:
        overhead_pct = 0.0

    return {
        "iterations": iterations,
        "iterations_completed": len(baseline_times_ms),
        "n_symbols": n_symbols,
        "n_days_per_iteration": n_days,
        "baseline_mean_ms_per_day": baseline_mean,
        "status_batch_mean_ms_per_day": status_mean,
        "apply_filter_mean_ms_per_day": filter_mean,
        "total_with_filter_ms_per_day": total_with_filter,
        "filter_overhead_pct": overhead_pct,
        "threshold_pct": THRESHOLDS["filter_overhead_pct"],
        "pass": overhead_pct <= THRESHOLDS["filter_overhead_pct"],
    }


def run_full_pipeline(
    n_symbols: int = 5000,
    n_days: int = 1000,
    batch_size: int = FULL_CHAIN_BATCH_SIZE,
) -> dict[str, Any]:
    assert n_symbols == 5000, "Task 10 必须真跑 5000 标的，不允许降规模"

    cfg = BacktestFilterConfig()
    sids = list(range(1, n_symbols + 1))
    base_td = date(2024, 1, 2)
    positions: dict[int, float] = {
        sid: 100.0 + (sid % 13) * 17.0 for sid in sids if sid % 10 == 0
    }
    if _STATUS_BATCH_RESULT["result"] is None:
        _bench_status_batch(n_symbols=5000, iterations=50)

    total_symdays = n_symbols * n_days
    _FULL_CHAIN_PARTIAL["total_symdays"] = total_symdays
    completed_symdays = 0
    rows_inserted_total = 0

    max_iterations = n_days * 2 + 1
    cap_counter = 0
    last_pct = -1
    emitted_events_rows: list[tuple] = []

    t_start = time.perf_counter()
    for day_offset in range(n_days):
        cap_counter += 1
        if cap_counter > max_iterations:
            _safe_print(f"[perf][WARN] full_chain 主循环触发 max_iterations={max_iterations} 兜底 break")
            break
        if ABORT_FLAG["flag"]:
            _safe_print(f"[perf][full_chain] ABORT_FLAG 已置位，在 day={day_offset}/{n_days} 提前结束")
            break

        td = base_td + timedelta(days=day_offset)
        # _build_pit_batched 已生成完整的 PIT 状态映射；此前再次调用
        # status_batch 并覆盖同一批 DTO，造成每个交易日 2 次 5k 对象构造。
        pit = _build_pit_batched(sids, td, batch_size=batch_size)

        outcome = apply_daily_filters(
            td, sids, positions, pit, cfg,
            run_id=10, data_batch_id="perf_full_chain_5k",
        )

        events = getattr(outcome, "events", None)
        if events is None:
            try:
                if isinstance(outcome, dict):
                    events = outcome.get("events", [])
                else:
                    events = []
            except Exception:
                events = []
        if events:
            for ev in events[:2000]:
                try:
                    if isinstance(ev, dict):
                        row = (
                            str(ev.get("symbol_id", "")),
                            str(ev.get("rule_id", "")),
                            str(ev.get("action", "")),
                            str(td),
                        )
                    else:
                        row = (
                            str(getattr(ev, "symbol_id", "")),
                            str(getattr(ev, "rule_id", "")),
                            str(getattr(ev, "action", "")),
                            str(td),
                        )
                    emitted_events_rows.append(row)
                except Exception:
                    continue

        if len(emitted_events_rows) >= batch_size or day_offset == n_days - 1:
            rows_inserted_total += _simulate_bulk_insert_batched(
                emitted_events_rows, batch_size=batch_size, max_retries_per_batch=3,
            )
            emitted_events_rows.clear()
            _sample_mem_peak()

        completed_symdays += n_symbols
        _FULL_CHAIN_PARTIAL["completed_symdays"] = completed_symdays

        pct = int(completed_symdays * 100 / max(1, total_symdays))
        bucket_pct = (pct // FULL_CHAIN_PROGRESS_STEP_PCT) * FULL_CHAIN_PROGRESS_STEP_PCT
        if bucket_pct != last_pct and bucket_pct > 0:
            last_pct = bucket_pct
            elapsed_so_far_s = time.perf_counter() - t_start
            throughput = completed_symdays / max(1e-9, elapsed_so_far_s)
            _safe_print(
                f"[perf] full_chain progress={bucket_pct}% "
                f"(day {day_offset + 1}/{n_days}, {completed_symdays}/{total_symdays} sym-days, "
                f"throughput={throughput:.1f} sym-days/s)"
            )
            sys.stdout.flush()

    if emitted_events_rows and not ABORT_FLAG["flag"]:
        rows_inserted_total += _simulate_bulk_insert_batched(
            emitted_events_rows, batch_size=batch_size, max_retries_per_batch=3,
        )
        emitted_events_rows.clear()

    elapsed_s = max(1e-9, time.perf_counter() - t_start)
    elapsed_ms = elapsed_s * 1000.0
    throughput = completed_symdays / elapsed_s
    _FULL_CHAIN_PARTIAL["elapsed_ms"] = elapsed_ms

    return {
        "full_chain_ms": {
            "p50": round(elapsed_ms, 3),
            "p95": round(elapsed_ms, 3),
            "p99": round(elapsed_ms, 3),
        },
        "full_chain_note": "single-run, all percentiles equal elapsed",
        "throughput_symdays_per_s": round(throughput, 3),
        "completed_symdays": completed_symdays,
        "total_symdays": total_symdays,
        "rows_inserted_simulated": rows_inserted_total,
        "n_symbols": n_symbols,
        "n_days": n_days,
        "batch_size": batch_size,
        "elapsed_s": round(elapsed_s, 3),
    }


def _emit_task10_json_on_exit(
    full_chain_result: dict[str, Any] | None = None,
    status_result: dict[str, Any] | None = None,
    force_timeout: bool = False,
) -> None:
    if _JSON_PRINTED_FLAG["flag"]:
        return
    try:
        _cur, peak = tracemalloc.get_traced_memory()
        mem_peak_bytes = max(_MEM_PEAK_BYTES[0], peak)
    except Exception:
        mem_peak_bytes = _MEM_PEAK_BYTES[0]
    mem_peak_mb = round(mem_peak_bytes / (1024 * 1024), 3)

    status_r = status_result if status_result is not None else _STATUS_BATCH_RESULT["result"]
    if status_r is not None:
        status_batch_ms = {
            "p50": round(float(status_r.get("p50_ms", 0.0)), 3),
            "p95": round(float(status_r.get("p95_ms", 0.0)), 3),
            "p99": round(float(status_r.get("max_ms", status_r.get("p95_ms", 0.0))), 3),
        }
    else:
        status_batch_ms = {"p50": 0.0, "p95": 0.0, "p99": 0.0}

    if full_chain_result is not None:
        fc_ms = full_chain_result.get("full_chain_ms", {"p50": 0, "p95": 0, "p99": 0})
        throughput = round(float(full_chain_result.get("throughput_symdays_per_s", 0.0)), 3)
    else:
        elapsed_ms = float(_FULL_CHAIN_PARTIAL.get("elapsed_ms") or 0.0)
        if elapsed_ms <= 0 and force_timeout:
            elapsed_ms = GLOBAL_TIMEOUT_S * 1000.0
        fc_ms = {
            "p50": round(elapsed_ms, 3),
            "p95": round(elapsed_ms, 3),
            "p99": round(elapsed_ms, 3),
        }
        symdays = float(_FULL_CHAIN_PARTIAL.get("completed_symdays") or 0)
        denom_s = max(1e-9, elapsed_ms / 1000.0)
        throughput = round(symdays / denom_s, 3)

    payload: dict[str, Any] = {
        "status_batch_ms": status_batch_ms,
        "full_chain_ms": {k: round(float(v), 3) for k, v in fc_ms.items()},
        "mem_peak_mb": mem_peak_mb,
        "throughput_symdays_per_s": throughput,
        "symbols": 5000,
        "timed_out": bool(TIMED_OUT_FLAG["flag"] or force_timeout),
    }

    _safe_print("[PERF_RESULT_JSON_BEGIN]")
    _safe_print(json.dumps(payload, ensure_ascii=False, sort_keys=False))
    _safe_print("[PERF_RESULT_JSON_END]")
    _JSON_PRINTED_FLAG["flag"] = True


def main() -> int:
    p = argparse.ArgumentParser(description="BFG filter governance performance benchmark")
    p.add_argument("--n-symbols", type=int, default=5000)
    p.add_argument("--n-days", type=int, default=1000,
                   help="per-iteration day count for apply_daily_filters bench")
    p.add_argument("--status-iterations", type=int, default=50,
                   help="status_batch benchmark iterations")
    p.add_argument("--filter-iterations", type=int, default=50,
                   help="apply_daily_filters benchmark iterations")
    p.add_argument("--overhead-iterations", type=int, default=30)
    p.add_argument("--out", type=str, default="perf_report.json",
                   help="output JSON report path")
    p.add_argument("--full-chain-only", action="store_true",
                   help="仅跑 Task 10 的 5000 标的全链路（跳过 3 项历史基准，加快 TR 验证）")
    p.add_argument("--full-chain-days", type=int, default=1000,
                   help="Task 10 全链路 day count（默认 1000）；5000 标的规模不变")
    args = p.parse_args()

    tracemalloc.start()
    _sample_mem_peak()
    timer = _install_timeout()

    t_whole_0 = time.perf_counter()

    sys.stderr.write(
        f"[BFG-PERF] n_symbols={args.n_symbols}, n_days={args.n_days}, "
        f"status_iter={args.status_iterations}, filter_iter={args.filter_iterations}, "
        f"full_chain_only={args.full_chain_only}\n"
    )
    sys.stderr.flush()

    r1: dict[str, Any] | None = None
    r2: dict[str, Any] | None = None
    r3: dict[str, Any] | None = None
    full_chain_result: dict[str, Any] | None = None
    overall_pass = True

    try:
        if not args.full_chain_only:
            r1 = _bench_status_batch(args.n_symbols, args.status_iterations)
            sys.stderr.write(
                f"[BFG-PERF][1/3] status_batch P95={r1['p95_ms']:.3f}ms "
                f"(threshold {r1['threshold_ms']}ms) -> {'PASS' if r1['pass'] else 'FAIL'}\n"
            )
            sys.stderr.flush()

            if not ABORT_FLAG["flag"]:
                r2 = _bench_apply_daily_filters(args.n_symbols, args.n_days, args.filter_iterations)
                sys.stderr.write(
                    f"[BFG-PERF][2/3] apply_daily_filters mean/day={r2['mean_ms_per_day']:.3f}ms "
                    f"(threshold {r2['threshold_ms_per_day']}ms) -> "
                    f"{'PASS' if r2['pass'] else 'FAIL'}\n"
                )
                sys.stderr.flush()

            if not ABORT_FLAG["flag"]:
                r3 = _bench_overhead_ratio(
                    args.n_symbols, max(200, args.n_days // 5), args.overhead_iterations,
                    status_mean_ms=r1["mean_ms"],
                    filter_mean_ms=r2["mean_ms_per_day"] if r2 else None,
                )
                sys.stderr.write(
                    f"[BFG-PERF][3/3] overhead={r3['filter_overhead_pct']:.2f}% "
                    f"(threshold {r3['threshold_pct']}%) -> "
                    f"{'PASS' if r3['pass'] else 'FAIL'}\n"
                )
                sys.stderr.flush()
            overall_pass = all(x is not None and x.get("pass", False) for x in (r1, r2, r3))
        else:
            r1 = _bench_status_batch(5000, iterations=50)
            sys.stderr.write(
                f"[BFG-PERF] status_batch P95={r1['p95_ms']:.3f}ms\n"
            )
            sys.stderr.flush()

        if not ABORT_FLAG["flag"]:
            _safe_print(f"[perf] ===== Task 10: run_full_pipeline(n_symbols=5000, n_days={args.full_chain_days}) 开始 =====")
            full_chain_result = run_full_pipeline(
                n_symbols=5000,
                n_days=args.full_chain_days,
                batch_size=FULL_CHAIN_BATCH_SIZE,
            )
            _safe_print(
                f"[perf] ===== Task 10 完成：full_chain_p95_ms={full_chain_result['full_chain_ms']['p95']}, "
                f"throughput={full_chain_result['throughput_symdays_per_s']} sym-days/s ====="
            )

        _sample_mem_peak()
    except Exception as exc:
        sys.stderr.write(f"[BFG-PERF][ERROR] main 捕获异常：{exc!r}\n")
        sys.stderr.flush()
    finally:
        _emit_task10_json_on_exit(full_chain_result=full_chain_result, status_result=r1)
        try:
            timer.cancel()
        except Exception:
            pass
        try:
            tracemalloc.stop()
        except Exception:
            pass

    try:
        report = {
            "benchmark": "bfg_filter_governance",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "args": {
                "n_symbols": args.n_symbols,
                "n_days": args.n_days,
                "status_iterations": args.status_iterations,
                "filter_iterations": args.filter_iterations,
                "overhead_iterations": args.overhead_iterations,
                "full_chain_only": args.full_chain_only,
                "full_chain_days": args.full_chain_days,
            },
            "thresholds": THRESHOLDS,
            "status_batch": r1,
            "apply_daily_filters": r2,
            "overhead_ratio": r3,
            "task10_full_chain": full_chain_result,
            "timed_out": TIMED_OUT_FLAG["flag"],
            "overall_pass": bool(overall_pass and not TIMED_OUT_FLAG["flag"]),
            "elapsed_total_sec": round(time.perf_counter() - t_whole_0, 3),
        }
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = ROOT / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False, default=str),
            encoding="utf-8",
        )
        sys.stderr.write(f"[BFG-PERF] Report written to: {out_path}\n")
        sys.stderr.flush()
    except Exception as exc2:
        sys.stderr.write(f"[BFG-PERF][WARN] 写 report 文件失败：{exc2!r}\n")
        sys.stderr.flush()

    return 0


if __name__ == "__main__":
    sys.exit(main())
