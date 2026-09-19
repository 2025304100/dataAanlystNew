"""T02 · 切分归一化哨兵单测（防泄漏）

阶段 PH0 ｜ 门禁 G0 ｜ 模块 M10（`app/services/factors/mining/evaluation_adapter.py`）

**本文件是「哨兵」，不是普通单测。** 它守住 v2.0 的两个 P0 修复：

  C11  purge/embargo 单位归一化 —— `build_time_split` 把 `purge_days` 当**传入
       `all_dates` 的索引间隔**。挖掘传入的是「有效调仓点」，故必须折算：
       daily 5 / weekly 1 / monthly 1。
  C12  显式传入 `min_*` —— 默认 `min_validation_days=50` 推出 `derived_min_total=260`，
       使周频 243 点、月频 60/120 点**全部 hard_fail 抛异常**。两处必须同时生效。

跑法（DoD）：

    .venv/Scripts/python.exe -m pytest \
        tests/services/factors/mining/test_evaluation_adapter_split.py -q

**三条纪律**（违反即视为破坏哨兵）：

1. 只用**真实** `factor_evaluator.build_time_split`，不得 stub / monkeypatch 它。
   一旦 stub，C11/C12 是否真的成立就无从验证 —— 那正是本文件存在的理由。
2. 不得为了让断言变绿而修改 `evaluation_adapter.py` 的常量。常量是冻结契约；
   断言失败说明 T01 搬错文件或上游被改，应回去修上游。
3. 表驱动的数值取自本机实测（2026-09-16，`.venv` Python）。改动断言前先跑一遍
   真实代码，不要凭推算改数。
"""
from __future__ import annotations

import datetime as dt
import math

import pytest

from app.services.factors.factor_evaluator import build_time_split
from app.services.factors.mining.evaluation_adapter import (
    DEGRADED_TEST_THRESHOLD,
    REBALANCE_INTERVAL_DAYS,
    SPLIT_ALGORITHM_VERSION,
    SPLIT_MINIMUMS,
    MiningSplitError,
    assert_no_leakage,
    build_split,
    compute_split_budget,
    normalize_purge_points,
    normalize_purge_trading_days,
)

pytestmark = pytest.mark.whitebox

# ══════════════════════════════════════════════════════════
# 样本构造
# ══════════════════════════════════════════════════════════

#: 每个调仓期在测试数据里跨越的日历天数（与 REBALANCE_INTERVAL_DAYS 同量级即可，
#: 因为切分只按索引计数，与日期实际跨度无关 —— 这本身就是 C11 的根因）
STEP_DAYS: dict[str, int] = {"daily": 1, "weekly": 7, "monthly": 30}

TARGET_HORIZON = 5

#: 需求 §2.1 的样本规模口径
N_DAILY_5Y = 1_215
N_WEEKLY_5Y = 243
N_MONTHLY_5Y = 60
N_MONTHLY_10Y = 120  # 10 年镜像后的月频点数


def _series(n: int, *, frequency: str) -> list[dt.date]:
    """构造 n 个等间隔「有效调仓点」。"""
    step = STEP_DAYS[frequency]
    return [dt.date(2021, 1, 1) + dt.timedelta(days=step * i) for i in range(n)]


def _case(frequency: str, n: int) -> tuple[object, object]:
    return build_split(
        all_dates=_series(n, frequency=frequency),
        frequency=frequency,
        target_horizon=TARGET_HORIZON,
    )


# (frequency, n) 标识一组的实测口径 → 见各测试的期望值
CASES_MAIN = [
    ("daily", N_DAILY_5Y),
    ("weekly", N_WEEKLY_5Y),
    ("monthly", N_MONTHLY_5Y),
    ("monthly", N_MONTHLY_10Y),
]


# ══════════════════════════════════════════════════════════
# A. 常量冻结 —— 防止后续 Agent 改常量让测试变绿
# ══════════════════════════════════════════════════════════


def test_rebalance_interval_days_frozen():
    """每期含交易日数：归一化的唯一依据，写死即契约。"""
    assert REBALANCE_INTERVAL_DAYS == {"daily": 1, "weekly": 5, "monthly": 21}


def test_split_minimums_frozen():
    """(min_total, min_val, min_test) 三元组冻结（**D-I 裁决后的新契约**）。

    自洽性由 `test_split_minimums_self_consistent` 与
    `test_split_floor_sweep_no_dead_zone` 守护（derived ≤ 声明地板，无死区）。
    """
    assert SPLIT_MINIMUMS == {
        "daily": (252, 40, 40),
        "weekly": (104, 18, 10),
        "monthly": (41, 5, 3),
    }


def test_split_algorithm_version_frozen():
    """版本号会随快照落库；改了参数必须同步改它，否则历史任务无法复现。"""
    assert SPLIT_ALGORITHM_VERSION == "split-1.0.0"


def test_degraded_test_threshold_frozen():
    """月频不做 Bootstrap/置换的分界（需求 §4.1 的 24 点；数值本身是待决 Q3）。"""
    assert DEGRADED_TEST_THRESHOLD == 24


# ══════════════════════════════════════════════════════════
# B. normalize_* —— C11 的纯计算部分
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "frequency,expected",
    [("daily", 5), ("weekly", 1), ("monthly", 1)],
)
def test_normalize_purge_points_maps_five_trading_days(frequency, expected):
    """5 个交易日持有期折算为调仓点数：daily 5 / weekly 1 / monthly 1。

    这是 C11 的核心：写死的 5 在周频意味着「隔离 5 周」、月频「隔离 5 个月」，
    与「阻断 T+5 标签跨界」的本意相反。
    """
    assert normalize_purge_points(target_horizon_days=5, frequency=frequency) == expected


@pytest.mark.parametrize(
    "frequency,purge_points,expected_trading_days",
    [("daily", 5, 5), ("weekly", 1, 5), ("monthly", 1, 21)],
)
def test_normalize_purge_trading_days_折回交易日(frequency, purge_points, expected_trading_days):
    """供 Step2 UI 展示用：必须回传折算后的**交易日数**，避免用户误读为「5 个交易日」。"""
    assert (
        normalize_purge_trading_days(purge_points=purge_points, frequency=frequency)
        == expected_trading_days
    )


@pytest.mark.parametrize("frequency", ["daily", "weekly", "monthly"])
def test_normalize_purge_points_never_returns_zero(frequency):
    """隔离期最少 1 期：返回 0 等于没有隔离，标签会跨界。"""
    assert normalize_purge_points(target_horizon_days=0, frequency=frequency) >= 1
    assert normalize_purge_points(target_horizon_days=1, frequency=frequency) >= 1


def test_normalize_purge_points_rounds_up():
    """向上取整：宁可多隔离，不可少隔离。"""
    assert normalize_purge_points(target_horizon_days=21, frequency="monthly") == 1
    assert normalize_purge_points(target_horizon_days=22, frequency="monthly") == 2
    assert normalize_purge_points(target_horizon_days=100, frequency="monthly") == 5


def test_normalize_purge_points_rejects_unknown_frequency():
    """未支持的频率必须显式报错，不能静默按 daily 处理。"""
    with pytest.raises(ValueError):
        normalize_purge_points(target_horizon_days=5, frequency="quarterly")


@pytest.mark.parametrize("frequency", ["daily", "weekly", "monthly"])
def test_normalized_window_covers_target_horizon_in_trading_days(frequency):
    """归一化的目的：折算回交易日必须 ≥ target_horizon，否则隔离不足以阻断 T+5 跨界。"""
    purge = normalize_purge_points(target_horizon_days=TARGET_HORIZON, frequency=frequency)
    covered = normalize_purge_trading_days(purge_points=purge, frequency=frequency)
    assert covered >= TARGET_HORIZON


# ══════════════════════════════════════════════════════════
# C. 段内点数 —— 真实 build_time_split 实测口径（冻结表）
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "frequency,n,expected_train,expected_val,expected_test",
    [
        ("daily", N_DAILY_5Y, 729, 238, 233),
        ("weekly", N_WEEKLY_5Y, 145, 48, 43),
        ("monthly", N_MONTHLY_5Y, 36, 11, 6),
        ("monthly", N_MONTHLY_10Y, 72, 23, 18),
    ],
)
def test_split_budget_matches_frozen_table(
    frequency, n, expected_train, expected_val, expected_test
):
    """四组样本规模的段内点数（取代需求 §2.1 的乐观直算值）。

    需求 §2.1 写月频 val 12 / test 12，那是 n×ratio 直算、未扣 purge/embargo/tail_loss；
    实算为 **val 11 / test 6**。
    """
    _split, budget = _case(frequency, n)
    assert budget.total_points == n
    assert (budget.train_points, budget.val_points, budget.test_points) == (
        expected_train,
        expected_val,
        expected_test,
    )
    assert budget.meets_floor is True


def test_weekly_5y_does_not_hard_fail():
    """C12 回归：周频 243 点曾因默认 `min_validation_days=50` 推出
    `derived_min_total=260` 而**必然**抛 `insufficient_dates`。

    修好后应能正常切出 val/test 两段，且各自不低于频率地板。
    """
    split, budget = _case("weekly", N_WEEKLY_5Y)
    assert split.test_start is not None, "周频必须能切出 test 段"
    assert budget.val_points >= SPLIT_MINIMUMS["weekly"][1]
    assert budget.test_points >= SPLIT_MINIMUMS["weekly"][2]


@pytest.mark.parametrize(
    "frequency,n,should_degrade",
    [
        ("daily", N_DAILY_5Y, False),
        ("weekly", N_WEEKLY_5Y, False),
        ("monthly", N_MONTHLY_5Y, True),
        ("monthly", N_MONTHLY_10Y, True),
    ],
)
def test_degraded_flag_matches_test_point_count(frequency, n, should_degrade):
    """`statistically_degraded` 必须严格等价于 `test_points < 24`。

    月频 10 年镜像只有 18 点 → 即使镜像也评不到 S/A（需求 §4.1 的 `test ≥ 24` 不可达）。
    本断言只陈述事实，不锁定门槛数值 —— 门槛数值是待决 Q3。
    """
    _split, budget = _case(frequency, n)
    assert budget.statistically_degraded is should_degrade
    assert budget.statistically_degraded == (budget.test_points < DEGRADED_TEST_THRESHOLD)


# ══════════════════════════════════════════════════════════
# D. 泄漏不变量 —— 段序、隔离点数、tail_loss 口径
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize("frequency,n", CASES_MAIN)
def test_segments_are_strictly_ordered_with_gaps(frequency, n):
    """四段必须严格有序、**不重叠**。这是防泄漏的第一道结构保证。"""
    split, _budget = _case(frequency, n)
    assert split.train_start < split.train_end
    assert split.train_end < split.validation_start, "训练段与验证段必须留隔离期"
    assert split.validation_start <= split.validation_end
    assert split.validation_end < split.test_start, "验证段与测试段必须留禁运期"
    assert split.test_start <= split.test_end


@pytest.mark.parametrize(
    "frequency,n", [("daily", N_DAILY_5Y), ("weekly", N_WEEKLY_5Y), ("monthly", N_MONTHLY_5Y)]
)
def test_purge_and_embargo_drop_exact_rebalance_points(frequency, n):
    """★ C11 的核心语义断言：被丢弃的是**调仓点**，数量恰好等于 purge/embargo。

    若沿用未归一化的写死 5：daily 仍丢 5 点（正确），但 weekly/monthly 会丢 5 期
    （= 25 / 105 个交易日），隔离窗口被放大到荒谬的量级。
    """
    series = _series(n, frequency=frequency)
    split, budget = _case(frequency, n)
    idx = {d: i for i, d in enumerate(sorted(series))}

    dropped_before_val = idx[split.validation_start] - (idx[split.train_end] + 1)
    dropped_before_test = idx[split.test_start] - (idx[split.validation_end] + 1)

    assert dropped_before_val == budget.purge_points
    assert dropped_before_test == budget.embargo_points


@pytest.mark.parametrize(
    "frequency,n", [("weekly", N_WEEKLY_5Y), ("monthly", N_MONTHLY_10Y)]
)
def test_unnormalized_purge_would_shrink_validation_and_test(frequency, n):
    """反证 C11 的必要性：不归一化（purge/embargo 沿用写死的 5）会额外吃掉 val/test。

    实测：weekly 48/43 → 44/39；monthly 10y 23/18 → 19/14。
    注意点数差只是表象 —— 真正的错在于隔离期语义从「1 周」变成「5 周」。
    """
    series = _series(n, frequency=frequency)
    _split, normalized = build_split(
        all_dates=series, frequency=frequency, target_horizon=TARGET_HORIZON
    )
    naive = compute_split_budget(
        all_dates=series,
        frequency=frequency,
        purge_points=5,  # ← 未归一化的写死值
        embargo_points=5,
        tail_loss=TARGET_HORIZON,
    )
    assert normalized.val_points > naive.val_points
    assert normalized.test_points > naive.test_points
    # 且未归一化的隔离窗口折算成交易日会超出所需 20 倍以上
    assert normalize_purge_trading_days(
        purge_points=5, frequency=frequency
    ) > TARGET_HORIZON * 4


@pytest.mark.parametrize("frequency,n", CASES_MAIN)
def test_budget_val_matches_span_and_test_excludes_tail_loss(frequency, n):
    """⚠ 口径提醒：`SplitBudget` 与 `TimeSplit` 的样本数定义**不同**。

    - `val_points` == `validation_start..validation_end` 的跨度长度（口径一致）
    - `test_points` == `test_start..test_end` 跨度长度 **− target_horizon**
      因为尾部 `target_horizon` 个点的 forward return 落空（NaN 标签），不可用。

    下游（M10 `evaluate_full` / M11 报告）若直接用 `test_end − test_start` 当样本数，
    会多算 `target_horizon` 个点。
    """
    series = _series(n, frequency=frequency)
    split, budget = _case(frequency, n)
    idx = {d: i for i, d in enumerate(sorted(series))}

    val_span = idx[split.validation_end] - idx[split.validation_start] + 1
    test_span = idx[split.test_end] - idx[split.test_start] + 1

    assert budget.val_points == val_span
    assert budget.test_points == test_span - TARGET_HORIZON
    assert budget.tail_loss == TARGET_HORIZON


# ══════════════════════════════════════════════════════════
# E. 反向断言 —— C11 单独生效**不够**，必须与 C12 同时生效
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "frequency,n",
    [("weekly", N_WEEKLY_5Y), ("monthly", N_MONTHLY_5Y), ("monthly", N_MONTHLY_10Y)],
)
def test_normalization_alone_is_insufficient(frequency, n):
    """★ C12 的反向哨兵：**只**做单位归一化、仍用 `build_time_split` 的默认 `min_*`
    → 周频与月频**照样全部**抛 `insufficient_dates`。

    默认 `min_validation_days=50` 推出 `derived_min_total = ceil((50+1+1)/0.2) = 260`：
    周频 243 点、月频 60/120 点全部不达标。

    因此 C11 与 C12 缺一不可 —— 这条断言就是证据。
    """
    purge = normalize_purge_points(target_horizon_days=TARGET_HORIZON, frequency=frequency)

    with pytest.raises(ValueError) as ei:
        # 故意只传归一化后的 purge/embargo，不传 min_validation_days / min_test_days / min_total_days
        build_time_split(
            all_dates=_series(n, frequency=frequency),
            target_horizon=TARGET_HORIZON,
            purge_days=purge,
            embargo_days=purge,
        )

    msg = str(ei.value)
    assert "insufficient_dates" in msg
    assert "derived_min_total=260" in msg
    assert "min_val_days=50" in msg


def test_explicit_min_star_lifts_the_block():
    """C12 正向：`build_split` 显式传 `min_*` 后，同一序列不再抛异常。

    与上一条构成「同一输入、仅差 min_*」的对照，证明阻断来自默认值本身。
    """
    split, budget = build_split(
        all_dates=_series(N_WEEKLY_5Y, frequency="weekly"),
        frequency="weekly",
        target_horizon=TARGET_HORIZON,
    )
    assert budget.meets_floor is True
    assert split.test_start is not None


# ══════════════════════════════════════════════════════════
# F. 频率地板拦截
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "frequency,n,floor",
    [("daily", 240, 252), ("weekly", 100, 104), ("monthly", 30, 41)],
)
def test_below_frequency_floor_raises_mining_split_error(frequency, n, floor):
    """低于频率地板必须抛 `MiningSplitError`（而非裸 `ValueError`），
    路由层才能转成 `MINING_SAMPLE_INSUFFICIENT` 错误码并带 `fix_link`。
    """
    assert SPLIT_MINIMUMS[frequency][0] == floor
    assert n < floor

    with pytest.raises(MiningSplitError) as ei:
        _case(frequency, n)

    assert ei.value.budget is not None
    assert ei.value.budget.meets_floor is False
    assert str(n) in str(ei.value)


def test_mining_split_error_carries_budget_for_ui():
    """异常必须带 budget，Step2 才能展示「差多少点、建议怎么做」。"""
    with pytest.raises(MiningSplitError) as ei:
        _case("monthly", 30)

    budget = ei.value.budget
    assert budget is not None
    assert budget.frequency == "monthly"
    assert budget.total_points == 30
    assert budget.frequency_floor == 41


def test_empty_input_is_rejected_not_crashed():
    """空输入不得抛 IndexError —— 必须走正常的不达标路径。"""
    budget = compute_split_budget(
        all_dates=[],
        frequency="daily",
        purge_points=5,
        embargo_points=5,
        tail_loss=TARGET_HORIZON,
    )
    assert budget.total_points == 0
    assert budget.meets_floor is False

    with pytest.raises(MiningSplitError):
        build_split(all_dates=[], frequency="daily", target_horizon=TARGET_HORIZON)


# ══════════════════════════════════════════════════════════
# G. 已知缺口哨兵 —— 本任务只记录，**不自行修复**
# ══════════════════════════════════════════════════════════


def test_split_minimums_self_consistent():
    """**D-I 裁决落地（2026-09-17）**：三段最小点数已改为自洽三元组。

    旧哨兵（`test_declared_floor_understates_effective_minimum`）钉住的「死区」已消除：
    `SPLIT_MINIMUMS` 新值 `(252,40,40)/(104,18,10)/(41,5,3)` 满足——

        derived_min_total = max(min_total, ceil((min_val+purge+embargo)/val_ratio))
                          == min_total        （无死区）

    且 floor 处各分段实际可用点数 ≥ 声明的分段最小值（月频 floor 由 36 上调至 41，
    使 test ≥ 3 有统计意义；月频 test < 24 仍走降级路径，最高 B 级）。
    """
    from app.services.factors.mining.evaluation_adapter import SPLIT_MINIMUMS
    import math as _math

    for frequency, (min_total, min_val, min_test) in SPLIT_MINIMUMS.items():
        purge = normalize_purge_points(
            target_horizon_days=TARGET_HORIZON, frequency=frequency)
        derived = max(min_total, _math.ceil(
            (min_val + 2 * purge) / 0.2))
        assert derived <= min_total, (
            f"{frequency}: derived({derived}) > 声明地板({min_total}) —— "
            "死区回来了，请按 floor 处可用分段重新推导三元组")


def test_split_floor_sweep_no_dead_zone():
    """逐点扫描 [floor, floor+40]：`build_split` 必须全部成功（无点级死区）。"""
    import datetime as dt

    step = {"daily": 1, "weekly": 5, "monthly": 21}
    from app.services.factors.mining.evaluation_adapter import SPLIT_MINIMUMS

    for frequency, (min_total, _mv, _mt) in SPLIT_MINIMUMS.items():
        for n in range(min_total, min_total + 41):
            dates = [dt.date(2021, 1, 1)
                     + dt.timedelta(days=step[frequency] * i) for i in range(n)]
            split, budget = _case(frequency, n)
            assert budget.meets_floor is True, (frequency, n)
            assert split.test_end is not None, (frequency, n)



@pytest.mark.parametrize(
    "frequency,n",
    [("weekly", 243), ("monthly", 60), ("monthly", 120)],
)
def test_feasible_cases_sit_above_effective_minimum(frequency, n):
    """反向确认：需求文档给的周/月频真实场景（5 年周频 243 点、5 年月频 60 点、
    10 年月频 120 点）都在 **D-I 新地板**（104/41）之上，切分成功。

    （原哨兵版本断言「这些场景在旧死区之上」；D-I 落地后死区已消除，
     本测试保留为「真实场景可用」的回归确认。）
    """
    _split, budget = _case(frequency, n)
    assert budget.meets_floor is True


# ══════════════════════════════════════════════════════════
# H. assert_no_leakage —— 红线的运行时执行点
# ══════════════════════════════════════════════════════════


def test_assert_no_leakage_accepts_dates_inside_train_window():
    """train 段内日期通过（含两端）。"""
    split, _budget = _case("daily", N_DAILY_5Y)
    assert_no_leakage(split, [split.train_start, split.train_end])


def test_assert_no_leakage_accepts_empty_usage():
    """空列表不报错（尚未用到任何日期）。"""
    split, _budget = _case("daily", N_DAILY_5Y)
    assert_no_leakage(split, [])


def test_assert_no_leakage_raises_on_validation_date():
    """★ 用到验证段日期 → 必须抛 AssertionError。宁可中断也不出污染结论。"""
    split, _budget = _case("daily", N_DAILY_5Y)
    with pytest.raises(AssertionError) as ei:
        assert_no_leakage(split, [split.train_start, split.validation_start])
    assert "fitness leakage" in str(ei.value)


def test_assert_no_leakage_raises_on_test_date():
    """用到测试段日期 → 必须抛。test 段只能在最终验证用一次（红线 C8）。"""
    split, _budget = _case("daily", N_DAILY_5Y)
    with pytest.raises(AssertionError):
        assert_no_leakage(split, [split.test_start])


def test_assert_no_leakage_raises_before_train_start():
    """早于 train_start 的日期同样越界（例如误用全量历史做回填）。"""
    split, _budget = _case("daily", N_DAILY_5Y)
    earlier = split.train_start - dt.timedelta(days=1)
    with pytest.raises(AssertionError):
        assert_no_leakage(split, [earlier])


def test_assert_no_leakage_reports_first_offenders():
    """诊断信息要能定位越界日期，否则线上很难排查。

    注意：消息里日期按 `repr` 渲染（`datetime.date(2023, 1, 5)`），不是 ISO 字符串 ——
    断言必须匹配实际格式（曾因写 `str(date)` 而误判为失败）。
    """
    split, _budget = _case("daily", N_DAILY_5Y)
    offenders = [split.validation_start, split.test_start]
    with pytest.raises(AssertionError) as ei:
        assert_no_leakage(split, offenders)

    msg = str(ei.value)
    assert "train window is" in msg
    assert f"{len(offenders)} dates outside were used" in msg
    # 两个越界日期都必须出现在消息里（按 actual repr 格式）
    for d in offenders:
        assert repr(d) in msg, f"越界日期 {d!r} 未出现在诊断信息中: {msg}"
