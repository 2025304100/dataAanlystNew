from __future__ import annotations
import hashlib
import json
from dataclasses import asdict, dataclass, field, FrozenInstanceError
from typing import Tuple, Literal


@dataclass(frozen=True)
class BacktestFilterConfig:
    # 次新股过滤
    filter_new_listing: bool = True
    min_listing_age_calendar_days: int = 120
    # ST 过滤
    filter_st: bool = True
    # 停牌过滤
    filter_suspended: bool = True
    # 退市强制清算
    force_delisting_liquidation: bool = True
    # 退市整理期排除
    delisting_period_excluded: bool = True
    # 最小历史天数
    min_history_days: int = 20
    # 生产保真模式（True => 核心规则关闭时自动置 False）
    production_fidelity: bool = True
    # 灰度诊断-only 模式：内部使用不对外 API 暴露
    diagnostic_only: bool = False
    # Task 32.1: 引擎兼容版本（filter_v1 正常 / legacy_baseline 只读回放）
    engine_compat_version: Literal["filter_v1", "legacy_baseline"] = "filter_v1"

    # 四项核心规则字段名集合（供 validate_production_fidelity 用）
    CORE_RULE_FIELDS: Tuple[str, ...] = field(default=(
        "filter_new_listing",
        "filter_st",
        "filter_suspended",
        "delisting_period_excluded",
    ), init=False, repr=False, compare=False)


def compute_config_hash(cfg: BacktestFilterConfig, schema_version: int = 2) -> str:
    """字段排序 -> 规范化 JSON -> SHA256 -> 64 位 hex。

    - 排除 diagnostic_only（灰度内部，不影响 config_hash 对外一致性）
    - schema_version == 1：忽略 engine_compat_version 字段（旧算法，
      用于兼容 Task32 前的 baseline fixture hash）
    - schema_version == 2（默认）：包含 engine_compat_version，
      即 Task 32.1 canonical 行为：legacy 模式哈希变化，
      可区分 baseline 回放和正式引擎的产物。
    """
    d = asdict(cfg)
    d.pop("diagnostic_only", None)
    if schema_version == 1:
        d.pop("engine_compat_version", None)
    # 键名排序 + None 移除 + 值序列化（日期转字符串）
    normalized = {}
    for k in sorted(d.keys()):
        v = d[k]
        if v is None:
            continue
        normalized[k] = v
    canonical = json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_production_fidelity(cfg: BacktestFilterConfig) -> Tuple[bool, str]:
    """校验生产保真。返回 (ok, non_fidelity_reason)。

    Task 32.1 规则：
      - 若 engine_compat_version == "legacy_baseline"：
        直接返回 (False, "legacy_baseline mode enabled, production fidelity disabled")
      - 否则：当 production_fidelity=True 时，四项核心规则必须全部开启。
    """
    if cfg.engine_compat_version == "legacy_baseline":
        return False, "legacy_baseline mode enabled, production fidelity disabled"
    if not cfg.production_fidelity:
        # 调用方已明确关掉生产保真，不再追加原因
        return True, ""
    reasons = []
    for field_name in BacktestFilterConfig.CORE_RULE_FIELDS:
        enabled = getattr(cfg, field_name, None)
        if enabled is False:
            reasons.append(f"{field_name} disabled")
    if reasons:
        return False, "; ".join(reasons)
    return True, ""
