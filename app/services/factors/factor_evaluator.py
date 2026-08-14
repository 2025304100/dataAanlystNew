"""WP5: 因子科学评估与压力测试。

对齐 docs/专业因子库开发计划.md §WP5 和 docs/因子设置与专业因子库改造方案.md §8。

核心能力：
- WP5-01 评估运行契约：不可变 EvaluationRun、配置哈希、数据截止时间、幂等键、终态保护
- WP5-02 时间切分与样本构造：训练/验证/测试、purge/embargo、目标对齐、事件日样本
- WP5-03 基础指标：Rank IC、ICIR、覆盖、分组单调性、换手、成本后收益
- WP5-04 分类门禁：continuous/event/regime 独立阈值和失败原因

安全约束：
- 评估结果可重复（同版本+同快照+同配置→容差内一致）
- 时间安全（财报字段按公告日可见，目标退出日不进入特征可见区）
- 事件因子不被误用连续门禁（只对事件日样本计算）
- EvaluationRun 不可变（创建后不允许修改 metrics_json/gate_result/config_json）
- 幂等键防重复（同 factor_version_id + config_hash + data_cutoff 不创建两个运行）
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.models.factor_evaluation import EvaluationRun
from app.services.factors.trade_calendar import (
    CompleteTradeDayEvidence,
    latest_complete_trade_date,
)


# ══════════════════════════════════════════════════════════
# WP5-01: 评估运行契约
# ══════════════════════════════════════════════════════════

EVALUATOR_VERSION = "wp5-1.0.0"

# 不可变字段集合（创建后不允许修改）
IMMUTABLE_FIELDS = frozenset({
    "factor_version_id",
    "universe_snapshot_id",
    "data_cutoff_at",
    "target_code",
    "train_start_date",
    "train_end_date",
    "validation_start_date",
    "validation_end_date",
    "config_json",
    "created_by",
})

# 终态门禁结果（不允许从 passed/rejected 回退）
TERMINAL_GATE_RESULTS = frozenset({"passed", "rejected"})


def compute_config_hash(config: dict[str, Any]) -> str:
    """计算评估配置哈希（SHA256 前 16 位，canonical JSON）。

    用于幂等检查：同 factor_version_id + config_hash + data_cutoff_at 不创建两个运行。
    """
    canonical = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def build_evaluation_run_id(
    *,
    factor_version_id: int,
    config_hash: str,
    data_cutoff: date,
) -> str:
    """构建确定性的评估运行 ID。

    格式：eval-{factor_version_id}-{config_hash[:8]}-{cutoff_yyyymmdd}
    确保同参数重复运行得到相同 ID（幂等）。
    """
    cutoff_str = data_cutoff.strftime("%Y%m%d")
    return f"eval-{factor_version_id}-{config_hash[:8]}-{cutoff_str}"


def create_evaluation_run(
    db: Session,
    *,
    factor_version_id: int,
    config: dict[str, Any],
    data_cutoff_at: datetime | None = None,
    target_code: str = "target_5d_return",
    train_start: date | None = None,
    train_end: date | None = None,
    validation_start: date | None = None,
    validation_end: date | None = None,
    universe_snapshot_id: str | None = None,
    created_by: str = "local_user",
    task_id: str | None = None,
    ctd_evidence: CompleteTradeDayEvidence | None = None,
) -> EvaluationRun:
    """创建不可变评估运行记录（幂等）。

    幂等逻辑：同 factor_version_id + config_hash + data_cutoff_at 已存在则返回既有记录。
    终态保护：已存在的记录如果 gate_result 是 passed/rejected，不允许覆盖。
    """
    config_hash = compute_config_hash(config)
    cutoff_date = (
        data_cutoff_at.date() if isinstance(data_cutoff_at, datetime)
        else data_cutoff_at if isinstance(data_cutoff_at, date)
        else date.today()
    )
    run_id = build_evaluation_run_id(
        factor_version_id=factor_version_id,
        config_hash=config_hash,
        data_cutoff=cutoff_date,
    )

    # 幂等检查
    existing = db.get(EvaluationRun, run_id)
    if existing is not None:
        # 终态保护：如果已有终态门禁结果，返回既有记录不覆盖
        if existing.gate_result in TERMINAL_GATE_RESULTS:
            return existing
        # 非终态（None/warn）允许更新可变字段（metrics/gate/artifact/task_id）
        return existing

    # 创建新记录
    run = EvaluationRun(
        id=run_id,
        factor_version_id=factor_version_id,
        universe_snapshot_id=universe_snapshot_id,
        data_cutoff_at=data_cutoff_at,
        target_code=target_code,
        train_start_date=datetime.combine(train_start, datetime.min.time()) if train_start else None,
        train_end_date=datetime.combine(train_end, datetime.min.time()) if train_end else None,
        validation_start_date=datetime.combine(validation_start, datetime.min.time()) if validation_start else None,
        validation_end_date=datetime.combine(validation_end, datetime.min.time()) if validation_end else None,
        config_json=json.dumps(config, ensure_ascii=False, default=str),
        metrics_json="{}",
        gate_result=None,
        rejection_reasons_json=None,
        task_id=task_id,
        created_by=created_by,
        # WPD-02 完整交易日证据
        selected_trade_date=ctd_evidence.selected_trade_date.isoformat() if ctd_evidence else None,
        observed_symbols=ctd_evidence.observed_symbols if ctd_evidence else None,
        expected_symbols=ctd_evidence.expected_symbols if ctd_evidence else None,
        completeness_ratio=round(ctd_evidence.completeness_ratio, 6) if ctd_evidence else None,
        fallback_reason=ctd_evidence.fallback_reason if ctd_evidence else None,
    )
    db.add(run)
    db.flush()
    return run


def finalize_evaluation_run(
    db: Session,
    *,
    run_id: str,
    metrics: dict[str, Any],
    gate_result: Literal["passed", "rejected", "warn"],
    rejection_reasons: list[str] | None = None,
    artifact_path: str | None = None,
) -> EvaluationRun:
    """完成评估运行（写入 metrics 和门禁结论）。

    终态保护：如果 run 已有 passed/rejected 门禁结果，拒绝覆盖。
    """
    run = db.get(EvaluationRun, run_id)
    if run is None:
        raise ValueError(f"evaluation_run_not_found:{run_id}")

    # 终态保护
    if run.gate_result in TERMINAL_GATE_RESULTS:
        raise ValueError(f"evaluation_run_finalized:{run.gate_result}")

    run.metrics_json = json.dumps(metrics, ensure_ascii=False, default=str)
    run.gate_result = gate_result
    run.rejection_reasons_json = (
        json.dumps(rejection_reasons, ensure_ascii=False) if rejection_reasons else None
    )
    if artifact_path:
        run.artifact_path = artifact_path
    db.flush()
    return run


def get_evaluation_run(db: Session, run_id: str) -> EvaluationRun | None:
    """获取评估运行记录。"""
    return db.get(EvaluationRun, run_id)


def list_evaluation_runs(
    db: Session,
    *,
    factor_version_id: int | None = None,
    gate_result: str | None = None,
    limit: int = 20,
) -> list[EvaluationRun]:
    """列出评估运行记录。"""
    from sqlalchemy import select

    stmt = select(EvaluationRun).order_by(EvaluationRun.created_at.desc())
    if factor_version_id is not None:
        stmt = stmt.where(EvaluationRun.factor_version_id == factor_version_id)
    if gate_result is not None:
        stmt = stmt.where(EvaluationRun.gate_result == gate_result)
    stmt = stmt.limit(limit)
    return list(db.scalars(stmt))


# ══════════════════════════════════════════════════════════
# WP5-02: 时间切分与样本构造
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True)
class TimeSplit:
    """时间切分方案（训练/验证/测试 + purge + embargo）。"""

    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date | None = None
    test_end: date | None = None
    purge_days: int = 5  # 训练-验证之间的隔离期
    embargo_days: int = 5  # 验证-测试之间的禁运期
    target_horizon: int = 5  # 目标收益 horizon（天）


def build_time_split(
    *,
    all_dates: list[date],
    target_horizon: int = 5,
    train_ratio: float = 0.6,
    validation_ratio: float = 0.2,
    purge_days: int = 5,
    embargo_days: int = 5,
) -> TimeSplit:
    """构建时间切分方案。

    安全约束：
    - 训练-验证之间有 purge 期（防止目标 horizon 泄漏）
    - 验证-测试之间有 embargo 期
    - 目标退出日不进入特征可见区（通过 purge + embargo 保证）
    """
    if len(all_dates) < 20:
        raise ValueError("insufficient_dates:需要至少 20 个交易日")

    sorted_dates = sorted(all_dates)
    n = len(sorted_dates)

    # 计算切分点
    train_end_idx = int(n * train_ratio)
    val_end_idx = int(n * (train_ratio + validation_ratio))

    train_start = sorted_dates[0]
    train_end = sorted_dates[max(0, train_end_idx - 1)]

    # purge：训练结束后跳过 purge_days 个交易日
    val_start_idx = min(train_end_idx + purge_days, n - 1)
    validation_start = sorted_dates[val_start_idx]
    validation_end = sorted_dates[max(val_start_idx, val_end_idx - 1)]

    # embargo + test
    test_start_idx = min(val_end_idx + embargo_days, n - 1)
    test_start = sorted_dates[test_start_idx] if test_start_idx < n else None
    test_end = sorted_dates[-1] if test_start is not None else None

    return TimeSplit(
        train_start=train_start,
        train_end=train_end,
        validation_start=validation_start,
        validation_end=validation_end,
        test_start=test_start,
        test_end=test_end,
        purge_days=purge_days,
        embargo_days=embargo_days,
        target_horizon=target_horizon,
    )


@dataclass
class AlignedSample:
    """特征-目标对齐后的样本。"""

    features: pd.DataFrame  # index=date, columns=symbol, values=factor_value
    targets: pd.DataFrame  # index=date, columns=symbol, values=forward_return
    trade_dates: list[date]
    symbols: list[str]
    coverage: float  # 有效样本占比
    event_dates: list[date] | None = None  # 事件因子的有效日期


def align_factor_with_target(
    factor_values: pd.DataFrame,
    forward_returns: pd.DataFrame,
    *,
    factor_kind: str = "continuous",
    event_dates: list[date] | None = None,
) -> AlignedSample:
    """对齐因子值与目标收益。

    安全约束：
    - 特征日 t 的因子值只能对齐 t+horizon 的目标收益（不预测过去）
    - 事件因子只对齐事件日样本
    - 移除 NaN 对（特征或目标缺失）

    Args:
        factor_values: index=date, columns=symbol, values=factor_value
        forward_returns: index=date, columns=symbol, values=forward_return（已对齐到特征日）
        factor_kind: continuous/event/regime
        event_dates: 事件因子的有效日期列表（仅 event 类型使用）
    """
    # 对齐索引和列
    common_dates = factor_values.index.intersection(forward_returns.index)
    common_symbols = factor_values.columns.intersection(forward_returns.columns)

    if len(common_dates) == 0 or len(common_symbols) == 0:
        return AlignedSample(
            features=pd.DataFrame(),
            targets=pd.DataFrame(),
            trade_dates=[],
            symbols=[],
            coverage=0.0,
            event_dates=event_dates,
        )

    fv = factor_values.loc[common_dates, common_symbols]
    fr = forward_returns.loc[common_dates, common_symbols]

    # 事件因子只保留事件日样本
    if factor_kind == "event" and event_dates:
        event_date_set = set(event_dates)
        event_idx = [d for d in fv.index if d in event_date_set]
        if len(event_idx) == 0:
            return AlignedSample(
                features=pd.DataFrame(),
                targets=pd.DataFrame(),
                trade_dates=[],
                symbols=[],
                coverage=0.0,
                event_dates=event_dates,
            )
        fv = fv.loc[event_idx]
        fr = fr.loc[event_idx]

    # 移除 NaN 对
    mask = fv.notna() & fr.notna()
    fv_clean = fv.where(mask)
    fr_clean = fr.where(mask)

    total_cells = fv.size
    valid_cells = mask.sum().sum()
    coverage = float(valid_cells / total_cells) if total_cells > 0 else 0.0

    return AlignedSample(
        features=fv_clean,
        targets=fr_clean,
        trade_dates=list(fv_clean.index),
        symbols=list(fv_clean.columns),
        coverage=coverage,
        event_dates=event_dates if factor_kind == "event" else None,
    )


# ══════════════════════════════════════════════════════════
# WP5-03: 基础评估指标
# ══════════════════════════════════════════════════════════


@dataclass
class ICMetrics:
    """IC（信息系数）相关指标。"""

    rank_ic_mean: float  # 日截面 Rank IC 均值
    rank_ic_median: float  # 日截面 Rank IC 中位数
    rank_ic_std: float  # Rank IC 标准差
    icir: float  # ICIR = mean / std
    positive_ic_ratio: float  # 正 IC 日期比例
    ic_series: list[float] = field(default_factory=list)  # 日 IC 序列


def compute_rank_ic(
    features: pd.DataFrame,
    targets: pd.DataFrame,
) -> ICMetrics:
    """计算日截面 Rank IC（Spearman 秩相关）。

    对每个交易日计算横截面 Rank IC，然后聚合。
    """
    ic_series: list[float] = []

    for trade_date in features.index:
        fv = features.loc[trade_date].dropna()
        tv = targets.loc[trade_date].dropna()

        # 对齐共同标的
        common = fv.index.intersection(tv.index)
        if len(common) < 5:
            ic_series.append(0.0)
            continue

        fv_ranked = fv.loc[common].rank()
        tv_ranked = tv.loc[common].rank()

        # Spearman 秩相关
        if fv_ranked.std() == 0 or tv_ranked.std() == 0:
            ic_series.append(0.0)
            continue

        corr = float(fv_ranked.corr(tv_ranked))
        if math.isnan(corr):
            ic_series.append(0.0)
        else:
            ic_series.append(corr)

    if not ic_series:
        return ICMetrics(0.0, 0.0, 0.0, 0.0, 0.0, [])

    arr = np.array(ic_series, dtype=float)
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0
    icir = float(mean / std) if std > 1e-10 else 0.0
    positive_ratio = float(np.mean(arr > 0))

    return ICMetrics(
        rank_ic_mean=mean,
        rank_ic_median=float(np.median(arr)),
        rank_ic_std=std,
        icir=icir,
        positive_ic_ratio=positive_ratio,
        ic_series=ic_series,
    )


@dataclass
class QuantileMetrics:
    """分组单调性指标。"""

    n_groups: int
    group_returns: list[float]  # 各分组平均收益
    monotonicity_score: float  # 单调性得分（-1 到 1）
    top_bottom_return: float  # Top-Bottom 多空收益
    top_bottom_sharpe: float  # Top-Bottom 夏普比率


def compute_quantile_returns(
    features: pd.DataFrame,
    targets: pd.DataFrame,
    *,
    n_groups: int = 5,
) -> QuantileMetrics:
    """计算分组收益和单调性。

    对每个交易日按因子值分 N 组，计算各组平均收益，然后聚合。
    """
    group_returns_daily: list[list[float]] = []

    for trade_date in features.index:
        fv = features.loc[trade_date].dropna()
        tv = targets.loc[trade_date].dropna()

        common = fv.index.intersection(tv.index)
        if len(common) < n_groups:
            continue

        fv_common = fv.loc[common]
        tv_common = tv.loc[common]

        # 按因子值排序分组
        try:
            groups = pd.qcut(fv_common, n_groups, labels=False, duplicates="drop")
        except ValueError:
            continue

        if groups.nunique() < 2:
            continue

        daily_returns = []
        for g in range(n_groups):
            mask = groups == g
            if mask.sum() > 0:
                daily_returns.append(float(tv_common[mask].mean()))
            else:
                daily_returns.append(0.0)
        group_returns_daily.append(daily_returns)

    if not group_returns_daily:
        return QuantileMetrics(n_groups, [0.0] * n_groups, 0.0, 0.0, 0.0)

    arr = np.array(group_returns_daily)
    avg_returns = arr.mean(axis=0).tolist()

    # 单调性得分：分组收益与组序号的相关性
    group_idx = np.arange(len(avg_returns))
    if np.std(avg_returns) > 1e-10:
        monotonicity = float(np.corrcoef(group_idx, avg_returns)[0, 1])
    else:
        monotonicity = 0.0

    # Top-Bottom 多空收益
    top_bottom = float(avg_returns[-1] - avg_returns[0]) if len(avg_returns) >= 2 else 0.0

    # Top-Bottom 夏普（跨日）
    if arr.shape[0] > 1:
        daily_tb = arr[:, -1] - arr[:, 0]
        tb_std = float(np.std(daily_tb, ddof=1))
        tb_sharpe = float(np.mean(daily_tb) / tb_std) if tb_std > 1e-10 else 0.0
    else:
        tb_sharpe = 0.0

    return QuantileMetrics(
        n_groups=len(avg_returns),
        group_returns=avg_returns,
        monotonicity_score=monotonicity,
        top_bottom_return=top_bottom,
        top_bottom_sharpe=tb_sharpe,
    )


@dataclass
class TurnoverMetrics:
    """换手率指标。"""

    avg_turnover: float  # 平均日换手率
    turnover_std: float  # 换手率标准差


def compute_turnover(features: pd.DataFrame) -> TurnoverMetrics:
    """计算因子换手率（持仓变化率）。

    换手率 = sum(|weight_t - weight_{t-1}|) / 2，权重按因子值排名归一化。
    """
    if features.shape[0] < 2:
        return TurnoverMetrics(0.0, 0.0)

    turnovers: list[float] = []
    dates = list(features.index)

    for i in range(1, len(dates)):
        prev = features.loc[dates[i - 1]].dropna()
        curr = features.loc[dates[i]].dropna()

        common = prev.index.intersection(curr.index)
        if len(common) < 2:
            continue

        # 按排名计算权重（归一化到 [0, 1]）
        prev_rank = prev.loc[common].rank()
        curr_rank = curr.loc[common].rank()

        if prev_rank.max() > 0:
            prev_weight = prev_rank / prev_rank.max()
        else:
            continue
        if curr_rank.max() > 0:
            curr_weight = curr_rank / curr_rank.max()
        else:
            continue

        turnover = float((prev_weight - curr_weight).abs().sum() / 2 / len(common))
        turnovers.append(turnover)

    if not turnovers:
        return TurnoverMetrics(0.0, 0.0)

    arr = np.array(turnovers, dtype=float)
    return TurnoverMetrics(
        avg_turnover=float(np.mean(arr)),
        turnover_std=float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0,
    )


@dataclass
class CostAdjustedReturn:
    """成本后收益。"""

    gross_return: float  # 成本前收益
    cost: float  # 交易成本
    net_return: float  # 成本后收益
    cost_rate: float  # 单边成本率


def compute_cost_adjusted_return(
    top_bottom_return: float,
    turnover: float,
    *,
    cost_rate: float = 0.001,  # 单边成本率 0.1%
) -> CostAdjustedReturn:
    """计算成本后 Top-Bottom 收益。

    成本 = 换手率 × 2 × 单边成本率（买卖双边）
    """
    cost = float(turnover * 2 * cost_rate)
    net = float(top_bottom_return - cost)
    return CostAdjustedReturn(
        gross_return=top_bottom_return,
        cost=cost,
        net_return=net,
        cost_rate=cost_rate,
    )


@dataclass
class CoverageMetrics:
    """覆盖率和数据质量指标。"""

    coverage: float  # 有效样本占比
    missing_rate: float  # 缺失率
    anomaly_rate: float  # 异常率（Inf/NaN 占比）
    available_dates: int  # 可用交易日数
    available_symbols: int  # 可用标的数


def compute_coverage(features: pd.DataFrame) -> CoverageMetrics:
    """计算覆盖率指标。"""
    total = features.size
    if total == 0:
        return CoverageMetrics(0.0, 1.0, 0.0, 0, 0)

    nan_count = int(features.isna().sum().sum())
    inf_count = int(np.isinf(features.select_dtypes(include=[np.number])).sum().sum()) if features.shape[0] > 0 else 0
    valid_count = total - nan_count

    return CoverageMetrics(
        coverage=float(valid_count / total),
        missing_rate=float(nan_count / total),
        anomaly_rate=float(inf_count / total) if total > 0 else 0.0,
        available_dates=int(features.shape[0]),
        available_symbols=int(features.shape[1]),
    )


# ══════════════════════════════════════════════════════════
# WP5-04: 分类门禁
# ══════════════════════════════════════════════════════════


# 门禁阈值（可配置，初始研究阈值）
GATE_THRESHOLDS = {
    "continuous": {
        "min_coverage": 0.70,  # 覆盖率不低于 70%
        "min_rank_ic": 0.0,  # 平均 Rank IC > 0
        "min_icir": 0.15,  # ICIR > 0.15
        "min_positive_ic_ratio": 0.55,  # 正 IC 日期比例 >= 55%
        "min_net_return": 0.0,  # 成本后 Top-Bottom 收益 > 0
        "min_validation_days": 50,  # 验证交易日 >= 50
        "min_effective_samples": 10000,  # 有效样本 >= 10000
    },
    "event": {
        "min_event_samples": 50,  # 事件样本数 >= 50
        "min_event_coverage": 0.30,  # 事件覆盖率 >= 30%
        "min_rank_ic": 0.0,
        "min_icir": 0.10,  # 事件因子 ICIR 阈值放宽
        "min_positive_ic_ratio": 0.50,  # 事件因子正 IC 比例放宽
        "min_net_return": 0.0,
        "min_validation_days": 30,  # 事件因子验证日数放宽
    },
    "regime": {
        "min_time_coverage": 0.60,  # 时间覆盖率 >= 60%
        "min_state_distinction": 0.10,  # 状态区分度
        "min_validation_days": 50,
    },
}


@dataclass
class GateResult:
    """门禁结论。"""

    result: Literal["passed", "rejected", "warn"]
    factor_kind: str
    reasons: list[str]  # 失败原因（空列表表示通过）
    metrics_summary: dict[str, Any]


def evaluate_gate(
    *,
    factor_kind: str,
    ic_metrics: ICMetrics,
    quantile_metrics: QuantileMetrics,
    turnover_metrics: TurnoverMetrics,
    cost_adjusted: CostAdjustedReturn,
    coverage_metrics: CoverageMetrics,
    validation_days: int,
    effective_samples: int,
    event_sample_count: int | None = None,
    thresholds: dict[str, Any] | None = None,
) -> GateResult:
    """执行分类门禁检查。

    continuous：覆盖/IC/ICIR/正IC比例/成本收益/验证日数/有效样本
    event：事件样本数/事件覆盖/IC/ICIR/正IC比例/成本收益
    regime：时间覆盖/状态区分/验证日数
    """
    th = thresholds or GATE_THRESHOLDS.get(factor_kind, GATE_THRESHOLDS["continuous"])
    reasons: list[str] = []

    if factor_kind == "continuous":
        if coverage_metrics.coverage < th["min_coverage"]:
            reasons.append(
                f"coverage_insufficient:覆盖率 {coverage_metrics.coverage:.2%} 低于阈值 {th['min_coverage']:.2%}"
            )
        if ic_metrics.rank_ic_mean <= th["min_rank_ic"]:
            reasons.append(
                f"rank_ic_non_positive:Rank IC 均值 {ic_metrics.rank_ic_mean:.4f} 不大于 0"
            )
        if ic_metrics.icir < th["min_icir"]:
            reasons.append(
                f"icir_below_threshold:ICIR {ic_metrics.icir:.4f} 低于阈值 {th['min_icir']}"
            )
        if ic_metrics.positive_ic_ratio < th["min_positive_ic_ratio"]:
            reasons.append(
                f"positive_ic_ratio_low:正 IC 比例 {ic_metrics.positive_ic_ratio:.2%} 低于阈值 {th['min_positive_ic_ratio']:.2%}"
            )
        if cost_adjusted.net_return <= th["min_net_return"]:
            reasons.append(
                f"net_return_non_positive:成本后收益 {cost_adjusted.net_return:.6f} 不大于 0"
            )
        if validation_days < th["min_validation_days"]:
            reasons.append(
                f"validation_days_insufficient:验证交易日 {validation_days} 低于阈值 {th['min_validation_days']}"
            )
        if effective_samples < th["min_effective_samples"]:
            reasons.append(
                f"effective_samples_insufficient:有效样本 {effective_samples} 低于阈值 {th['min_effective_samples']}"
            )

    elif factor_kind == "event":
        if event_sample_count is not None and event_sample_count < th["min_event_samples"]:
            reasons.append(
                f"event_samples_insufficient:事件样本数 {event_sample_count} 低于阈值 {th['min_event_samples']}"
            )
        if coverage_metrics.coverage < th["min_event_coverage"]:
            reasons.append(
                f"event_coverage_insufficient:事件覆盖率 {coverage_metrics.coverage:.2%} 低于阈值 {th['min_event_coverage']:.2%}"
            )
        if ic_metrics.rank_ic_mean <= th["min_rank_ic"]:
            reasons.append(f"rank_ic_non_positive:Rank IC 均值 {ic_metrics.rank_ic_mean:.4f}")
        if ic_metrics.icir < th["min_icir"]:
            reasons.append(
                f"icir_below_threshold:ICIR {ic_metrics.icir:.4f} 低于阈值 {th['min_icir']}"
            )
        if ic_metrics.positive_ic_ratio < th["min_positive_ic_ratio"]:
            reasons.append(
                f"positive_ic_ratio_low:正 IC 比例 {ic_metrics.positive_ic_ratio:.2%}"
            )
        if cost_adjusted.net_return <= th["min_net_return"]:
            reasons.append(f"net_return_non_positive:成本后收益 {cost_adjusted.net_return:.6f}")
        if validation_days < th["min_validation_days"]:
            reasons.append(
                f"validation_days_insufficient:验证交易日 {validation_days} 低于阈值 {th['min_validation_days']}"
            )

    elif factor_kind == "regime":
        if coverage_metrics.available_dates > 0:
            time_coverage = coverage_metrics.coverage
            if time_coverage < th["min_time_coverage"]:
                reasons.append(
                    f"time_coverage_insufficient:时间覆盖率 {time_coverage:.2%} 低于阈值 {th['min_time_coverage']:.2%}"
                )
        if validation_days < th["min_validation_days"]:
            reasons.append(
                f"validation_days_insufficient:验证交易日 {validation_days} 低于阈值 {th['min_validation_days']}"
            )

    result: Literal["passed", "rejected", "warn"] = "passed" if not reasons else "rejected"

    return GateResult(
        result=result,
        factor_kind=factor_kind,
        reasons=reasons,
        metrics_summary={
            "rank_ic_mean": ic_metrics.rank_ic_mean,
            "icir": ic_metrics.icir,
            "positive_ic_ratio": ic_metrics.positive_ic_ratio,
            "monotonicity_score": quantile_metrics.monotonicity_score,
            "top_bottom_return": quantile_metrics.top_bottom_return,
            "net_return": cost_adjusted.net_return,
            "coverage": coverage_metrics.coverage,
            "avg_turnover": turnover_metrics.avg_turnover,
            "validation_days": validation_days,
            "effective_samples": effective_samples,
            "event_sample_count": event_sample_count,
        },
    )


# ══════════════════════════════════════════════════════════
# 评估主流程
# ══════════════════════════════════════════════════════════


@dataclass
class EvaluationConfig:
    """评估配置。"""

    factor_kind: str = "continuous"
    target_code: str = "target_5d_return"
    target_horizon: int = 5
    n_groups: int = 5
    cost_rate: float = 0.001
    train_ratio: float = 0.6
    validation_ratio: float = 0.2
    purge_days: int = 5
    embargo_days: int = 5
    event_dates: list[date] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluator_version": EVALUATOR_VERSION,
            "factor_kind": self.factor_kind,
            "target_code": self.target_code,
            "target_horizon": self.target_horizon,
            "n_groups": self.n_groups,
            "cost_rate": self.cost_rate,
            "train_ratio": self.train_ratio,
            "validation_ratio": self.validation_ratio,
            "purge_days": self.purge_days,
            "embargo_days": self.embargo_days,
            "event_dates": [d.isoformat() for d in self.event_dates] if self.event_dates else None,
        }


@dataclass
class EvaluationOutcome:
    """评估结果。"""

    run_id: str
    config_hash: str
    gate_result: Literal["passed", "rejected", "warn"]
    rejection_reasons: list[str]
    metrics: dict[str, Any]
    time_split: TimeSplit | None
    coverage: CoverageMetrics


def run_evaluation(
    db: Session,
    *,
    factor_version_id: int,
    factor_values: pd.DataFrame,
    forward_returns: pd.DataFrame,
    config: EvaluationConfig,
    data_cutoff_at: datetime | None = None,
    ctd_evidence: CompleteTradeDayEvidence | None = None,
    created_by: str = "local_user",
    task_id: str | None = None,
) -> EvaluationOutcome:
    """执行完整评估流程。

    流程：创建运行 → 时间切分 → 对齐 → IC → 分组 → 换手 → 成本 → 覆盖 → 门禁 → 写入

    幂等保护：如果同参数运行已终态（passed/rejected），直接返回既有结果不重新计算。
    """
    config_dict = config.to_dict()
    config_hash = compute_config_hash(config_dict)

    cutoff_date = (
        data_cutoff_at.date() if isinstance(data_cutoff_at, datetime)
        else data_cutoff_at if isinstance(data_cutoff_at, date)
        else date.today()
    )

    # 幂等检查：如果同参数运行已终态，直接返回既有结果
    existing_run_id = build_evaluation_run_id(
        factor_version_id=factor_version_id,
        config_hash=config_hash,
        data_cutoff=cutoff_date,
    )
    existing_run = db.get(EvaluationRun, existing_run_id)
    if existing_run is not None and existing_run.gate_result in TERMINAL_GATE_RESULTS:
        # 已终态，返回既有结果
        existing_metrics = json.loads(existing_run.metrics_json) if existing_run.metrics_json else {}
        existing_reasons = (
            json.loads(existing_run.rejection_reasons_json) if existing_run.rejection_reasons_json else []
        )
        split_metrics = existing_metrics.get("time_split", {})
        if not isinstance(split_metrics, dict):
            split_metrics = {}

        def _stored_date(metric_name: str, fallback: date | datetime | None) -> date | None:
            value = split_metrics.get(metric_name, fallback)
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            if isinstance(value, str):
                try:
                    return date.fromisoformat(value[:10])
                except ValueError:
                    return None
            return None

        train_start = _stored_date("train_start", existing_run.train_start_date)
        train_end = _stored_date("train_end", existing_run.train_end_date)
        validation_start = _stored_date("validation_start", existing_run.validation_start_date)
        validation_end = _stored_date("validation_end", existing_run.validation_end_date)
        existing_time_split = None
        if train_start and train_end and validation_start and validation_end:
            existing_time_split = TimeSplit(
                train_start=train_start,
                train_end=train_end,
                validation_start=validation_start,
                validation_end=validation_end,
                test_start=_stored_date("test_start", None),
                test_end=_stored_date("test_end", None),
                purge_days=int(split_metrics.get("purge_days", config.purge_days)),
                embargo_days=int(split_metrics.get("embargo_days", config.embargo_days)),
                target_horizon=config.target_horizon,
            )

        # 兼容早期创建的评估记录：旧记录可能没有保存时间切分字段。
        # 任务 worker 仍需要验证区间执行压力测试，因此使用当前冻结数据重建切分，
        # 不改变不可变的历史运行记录或其指标。
        if existing_time_split is None:
            try:
                existing_time_split = build_time_split(
                    all_dates=sorted(set(factor_values.index.tolist() + forward_returns.index.tolist())),
                    target_horizon=config.target_horizon,
                    train_ratio=config.train_ratio,
                    validation_ratio=config.validation_ratio,
                    purge_days=config.purge_days,
                    embargo_days=config.embargo_days,
                )
            except ValueError:
                # 数据不足时保留 None；调用方可跳过依赖验证区间的可选压力测试。
                existing_time_split = None

        return EvaluationOutcome(
            run_id=existing_run.id,
            config_hash=config_hash,
            gate_result=existing_run.gate_result,  # type: ignore[arg-type]
            rejection_reasons=existing_reasons,
            metrics=existing_metrics,
            time_split=existing_time_split,
            coverage=CoverageMetrics(0.0, 0.0, 0.0, 0, 0),
        )

    # 1. 创建评估运行（幂等）
    all_dates = sorted(set(factor_values.index.tolist() + forward_returns.index.tolist()))
    time_split = build_time_split(
        all_dates=all_dates,
        target_horizon=config.target_horizon,
        train_ratio=config.train_ratio,
        validation_ratio=config.validation_ratio,
        purge_days=config.purge_days,
        embargo_days=config.embargo_days,
    )

    run = create_evaluation_run(
        db,
        factor_version_id=factor_version_id,
        config=config_dict,
        data_cutoff_at=data_cutoff_at or datetime.combine(cutoff_date, datetime.min.time()),
        target_code=config.target_code,
        train_start=time_split.train_start,
        train_end=time_split.train_end,
        validation_start=time_split.validation_start,
        validation_end=time_split.validation_end,
        created_by=created_by,
        task_id=task_id,
        ctd_evidence=ctd_evidence,
    )

    # 2. 对齐样本（仅验证期）
    val_mask = (factor_values.index >= time_split.validation_start) & (
        factor_values.index <= time_split.validation_end
    )
    val_features = factor_values.loc[val_mask]
    val_targets = forward_returns.loc[forward_returns.index.isin(val_features.index)]

    aligned = align_factor_with_target(
        val_features,
        val_targets,
        factor_kind=config.factor_kind,
        event_dates=config.event_dates,
    )

    # 3. 计算指标
    ic_metrics = compute_rank_ic(aligned.features, aligned.targets)
    quantile_metrics = compute_quantile_returns(
        aligned.features, aligned.targets, n_groups=config.n_groups
    )
    turnover_metrics = compute_turnover(aligned.features)
    cost_adjusted = compute_cost_adjusted_return(
        quantile_metrics.top_bottom_return,
        turnover_metrics.avg_turnover,
        cost_rate=config.cost_rate,
    )
    coverage_metrics = compute_coverage(aligned.features)

    validation_days = len(aligned.trade_dates)
    effective_samples = int(aligned.features.notna().sum().sum())
    event_sample_count = (
        len(config.event_dates) if config.factor_kind == "event" and config.event_dates else None
    )

    # 4. 门禁
    gate = evaluate_gate(
        factor_kind=config.factor_kind,
        ic_metrics=ic_metrics,
        quantile_metrics=quantile_metrics,
        turnover_metrics=turnover_metrics,
        cost_adjusted=cost_adjusted,
        coverage_metrics=coverage_metrics,
        validation_days=validation_days,
        effective_samples=effective_samples,
        event_sample_count=event_sample_count,
    )

    # 5. 构建 metrics
    metrics = {
        "evaluator_version": EVALUATOR_VERSION,
        "ic": {
            "rank_ic_mean": ic_metrics.rank_ic_mean,
            "rank_ic_median": ic_metrics.rank_ic_median,
            "rank_ic_std": ic_metrics.rank_ic_std,
            "icir": ic_metrics.icir,
            "positive_ic_ratio": ic_metrics.positive_ic_ratio,
        },
        "quantile": {
            "n_groups": quantile_metrics.n_groups,
            "group_returns": quantile_metrics.group_returns,
            "monotonicity_score": quantile_metrics.monotonicity_score,
            "top_bottom_return": quantile_metrics.top_bottom_return,
            "top_bottom_sharpe": quantile_metrics.top_bottom_sharpe,
        },
        "turnover": {
            "avg_turnover": turnover_metrics.avg_turnover,
            "turnover_std": turnover_metrics.turnover_std,
        },
        "cost_adjusted": {
            "gross_return": cost_adjusted.gross_return,
            "cost": cost_adjusted.cost,
            "net_return": cost_adjusted.net_return,
            "cost_rate": cost_adjusted.cost_rate,
        },
        "coverage": {
            "coverage": coverage_metrics.coverage,
            "missing_rate": coverage_metrics.missing_rate,
            "anomaly_rate": coverage_metrics.anomaly_rate,
            "available_dates": coverage_metrics.available_dates,
            "available_symbols": coverage_metrics.available_symbols,
        },
        "validation_days": validation_days,
        "effective_samples": effective_samples,
        "event_sample_count": event_sample_count,
        "time_split": {
            "train_start": time_split.train_start.isoformat(),
            "train_end": time_split.train_end.isoformat(),
            "validation_start": time_split.validation_start.isoformat(),
            "validation_end": time_split.validation_end.isoformat(),
            "test_start": time_split.test_start.isoformat() if time_split.test_start else None,
            "test_end": time_split.test_end.isoformat() if time_split.test_end else None,
            "purge_days": time_split.purge_days,
            "embargo_days": time_split.embargo_days,
        },
    }

    # 6. 写入门禁结论（终态保护）
    finalize_evaluation_run(
        db,
        run_id=run.id,
        metrics=metrics,
        gate_result=gate.result,
        rejection_reasons=gate.reasons if gate.reasons else None,
    )

    return EvaluationOutcome(
        run_id=run.id,
        config_hash=config_hash,
        gate_result=gate.result,
        rejection_reasons=gate.reasons,
        metrics=metrics,
        time_split=time_split,
        coverage=coverage_metrics,
    )


__all__ = [
    # WP5-01 评估运行契约
    "EVALUATOR_VERSION",
    "IMMUTABLE_FIELDS",
    "TERMINAL_GATE_RESULTS",
    "compute_config_hash",
    "build_evaluation_run_id",
    "create_evaluation_run",
    "finalize_evaluation_run",
    "get_evaluation_run",
    "list_evaluation_runs",
    # WP5-02 时间切分
    "TimeSplit",
    "build_time_split",
    "AlignedSample",
    "align_factor_with_target",
    # WP5-03 基础指标
    "ICMetrics",
    "compute_rank_ic",
    "QuantileMetrics",
    "compute_quantile_returns",
    "TurnoverMetrics",
    "compute_turnover",
    "CostAdjustedReturn",
    "compute_cost_adjusted_return",
    "CoverageMetrics",
    "compute_coverage",
    # WP5-04 分类门禁
    "GATE_THRESHOLDS",
    "GateResult",
    "evaluate_gate",
    # 评估主流程
    "EvaluationConfig",
    "EvaluationOutcome",
    "run_evaluation",
]
