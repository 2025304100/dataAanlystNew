"""P0 修复自检：purge/embargo 单位归一化 + 显式样本量地板。

用 stub 替换 L1 依赖（factor_evaluator / contracts），
直接加载 staging 中的 evaluation_adapter.py 并验证：
  B9  月频 purge_points 必须为 1（而非 5）
  B10 周频 243 点在显式传参后可正常切分

运行：
  D:/ai_project/dataAanlystNew/.venv/Scripts/python.exe \
    docs/factor-mining-v2-skeleton/_selfcheck_p0.py
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import math
import pathlib
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent

# ── stub：app.services.factors.factor_evaluator ──────────────
fe = types.ModuleType("app.services.factors.factor_evaluator")


class TimeSplit:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def build_time_split(
    *,
    all_dates,
    target_horizon=5,
    train_ratio=0.6,
    validation_ratio=0.2,
    purge_days=5,
    embargo_days=5,
    min_validation_days=50,
    min_test_days=3,
    min_total_days=20,
):
    """factor_evaluator.build_time_split 的等价实现（口径见 factor_evaluator.py:259-297）。"""
    s = sorted(all_dates)
    n = len(s)
    tail = int(target_horizon)
    usable = max(0, n - tail)

    min_via_val = int(min_validation_days) + int(purge_days) + int(embargo_days)
    derived = max(
        int(min_total_days),
        int(math.ceil(min_via_val / float(validation_ratio))) if validation_ratio > 0 else 0,
    )

    te = max(0, min(n - 1, int(n * train_ratio)))
    ve = max(te, min(n - 1, int(n * (train_ratio + validation_ratio))))
    vs = min(n - 1, te + int(purge_days))
    ts = min(n - 1, ve + int(embargo_days))

    vraw = max(0, ve - vs)
    teraw = max(0, (n - 1) - ts + 1) if ts < n else 0
    ev = max(0, min(vraw, usable - vs))
    et = max(0, min(teraw, usable - ts)) if ts < n else 0

    if (
        n < int(min_total_days)
        or n < derived
        or ev < int(min_validation_days)
        or (teraw > 0 and et < int(min_test_days))
    ):
        raise ValueError(
            f"insufficient_dates actual={n} derived_min_total={derived} "
            f"(min_val={min_validation_days} purge={purge_days})"
        )
    return TimeSplit(
        train_start=s[0],
        train_end=s[max(0, te - 1)],
        validation_start=s[vs],
        validation_end=s[max(vs, ve - 1)],
        test_start=s[ts] if ts < n else None,
        test_end=s[-1] if ts < n else None,
        purge_days=purge_days,
        embargo_days=embargo_days,
        target_horizon=target_horizon,
    )


fe.TimeSplit = TimeSplit
fe.build_time_split = build_time_split

for name in ("app", "app.services", "app.services.factors", "app.services.factors.mining"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["app.services.factors.factor_evaluator"] = fe

# ── stub：contracts ──────────────────────────────────────────
ct = types.ModuleType("app.services.factors.mining.contracts")


class SplitBudget:
    def __init__(self, **kw):
        self.__dict__.update(kw)


ct.SplitBudget = SplitBudget
for _n in ("Fitness", "Individual", "MiningContext"):
    setattr(ct, _n, type(_n, (), {}))
sys.modules["app.services.factors.mining.contracts"] = ct

# ── 加载被测模块 ─────────────────────────────────────────────
spec = importlib.util.spec_from_file_location(
    "ea", HERE / "app/services/factors/mining/evaluation_adapter.py"
)
ea = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ea)


def main() -> int:
    failures: list[str] = []

    print("=== B9: purge 单位归一化（设计文档 §7.2.2） ===")
    for freq, expected in (("daily", 5), ("weekly", 1), ("monthly", 1)):
        got = ea.normalize_purge_points(target_horizon_days=5, frequency=freq)
        ok = got == expected
        print(f"  {freq:8s} purge_points={got}  期望 {expected}  {'OK' if ok else 'FAIL'}")
        if not ok:
            failures.append(f"B9_{freq}")

    print()
    print("=== B9b: 折算交易日数（UI 展示用，防误读） ===")
    for freq, points, expected_days in (("daily", 5, 5), ("weekly", 1, 5), ("monthly", 1, 21)):
        days = ea.normalize_purge_trading_days(purge_points=points, frequency=freq)
        ok = days == expected_days
        print(f"  {freq:8s} {points} 调仓点 = {days} 交易日  期望 {expected_days}  {'OK' if ok else 'FAIL'}")
        if not ok:
            failures.append(f"B9b_{freq}")

    print()
    print("=== B10: 显式传参后四种情形均可切分 ===")
    cases = [
        ("daily", 1215, 1, "5年"),
        ("weekly", 243, 7, "5年"),
        ("monthly", 60, 30, "5年"),
        ("monthly", 120, 30, "10年镜像"),
    ]
    expected_val_test = {
        ("daily", 1215): (238, 233),
        ("weekly", 243): (48, 43),
        ("monthly", 60): (11, 6),
        ("monthly", 120): (23, 18),
    }
    for freq, n, step_days, label in cases:
        dates = [dt.date(2021, 1, 1) + dt.timedelta(days=step_days * i) for i in range(n)]
        try:
            _split, budget = ea.build_split(
                all_dates=dates, frequency=freq, target_horizon=5
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  {freq:8s} n={n:5d} {label:10s} -> 抛异常 {exc}")
            failures.append(f"B10_{freq}_{n}")
            continue
        exp = expected_val_test[(freq, n)]
        ok = (budget.val_points, budget.test_points) == exp
        print(
            f"  {freq:8s} n={n:5d} {label:10s} train={budget.train_points:4d} "
            f"val={budget.val_points:3d} test={budget.test_points:3d} "
            f"| purge {budget.purge_points}点={budget.purge_trading_days}交易日 "
            f"| degraded={budget.statistically_degraded} | 期望 val/test={exp} {'OK' if ok else 'FAIL'}"
        )
        if not ok:
            failures.append(f"B10_expect_{freq}_{n}")

    print()
    print("=== 复现 P0：只做归一化、仍依赖默认 min_validation_days=50 ===")
    for freq, n, step_days in (("weekly", 243, 7), ("monthly", 60, 30), ("monthly", 120, 30)):
        dates = [dt.date(2021, 1, 1) + dt.timedelta(days=step_days * i) for i in range(n)]
        try:
            build_time_split(all_dates=dates, purge_days=1, embargo_days=1)
            print(f"  {freq:8s} n={n:5d}: 意外通过（说明 C12 非必需）")
            failures.append(f"P0_expected_fail_{freq}_{n}")
        except ValueError as exc:
            print(f"  {freq:8s} n={n:5d}: 仍抛异常 -> {str(exc)[:64]}")
            print("           => C12（显式传 min_*）确为必需，两处必须同时改")

    print()
    print("=== 月频统计降级判定 ===")
    dates = [dt.date(2021, 1, 1) + dt.timedelta(days=30 * i) for i in range(120)]
    _s, b10 = ea.build_split(all_dates=dates, frequency="monthly", target_horizon=5)
    print(f"  10 年镜像月频 test={b10.test_points} 点，需求 §4.1 门槛 24 -> 可达性={b10.test_points >= 24}")
    if b10.test_points >= 24:
        failures.append("R3_expected_unreachable")

    print()
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("ALL OK —— C11（归一化）与 C12（显式 min_*）缺一不可，均已验证。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
