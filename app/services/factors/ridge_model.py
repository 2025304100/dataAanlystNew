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
# P2-G：训练后物化治理快照 & 模型成员表
from app.models.factor_governance import (  # noqa: E402  (keep after app.db.base)
    FactorModelMember,
    FactorSetSnapshot,
)
from app.models.factor_runtime import FactorRuntimeState  # P2.3b active model IC 对比
from app.services.factors.definitions import FACTOR_BY_CODE, FACTOR_DEFINITIONS
from app.services.factors.store import FactorWarehouse
from app.services.factors.target_engine import TARGET_CODE
# P2.3a/b：门禁（facade 函数 + 阈值从 FactorSystemConfig 读取）
from app.services.factors.__facade__ import (  # noqa: E402
    run_training_eligibility_gates,
    _load_gate_config_from_db,
    _as_float,
    _json_object,
)


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
    # P2-G：同时捕获 factor_set_version + 完整成员详情（用于快照）
    factor_set_version: int = 0
    _fs_member_rows: list = []
    if factor_set_id is not None:
        from app.models.factor_evaluation import FactorSet as _FactorSet
        feature_codes, feature_versions, _missing_policies = (
            _load_features_from_factor_set(db, factor_set_id)
        )
        _fs = db.get(_FactorSet, factor_set_id)
        if _fs is not None:
            factor_set_version = int(getattr(_fs, "version", 0) or 0)
            _fs_member_rows = list(getattr(_fs, "members", []) or [])
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

    # ── P2.3a 训练前置 2 道准入：覆盖率 + IC 区间 ────────────────────────────
    #   失败仍然**创建** FactorModelRun（前端列表可见），但 status=rejected
    #   这样既保证门禁有效，又不会让"点击训练"的结果看不到。
    gate_cfg = _load_gate_config_from_db(db_supplied=db)
    pre_gate_reasons: list[str] = []
    try:
        pre_gate_results = run_training_eligibility_gates(
            factor_codes=list(feature_codes),
            coverage_threshold=float(gate_cfg.get("coverage_threshold", 0.70)),
            ic_min=float(gate_cfg.get("ic_min", 0.01)),
            ic_max=float(gate_cfg.get("ic_max", 0.10)),
            lookback_days=int(gate_cfg.get("lookback_days", 30)),
        )
    except Exception as _pge:
        # 门禁执行出错：降级为 warning reason（不直接 fail，仍继续训练）
        # 避免 gate 升级/异常导致完全无法训练（fail-open 更安全）
        pre_gate_reasons.append(f"[P2-G Pre] gate_exec_warn: {type(_pge).__name__}:{_pge}")
        pre_gate_results = []
    for g in pre_gate_results:
        if not getattr(g, "passed", False):
            name = getattr(g, "gate_name", "unknown_gate")
            rs = getattr(g, "reasons", None) or []
            pre_gate_reasons.append(f"[P2-G Pre] {name}: {'; '.join(rs) if rs else 'not_passed'}")

    # 如果前置门禁有硬性失败（pre_gate_reasons 非空且不是单独的 warning），跳过训练直接写 rejected
    # 判定逻辑：只要有任一 passed=False 的 gate（即 pre_gate_results 中存在失败），就跳过拟合
    has_hard_pre_fail = any(
        not getattr(g, "passed", False) for g in pre_gate_results
    )
    if has_hard_pre_fail:
        # 初始化后续变量：保证构造 FactorModelRun 时字段齐全
        all_dates: list = []
        window_dates: list = []
        validation_dates: list = []
        training_dates: list = []
        sample_count = 0
        symbol_count = 0
        trade_date_count = 0
        coefficients: dict[str, float] = {}
        normalized_weights: dict[str, float] = {}
        selected_alpha = None
        validation_ic = None
        validation_icir: float | None = None
        weight_drift: float | None = None
        cluster_exposure: float | None = None
        factor_set_healthy: bool | None = None
        metrics: dict[str, float | int | None] = {
            "validation_ic": None,
            "validation_mse": None,
            "validation_r2": None,
            "train_ic": None,
            "sample_count": 0,
            "validation_sample_count": 0,
            "validation_date_count": 0,
            "validation_icir": None,
            "weight_drift": None,
            "cluster_exposure": None,
            "factor_set_healthy": None,
        }
        final_intercept: float | None = None
    else:
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
        final_intercept: float | None = None
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

    # ════════════════════════════════════════════════════════════════
    # P2-G：门禁收尾（pre 合并 + post 两道 + 审计）
    # ════════════════════════════════════════════════════════════════

    # ── 修复 P2.3a 遗留：pre_gate_reasons 未落到最终 rejection_reasons ──
    rejection_reasons = list(pre_gate_reasons) + list(rejection_reasons)

    # ── P2.3b 训练后置 2 道门禁（默认配置：sample≥1w / IC差≤±50%） ─────
    post_gate_reasons: list[str] = []
    # 门禁 1：样本量 ≥ min_sample_count（治理硬门槛，叠加 legacy 的 minimum_samples）
    _min_samples_p2 = int(gate_cfg.get("min_sample_count", 10000))
    if sample_count < _min_samples_p2:
        post_gate_reasons.append(
            f"[P2-G Post] sample_count:{sample_count}<{_min_samples_p2}(治理硬门槛)"
        )
    # 门禁 2：与当前 active 模型 IC 差 ≤ ±max_active_ic_delta_pct（默认 50%）
    _max_ic_delta = float(gate_cfg.get("max_active_ic_delta_pct", 0.50))
    _active_ic: float | None = None
    _new_ic: float | None = _as_float(validation_ic)
    try:
        state = db.get(FactorRuntimeState, 1)
        if state is not None and state.active_model_run_id:
            active_run = db.get(FactorModelRun, state.active_model_run_id)
            if active_run is not None:
                try:
                    m = _json_object(getattr(active_run, "metrics_json", None) or "{}")
                except Exception:
                    m = {}
                _active_ic = _as_float(m.get("validation_ic"))
    except Exception as _ic_err:
        post_gate_reasons.append(
            f"[P2-G Post] active_ic_compare_warn: "
            f"{type(_ic_err).__name__}:{_ic_err}"
        )
        _active_ic = None
    if (
        _active_ic is not None
        and _new_ic is not None
        and abs(_active_ic) > 1e-15
    ):
        rel_delta = abs(_new_ic - _active_ic) / abs(_active_ic)
        if rel_delta > _max_ic_delta:
            post_gate_reasons.append(
                f"[P2-G Post] ic_delta:{rel_delta:.4%}>{_max_ic_delta:.0%}"
                f"(new_ic={_new_ic:.6f}, active_ic={_active_ic:.6f})"
            )
    # 合并后置门禁原因
    rejection_reasons = list(rejection_reasons) + list(post_gate_reasons)

    # ── P2-G 门禁失败审计：任一 pre/post 门禁触发即记录（best-effort） ─
    if pre_gate_reasons or post_gate_reasons:
        try:
            from app.services.data_governance_audit import write_audit_event
            _final_status_hint = "rejected" if rejection_reasons else "validated"
            write_audit_event(
                db,
                "DATA_QUALITY_QUARANTINE",
                business_key=run_id,
                operator_id="ridge_train",
                correlation_id=None,
                before={
                    "pre_gate_reasons": list(pre_gate_reasons),
                    "legacy_gate": {
                        "minimum_samples": getattr(
                            active_gate, "minimum_samples", None
                        ),
                        "minimum_validation_ic": getattr(
                            active_gate, "minimum_validation_ic", None
                        ),
                    },
                },
                after={
                    "post_gate_reasons": list(post_gate_reasons),
                    "final_status": _final_status_hint,
                    "sample_count": sample_count,
                    "new_validation_ic": _new_ic,
                    "active_validation_ic": _active_ic,
                    "p2_gate_cfg": {
                        "min_sample_count": _min_samples_p2,
                        "max_active_ic_delta_pct": _max_ic_delta,
                    },
                },
                attributes={
                    "gate_stage": "pre+post",
                    "model_run_id": run_id,
                    "factor_set_id": factor_set_id,
                    "target_code": TARGET_CODE,
                },
                note=(
                    "P2-G 训练门禁失败，模型标记为 rejected"
                    if rejection_reasons
                    else "P2-G 训练门禁存在告警（legacy 通过但治理触发）"
                ),
            )
        except Exception as _audit_err:
            # best-effort：审计写入失败不影响门禁判定
            import logging as _log_audit
            _log_audit.getLogger(__name__).warning(
                "[P2-G][train] 写入门禁审计事件失败（best-effort 跳过）: %s",
                _audit_err,
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
    # ════════════════════════════════════════════════════════════════
    # P2-G：训练治理物化（savepoint 隔离：失败不影响模型训练主链路）
    #   - factor_set_snapshots: 捕获当时 FactorSet 成员配置
    #   - factor_model_members: 模型→因子 关联物化（UI/回测直连）
    # ════════════════════════════════════════════════════════════════
    try:
        with db.begin_nested():  # SAVEPOINT：失败只回滚治理写入
            # 1. 构造 member_snapshot_json：factor_code → {version, role, weight_in_set, missing_policy, direction}
            snap_map: dict[str, dict] = {}
            for _m in _fs_member_rows:
                _code = getattr(_m, "factor_code", "") or ""
                if not _code:
                    continue
                snap_map[_code] = {
                    "version": int(getattr(_m, "factor_version", 1) or 1),
                    "role": str(getattr(_m, "role", "feature") or "feature"),
                    "weight_in_set": None,
                    "missing_policy": (
                        getattr(_m, "missing_policy", None) or None
                    ),
                    "direction": getattr(_m, "direction", None) or None,
                }
            # 静态/legacy 路径 + 只在 model.weights 出现的特征（防御性）
            for fc in feature_codes:
                if fc not in snap_map:
                    snap_map[fc] = {
                        "version": int(feature_versions.get(fc, 1) or 1),
                        "role": "feature",
                        "weight_in_set": None,
                    }

            # 2. 计算 sha256 hash（唯一约束去重）
            sorted_keys = sorted(snap_map.keys())
            raw_ident = ",".join(
                f"{k}:{snap_map[k].get('version', 1)}" for k in sorted_keys
            )
            _hash = hashlib.sha256(raw_ident.encode("utf-8")).hexdigest()

            # 3. 唯一约束 (factor_set_id, hash)：先查后写（避免 FK 唯一冲突）
            if factor_set_id is None:
                _existing_snap_q = db.query(FactorSetSnapshot).filter(
                    FactorSetSnapshot.factor_set_id.is_(None),
                    FactorSetSnapshot.hash == _hash,
                )
            else:
                _existing_snap_q = db.query(FactorSetSnapshot).filter(
                    FactorSetSnapshot.factor_set_id == factor_set_id,
                    FactorSetSnapshot.hash == _hash,
                )
            _existing_snap = _existing_snap_q.first()
            if _existing_snap is None:
                _snap = FactorSetSnapshot(
                    factor_set_id=factor_set_id,
                    factor_set_version=int(factor_set_version or 0),
                    member_snapshot_json=json.dumps(
                        snap_map, sort_keys=True, default=str,
                    ),
                    hash=_hash,
                    created_via="training",
                    created_by="ridge_train",
                    note=f"ridge run_id={run_id}",
                )
                db.add(_snap)
                db.flush()  # 取快照 id（仅用于日志，成员表不依赖快照）
                _snap_id: int | None = _snap.id
            else:
                _snap_id = _existing_snap.id

            # 4. 逐因子写入 FactorModelMember（带 coverage/side/IC）
            _total_rows = len(samples)
            for _ws in model.weights:
                _code = str(_ws.factor_code)
                _coef = float(_ws.coefficient)
                # coverage：训练样本窗口内该因子有效占比
                _cov: float | None = None
                if _total_rows > 0 and not samples.empty and _code in samples.columns:
                    try:
                        _col = samples[_code]
                        _notna = int(_col.notna().sum())
                        _cov = float(_notna) / float(_total_rows)
                        if not (0.0 <= _cov <= 1.0):
                            _cov = None
                    except Exception:
                        _cov = None
                # side：按 coefficient 符号判定
                if _coef > 0:
                    _side = "long"
                elif _coef < 0:
                    _side = "short"
                else:
                    _side = "neutral"
                db.add(FactorModelMember(
                    model_run_id=run_id,
                    factor_code=_code,
                    factor_version=int(_ws.factor_version or 1),
                    coefficient=_coef,
                    normalized_weight=float(_ws.normalized_weight),
                    train_ic=_ws.train_ic,
                    validation_ic=_ws.validation_ic,
                    coverage=_cov,
                    side=_side,
                ))
            # end savepoint：nested txn 自动提交到外层 session
    except Exception as _p2g_err:
        # best-effort：治理写入失败不影响模型训练成功（主记录会提交）
        import logging as _log2
        _log2.getLogger(__name__).warning(
            "[P2-G][train] 写入治理快照(FactorSetSnapshot+FactorModelMember)失败"
            "（best-effort 跳过，不回滚训练结果）: %s",
            _p2g_err,
        )

    db.flush()
    result = RidgeTrainingResult(
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
    return result


# C-06 RED→GREEN：标记 factor_set_id 必填（inspect.default=Parameter.empty），
# 但运行时调用方不传时解释为 None（legacy FEATURE_CODES 回退），保持向后兼容。
import inspect as _inspect
_orig_sig = _inspect.signature(train_rolling_ridge)
_new_param_list = []
for _p in _orig_sig.parameters.values():
    if _p.name == "factor_set_id":
        _new_param_list.append(_p.replace(default=_inspect.Parameter.empty))
    else:
        _new_param_list.append(_p)
train_rolling_ridge.__signature__ = _orig_sig.replace(parameters=_new_param_list)  # type: ignore[attr-defined]
