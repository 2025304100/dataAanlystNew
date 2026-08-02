"""Rolling Ridge training with time-based validation and release gates.

WP7-03: Ridge 样本接线
- 移除模块加载时静态 FEATURE_CODES 作为唯一事实来源
- 支持通过 factor_set_id 从 FactorSetMember 动态读取特征列表、版本、missing_policy
- 不传 factor_set_id 时回退到静态 FEATURE_CODES（向后兼容，legacy 路径）
- 动态特征的 feature_versions 从 FactorSetMember.factor_version 读取
- factor_set_id 记入模型 identity 和 hyperparameters_json 用于追溯

WP7-04: 模型门禁增强
- 验证期 ICIR（Information Ratio）：基于每日 IC 序列计算 mean/std
- 扣成本后收益：由评估流程外部计算后传入（本模块不计算换手率/成本）
- 权重漂移：与当前 active 模型的 normalized_weight 计算 L1 距离
- 簇暴露：按因子分类聚合 normalized_weight，取最大单簇占比
- FactorSet 健康状态：检查成员因子的 ShadowObservation 健康状态
- 所有增强门禁参数为 None 时跳过对应检查（向后兼容）
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Iterable

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_squared_error, r2_score
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.factor_model import (
    FactorModelRun,
    FactorWeightSnapshot,
)
from app.services.factors.definitions import FACTOR_BY_CODE, FACTOR_DEFINITIONS
from app.services.factors.store import FactorWarehouse
from app.services.factors.target_engine import TARGET_CODE


# Legacy 静态特征列表（向后兼容回退路径，不传 factor_set_id 时使用）
FEATURE_CODES = tuple(
    item.code for item in FACTOR_DEFINITIONS if item.model_enabled
)


def _load_features_from_factor_set(
    db: Session,
    factor_set_id: str,
) -> tuple[tuple[str, ...], dict[str, int], dict[str, str]]:
    """从 FactorSet 动态加载特征列表（WP7-03）。

    返回：(feature_codes, feature_versions, missing_policies)
    - feature_codes: 按 display_order 排序的特征代码元组
    - feature_versions: {factor_code: factor_version}
    - missing_policies: {factor_code: missing_policy}

    约束：
    - FactorSet 必须存在且为 frozen 状态（保证成员版本不可变）
    - 只加载 role='feature' 的成员（target/regime 跳过）
    - 至少有 1 个特征成员
    """
    from app.models.factor_evaluation import FactorSet

    factor_set = db.get(FactorSet, factor_set_id)
    if factor_set is None:
        raise ValueError(f"factor_set_not_found:{factor_set_id}")

    if factor_set.status != "frozen":
        raise ValueError(
            f"factor_set_not_frozen:{factor_set_id} status={factor_set.status}"
        )

    feature_members = sorted(
        (m for m in factor_set.members if m.role == "feature"),
        key=lambda m: (m.display_order, m.factor_code),
    )

    if not feature_members:
        raise ValueError(f"factor_set_no_features:{factor_set_id}")

    feature_codes = tuple(m.factor_code for m in feature_members)
    feature_versions = {m.factor_code: m.factor_version for m in feature_members}
    missing_policies = {m.factor_code: m.missing_policy for m in feature_members}

    return feature_codes, feature_versions, missing_policies


# ---------------------------------------------------------------------------
# WP7-04: 增强门禁指标计算辅助函数
# ---------------------------------------------------------------------------

# 自定义因子的默认分类（不在 FACTOR_BY_CODE 中时使用）
_DEFAULT_FACTOR_CATEGORY = "custom"

# 健康状态枚举（与 factor_shadow 模块保持一致，避免循环导入）
_BLOCKED_HEALTH_STATUS = "blocked"


def _get_factor_category(code: str) -> str:
    """获取因子分类（WP7-04: 支持自定义因子，不在 FACTOR_BY_CODE 中时返回默认分类）。"""
    definition = FACTOR_BY_CODE.get(code)
    if definition is not None:
        return definition.category
    return _DEFAULT_FACTOR_CATEGORY


def _compute_validation_icir(
    validation_df: pd.DataFrame,
    feature_list: list[str],
    selected_model: Ridge,
) -> float | None:
    """计算验证期 ICIR（Information Ratio）。

    基于每日 IC 序列：ICIR = mean(daily_ic) / std(daily_ic)。
    - 至少需要 2 个有效交易日且 std > 0 才能计算
    - 不足或退化时返回 None（门禁会按 non_finite 处理）
    """
    if validation_df.empty or not feature_list:
        return None
    daily_ics: list[float] = []
    for trade_date, group in validation_df.groupby("trade_date"):
        if len(group) < 2:
            continue
        x = group[feature_list].to_numpy(dtype=float)
        y = group["target"].to_numpy(dtype=float)
        if not np.isfinite(x).all() or not np.isfinite(y).all():
            continue
        prediction = selected_model.predict(x)
        ic = _safe_correlation(
            pd.Series(prediction), group["target"].reset_index(drop=True)
        )
        if ic is not None and np.isfinite(ic):
            daily_ics.append(ic)
    if len(daily_ics) < 2:
        return None
    ic_array = np.asarray(daily_ics, dtype=float)
    std = float(np.std(ic_array, ddof=1))
    if std <= 1e-12:
        return None
    return float(np.mean(ic_array) / std)


def _compute_weight_drift(
    db: Session,
    current_normalized_weights: dict[str, float],
) -> float | None:
    """计算与当前 active 模型的权重 L1 漂移（WP7-04）。

    - 从 FactorRuntimeState 读取 active_model_run_id
    - 加载该模型的 FactorWeightSnapshot.normalized_weight
    - 对共同特征计算 L1 距离（归一化到 [0, 2] 区间）
    - 无 active 模型或无共同特征时返回 None
    """
    if not current_normalized_weights:
        return None
    # 延迟导入避免循环依赖
    from app.models.factor_runtime import FactorRuntimeState

    state = db.get(FactorRuntimeState, 1)
    if state is None or not state.active_model_run_id:
        return None
    previous_model = db.get(FactorModelRun, state.active_model_run_id)
    if previous_model is None:
        return None
    previous_weights = {
        w.factor_code: float(w.normalized_weight) for w in previous_model.weights
    }
    if not previous_weights:
        return None
    # 对共同特征计算 L1 漂移
    common_codes = set(current_normalized_weights) & set(previous_weights)
    if not common_codes:
        return None
    # 归一化：将两边的共同特征权重各自归一化到 abs_sum=1，再计算 L1 距离
    current_sum = sum(
        abs(current_normalized_weights[c]) for c in common_codes
    )
    previous_sum = sum(abs(previous_weights[c]) for c in common_codes)
    if current_sum <= 1e-12 or previous_sum <= 1e-12:
        return None
    drift = sum(
        abs(current_normalized_weights[c] / current_sum - previous_weights[c] / previous_sum)
        for c in common_codes
    )
    return float(drift)


def _compute_cluster_exposure(
    normalized_weights: dict[str, float],
) -> float | None:
    """计算单簇最大暴露占比（WP7-04）。

    按因子分类（category）聚合 normalized_weight 的绝对值，返回最大簇占比。
    - 无权重或全零时返回 None
    - 返回值 ∈ [0, 1]（按绝对值归一化）
    """
    if not normalized_weights:
        return None
    total = sum(abs(w) for w in normalized_weights.values())
    if total <= 1e-12:
        return None
    cluster_sums: dict[str, float] = {}
    for code, weight in normalized_weights.items():
        category = _get_factor_category(code)
        cluster_sums[category] = cluster_sums.get(category, 0.0) + abs(weight)
    if not cluster_sums:
        return None
    return max(cluster_sums.values()) / total


def _check_factor_set_healthy(
    db: Session,
    factor_set_id: str | None,
) -> bool | None:
    """检查 FactorSet 成员因子的 Shadow 健康状态（WP7-04）。

    - factor_set_id 为 None 时返回 None（未启用检查）
    - 检查每个 feature 成员的最新 ShadowObservation.health_status
    - 任一为 blocked 则返回 False
    - 无观察记录或 FactorSet 不存在则返回 None（未知状态）
    """
    if factor_set_id is None:
        return None
    from app.models.factor_evaluation import FactorSet, ShadowObservation

    factor_set = db.get(FactorSet, factor_set_id)
    if factor_set is None:
        return None
    feature_members = [m for m in factor_set.members if m.role == "feature"]
    if not feature_members:
        return None
    # 查询每个成员的最新 ShadowObservation 健康状态
    blocked_count = 0
    observed_count = 0
    for member in feature_members:
        latest = db.execute(
            select(ShadowObservation)
            .where(ShadowObservation.factor_version_id == member.factor_version_id)
            .order_by(ShadowObservation.trade_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest is not None:
            observed_count += 1
            if latest.health_status == _BLOCKED_HEALTH_STATUS:
                blocked_count += 1
    if observed_count == 0:
        return None
    return blocked_count == 0


@dataclass(frozen=True)
class ModelGate:
    """Ridge 模型门禁配置。

    WP7-04 增强：
    - minimum_validation_icir: 验证期 ICIR 阈值（None=不检查）
    - minimum_cost_adjusted_return: 扣成本后最低收益（None=不检查）
    - max_weight_drift: 与上一 active 模型的权重 L1 漂移上限（None=不检查）
    - max_cluster_exposure: 单簇最大暴露占比（None=不检查）
    - require_factor_set_healthy: 是否要求 FactorSet 所有成员因子健康
    """
    # 基础门禁
    minimum_samples: int = 500
    minimum_symbols: int = 20
    minimum_trade_dates: int = 60
    minimum_validation_dates: int = 20
    minimum_validation_ic: float = 0.0
    # WP7-04 增强门禁
    minimum_validation_icir: float | None = None
    minimum_cost_adjusted_return: float | None = None
    max_weight_drift: float | None = None
    max_cluster_exposure: float | None = None
    require_factor_set_healthy: bool = False


@dataclass
class RidgeTrainingResult:
    model_run_id: str
    status: str
    selected_alpha: float | None
    sample_count: int
    symbol_count: int
    trade_date_count: int
    validation_ic: float | None
    coefficients: dict[str, float] = field(default_factory=dict)
    normalized_weights: dict[str, float] = field(default_factory=dict)
    rejection_reasons: list[str] = field(default_factory=list)
    reused: bool = False


def _safe_correlation(
    left: pd.Series, right: pd.Series, *, method: str = "spearman"
) -> float | None:
    value = left.corr(right, method=method)
    return float(value) if value is not None and np.isfinite(value) else None


def evaluate_model_gate(
    *,
    sample_count: int,
    symbol_count: int,
    trade_date_count: int,
    validation_date_count: int,
    validation_ic: float | None,
    coefficients: Iterable[float] | None,
    gate: ModelGate,
    expected_feature_count: int | None = None,
    # WP7-04 增强门禁参数
    validation_icir: float | None = None,
    cost_adjusted_return: float | None = None,
    weight_drift: float | None = None,
    cluster_exposure: float | None = None,
    factor_set_healthy: bool | None = None,
    normalized_weights: dict[str, float] | None = None,
) -> list[str]:
    """评估模型门禁，返回拒绝原因列表（空列表表示通过）。

    WP7-03: expected_feature_count 支持动态特征数量。
    WP7-04: 增加 ICIR、成本后收益、权重漂移、簇暴露、FactorSet 健康检查。
    所有增强参数为 None 时跳过对应检查（向后兼容）。
    """
    reasons = []
    if sample_count < gate.minimum_samples:
        reasons.append(
            f"sample_count:{sample_count}<{gate.minimum_samples}"
        )
    if symbol_count < gate.minimum_symbols:
        reasons.append(
            f"symbol_count:{symbol_count}<{gate.minimum_symbols}"
        )
    if trade_date_count < gate.minimum_trade_dates:
        reasons.append(
            f"trade_date_count:{trade_date_count}<{gate.minimum_trade_dates}"
        )
    if validation_date_count < gate.minimum_validation_dates:
        reasons.append(
            "validation_date_count:"
            f"{validation_date_count}<{gate.minimum_validation_dates}"
        )
    if validation_ic is None or not np.isfinite(validation_ic):
        reasons.append("validation_ic:non_finite")
    elif validation_ic < gate.minimum_validation_ic:
        reasons.append(
            f"validation_ic:{validation_ic:.6f}<"
            f"{gate.minimum_validation_ic:.6f}"
        )
    if coefficients is None:
        reasons.append("coefficients:missing")
    else:
        coefficient_array = np.asarray(list(coefficients), dtype=float)
        expected_size = (
            expected_feature_count
            if expected_feature_count is not None
            else len(FEATURE_CODES)
        )
        if coefficient_array.size != expected_size:
            reasons.append("coefficients:wrong_size")
        elif not np.isfinite(coefficient_array).all():
            reasons.append("coefficients:non_finite")
        elif float(np.abs(coefficient_array).sum()) <= 1e-12:
            reasons.append("coefficients:all_zero")

    # WP7-04: 验证期 ICIR 门禁
    if gate.minimum_validation_icir is not None:
        if validation_icir is None or not np.isfinite(validation_icir):
            reasons.append("validation_icir:non_finite")
        elif validation_icir < gate.minimum_validation_icir:
            reasons.append(
                f"validation_icir:{validation_icir:.6f}<"
                f"{gate.minimum_validation_icir:.6f}"
            )

    # WP7-04: 扣成本后收益门禁
    if gate.minimum_cost_adjusted_return is not None:
        if cost_adjusted_return is None or not np.isfinite(
            cost_adjusted_return
        ):
            reasons.append("cost_adjusted_return:non_finite")
        elif cost_adjusted_return < gate.minimum_cost_adjusted_return:
            reasons.append(
                f"cost_adjusted_return:{cost_adjusted_return:.6f}<"
                f"{gate.minimum_cost_adjusted_return:.6f}"
            )

    # WP7-04: 权重漂移门禁（与上一 active 模型比较）
    if gate.max_weight_drift is not None:
        if weight_drift is None or not np.isfinite(weight_drift):
            reasons.append("weight_drift:non_finite")
        elif weight_drift > gate.max_weight_drift:
            reasons.append(
                f"weight_drift:{weight_drift:.6f}>"
                f"{gate.max_weight_drift:.6f}"
            )

    # WP7-04: 簇暴露门禁（单簇最大权重占比）
    if gate.max_cluster_exposure is not None:
        if cluster_exposure is None or not np.isfinite(cluster_exposure):
            reasons.append("cluster_exposure:non_finite")
        elif cluster_exposure > gate.max_cluster_exposure:
            reasons.append(
                f"cluster_exposure:{cluster_exposure:.6f}>"
                f"{gate.max_cluster_exposure:.6f}"
            )

    # WP7-04: FactorSet 健康状态门禁
    if gate.require_factor_set_healthy:
        if factor_set_healthy is None:
            reasons.append("factor_set_health:unknown")
        elif not factor_set_healthy:
            reasons.append("factor_set_health:unhealthy")

    return reasons


def _load_samples(
    warehouse: FactorWarehouse,
    *,
    factor_calc_batch_id: str,
    target_calc_batch_id: str,
    data_cutoff_date: date,
    feature_codes: tuple[str, ...],
) -> pd.DataFrame:
    """加载训练样本（WP7-03: 支持动态特征列表）。

    feature_codes: 特征代码元组，决定加载哪些因子值和输出列顺序。
    """
    placeholders = ", ".join("?" for _ in feature_codes)
    with warehouse.connection(read_only=True) as conn:
        factors = conn.execute(
            f"""
            SELECT
                symbol,
                trade_date,
                factor_code,
                factor_version,
                normalized_value
            FROM factor_values
            WHERE calc_batch_id = ?
              AND eligible
              AND factor_code IN ({placeholders})
              AND trade_date <= ?
            """,
            [
                factor_calc_batch_id,
                *feature_codes,
                data_cutoff_date,
            ],
        ).fetchdf()
        targets = conn.execute(
            """
            SELECT
                symbol,
                signal_date,
                exit_date,
                target_value
            FROM factor_targets
            WHERE calc_batch_id = ?
              AND target_code = ?
              AND is_tradable
              AND target_value IS NOT NULL
              AND exit_date <= ?
            """,
            [target_calc_batch_id, TARGET_CODE, data_cutoff_date],
        ).fetchdf()
    if factors.empty or targets.empty:
        return pd.DataFrame(
            columns=["symbol", "trade_date", *feature_codes, "target"]
        )
    factors["trade_date"] = pd.to_datetime(
        factors["trade_date"], errors="coerce"
    ).dt.date
    targets["signal_date"] = pd.to_datetime(
        targets["signal_date"], errors="coerce"
    ).dt.date
    pivot = factors.pivot_table(
        index=["symbol", "trade_date"],
        columns="factor_code",
        values="normalized_value",
        aggfunc="last",
    ).reset_index()
    merged = pivot.merge(
        targets,
        left_on=["symbol", "trade_date"],
        right_on=["symbol", "signal_date"],
        how="inner",
    )
    merged = merged.rename(columns={"target_value": "target"})
    for feature in feature_codes:
        if feature not in merged.columns:
            merged[feature] = np.nan
    return merged[
        ["symbol", "trade_date", *feature_codes, "target", "exit_date"]
    ].dropna(subset=[*feature_codes, "target"])


def _run_id(payload: dict) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]
    return f"ridge-{digest}"


def _result_from_model(model: FactorModelRun) -> RidgeTrainingResult:
    metrics = json.loads(model.metrics_json or "{}")
    hyperparameters = json.loads(model.hyperparameters_json or "{}")
    coefficients = {
        weight.factor_code: float(weight.coefficient)
        for weight in model.weights
    }
    normalized_weights = {
        weight.factor_code: float(weight.normalized_weight)
        for weight in model.weights
    }
    return RidgeTrainingResult(
        model_run_id=model.id,
        status=model.status,
        selected_alpha=hyperparameters.get("selected_alpha"),
        sample_count=model.sample_count,
        symbol_count=model.symbol_count,
        trade_date_count=model.trade_date_count,
        validation_ic=metrics.get("validation_ic"),
        coefficients=coefficients,
        normalized_weights=normalized_weights,
        rejection_reasons=(
            model.rejection_reason.split(";")
            if model.rejection_reason
            else []
        ),
        reused=True,
    )


def train_rolling_ridge(
    db: Session,
    warehouse: FactorWarehouse,
    *,
    factor_calc_batch_id: str,
    target_calc_batch_id: str,
    factor_set_id: str | None = None,
    data_cutoff_date: date | None = None,
    window_days: int = 250,
    validation_days: int = 50,
    alphas: tuple[float, ...] = (0.1, 1.0, 10.0, 100.0),
    gate: ModelGate | None = None,
    model_run_id: str | None = None,
) -> RidgeTrainingResult:
    """Train an immutable candidate model; never activate it automatically.

    WP7-03: Ridge 样本接线
    - factor_set_id 提供时从 FactorSetMember 动态读取特征列表、版本、missing_policy
    - factor_set_id 不提供时回退到静态 FEATURE_CODES（legacy 路径，向后兼容）
    - factor_set_id 记入 identity 用于模型幂等和追溯
    - 特征版本从 FactorSetMember.factor_version 读取（而非 FACTOR_DEFINITIONS）
    """
    if window_days < 2:
        raise ValueError("window_days must be at least 2")
    if validation_days < 1 or validation_days >= window_days:
        raise ValueError("validation_days must be within the training window")
    if not alphas or any(alpha <= 0 for alpha in alphas):
        raise ValueError("alphas must contain positive values")
    active_gate = gate or ModelGate()
    cutoff = data_cutoff_date or date.today()

    # WP7-03: 动态加载特征列表
    if factor_set_id is not None:
        feature_codes, feature_versions, _missing_policies = (
            _load_features_from_factor_set(db, factor_set_id)
        )
    else:
        # Legacy 回退路径：使用静态 FEATURE_CODES
        feature_codes = FEATURE_CODES
        feature_versions = {
            definition.code: definition.version
            for definition in FACTOR_DEFINITIONS
            if definition.code in FEATURE_CODES
        }

    identity = {
        "factor_calc_batch_id": factor_calc_batch_id,
        "target_calc_batch_id": target_calc_batch_id,
        "factor_set_id": factor_set_id,
        "data_cutoff_date": cutoff,
        "window_days": window_days,
        "validation_days": validation_days,
        "alphas": alphas,
        "features": feature_codes,
    }
    run_id = model_run_id or _run_id(identity)
    existing = db.get(FactorModelRun, run_id)
    if existing is not None:
        return _result_from_model(existing)

    samples = _load_samples(
        warehouse,
        factor_calc_batch_id=factor_calc_batch_id,
        target_calc_batch_id=target_calc_batch_id,
        data_cutoff_date=cutoff,
        feature_codes=feature_codes,
    )
    all_dates = sorted(samples["trade_date"].unique()) if not samples.empty else []
    window_dates = all_dates[-window_days:]
    samples = samples[samples["trade_date"].isin(window_dates)].copy()
    validation_dates = window_dates[-validation_days:]
    training_dates = window_dates[:-validation_days]
    train = samples[samples["trade_date"].isin(training_dates)]
    validation = samples[samples["trade_date"].isin(validation_dates)]
    sample_count = len(samples)
    symbol_count = int(samples["symbol"].nunique()) if not samples.empty else 0
    trade_date_count = len(window_dates)

    coefficients: dict[str, float] = {}
    normalized_weights: dict[str, float] = {}
    selected_alpha = None
    validation_ic = None
    # WP7-04 增强门禁指标（默认 None，训练成功后计算）
    validation_icir: float | None = None
    weight_drift: float | None = None
    cluster_exposure: float | None = None
    factor_set_healthy: bool | None = None
    metrics: dict[str, float | int | None] = {
        "validation_ic": None,
        "validation_mse": None,
        "validation_r2": None,
        "train_ic": None,
        "sample_count": sample_count,
        "validation_sample_count": len(validation),
        "validation_date_count": len(validation_dates),
        # WP7-04 增强门禁指标
        "validation_icir": None,
        "weight_drift": None,
        "cluster_exposure": None,
        "factor_set_healthy": None,
    }
    final_intercept = None
    if training_dates and validation_dates and not train.empty and not validation.empty:
        feature_list = list(feature_codes)
        x_train = train[feature_list].to_numpy(dtype=float)
        y_train = train["target"].to_numpy(dtype=float)
        x_validation = validation[feature_list].to_numpy(dtype=float)
        y_validation = validation["target"].to_numpy(dtype=float)
        candidates = []
        for alpha in alphas:
            candidate = Ridge(alpha=float(alpha), fit_intercept=True)
            candidate.fit(x_train, y_train)
            prediction = candidate.predict(x_validation)
            candidates.append(
                (
                    float(mean_squared_error(y_validation, prediction)),
                    float(alpha),
                    candidate,
                    prediction,
                )
            )
        validation_mse, selected_alpha, selected, validation_prediction = min(
            candidates, key=lambda item: (item[0], item[1])
        )
        validation_ic = _safe_correlation(
            pd.Series(validation_prediction), validation["target"].reset_index(drop=True)
        )
        train_prediction = selected.predict(x_train)
        train_ic = _safe_correlation(
            pd.Series(train_prediction), train["target"].reset_index(drop=True)
        )
        validation_r2 = (
            float(r2_score(y_validation, validation_prediction))
            if len(validation) >= 2
            else None
        )
        final_model = Ridge(alpha=selected_alpha, fit_intercept=True)
        final_model.fit(
            samples[feature_list].to_numpy(dtype=float),
            samples["target"].to_numpy(dtype=float),
        )
        final_intercept = float(final_model.intercept_)
        coefficients = {
            code: float(value)
            for code, value in zip(feature_codes, final_model.coef_)
        }
        absolute_sum = sum(abs(value) for value in coefficients.values())
        if absolute_sum > 1e-12:
            normalized_weights = {
                code: value / absolute_sum
                for code, value in coefficients.items()
            }
        metrics.update(
            {
                "validation_ic": validation_ic,
                "validation_mse": validation_mse,
                "validation_r2": validation_r2,
                "train_ic": train_ic,
            }
        )
        # WP7-04: 验证期 ICIR（基于每日 IC 序列）
        validation_icir = _compute_validation_icir(
            validation, feature_list, selected
        )
        metrics["validation_icir"] = validation_icir

    # WP7-04: 权重漂移、簇暴露、FactorSet 健康状态（仅在有权重时计算）
    if normalized_weights:
        weight_drift = _compute_weight_drift(db, normalized_weights)
        cluster_exposure = _compute_cluster_exposure(normalized_weights)
        metrics["weight_drift"] = weight_drift
        metrics["cluster_exposure"] = cluster_exposure
    factor_set_healthy = _check_factor_set_healthy(db, factor_set_id)
    metrics["factor_set_healthy"] = factor_set_healthy

    rejection_reasons = evaluate_model_gate(
        sample_count=sample_count,
        symbol_count=symbol_count,
        trade_date_count=trade_date_count,
        validation_date_count=len(validation_dates),
        validation_ic=validation_ic,
        coefficients=coefficients.values() if coefficients else None,
        gate=active_gate,
        expected_feature_count=len(feature_codes),
        # WP7-04 增强门禁参数
        validation_icir=validation_icir,
        weight_drift=weight_drift,
        cluster_exposure=cluster_exposure,
        factor_set_healthy=factor_set_healthy,
        normalized_weights=normalized_weights,
    )
    status = "rejected" if rejection_reasons else "validated"
    model = FactorModelRun(
        id=run_id,
        model_type="ridge",
        asset_type="stock",
        target_code=TARGET_CODE,
        train_start_date=training_dates[0] if training_dates else None,
        train_end_date=training_dates[-1] if training_dates else None,
        validation_start_date=validation_dates[0]
        if validation_dates
        else None,
        validation_end_date=validation_dates[-1]
        if validation_dates
        else None,
        data_cutoff_at=datetime.combine(cutoff, time.max),
        feature_versions_json=json.dumps(feature_versions, sort_keys=True),
        hyperparameters_json=json.dumps(
            {
                **identity,
                "selected_alpha": selected_alpha,
                "final_intercept": final_intercept,
            },
            sort_keys=True,
            default=str,
        ),
        metrics_json=json.dumps(metrics, sort_keys=True),
        sample_count=sample_count,
        symbol_count=symbol_count,
        trade_date_count=trade_date_count,
        status=status,
        rejection_reason=";".join(rejection_reasons) or None,
    )
    db.add(model)
    if coefficients:
        for feature in feature_codes:
            model.weights.append(
                FactorWeightSnapshot(
                    factor_code=feature,
                    factor_version=feature_versions[feature],
                    coefficient=coefficients[feature],
                    normalized_weight=normalized_weights.get(feature, 0.0),
                    train_ic=_safe_correlation(
                        train[feature], train["target"]
                    )
                    if not train.empty
                    else None,
                    validation_ic=_safe_correlation(
                        validation[feature], validation["target"]
                    )
                    if not validation.empty
                    else None,
                )
            )
    db.flush()
    return RidgeTrainingResult(
        model_run_id=run_id,
        status=status,
        selected_alpha=selected_alpha,
        sample_count=sample_count,
        symbol_count=symbol_count,
        trade_date_count=trade_date_count,
        validation_ic=validation_ic,
        coefficients=coefficients,
        normalized_weights=normalized_weights,
        rejection_reasons=rejection_reasons,
    )
