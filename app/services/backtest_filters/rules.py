"""过滤规则常量、DTO 与异常类型定义。

规则执行顺序：
1. STATUS_UNKNOWN_BLOCK （数据时效完整性）
2. NEW_LISTING_EXCLUDE （次新股）
3. ST_EXCLUDE / ST_KEEP_HOLD （ST 过滤）
4. SUSPENDED_EXCLUDE / SUSPENDED_FREEZE （停牌过滤 + 持仓冻结）
5. DELISTING_PERIOD_EXCLUDE （退市整理期排除新开仓）
6. DELISTED_MANDATORY_LIQUIDATION_CANDIDATE （输出退市清算候选）

- 所有常量值全局唯一。
- DTO 为 Pydantic v2 frozen/serializable。
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ========================================================================
# RULE CODE 常量（值唯一，审计事件 rule_code 严格引用）
# ========================================================================

# --- 数据时效/异常 ---
RULE_STATUS_UNKNOWN_BLOCK = "STATUS_UNKNOWN_BLOCK"
RULE_PRICE_INVALID = "PRICE_INVALID"
RULE_VOLUME_INVALID = "VOLUME_INVALID"
RULE_TARGET_LABEL_MISSING = "TARGET_LABEL_MISSING"

# --- 次新股 ---
RULE_NEW_LISTING_EXCLUDE = "NEW_LISTING_EXCLUDE"
RULE_NEW_LISTING_LISTING_DATE_UNKNOWN = "NEW_LISTING_LISTING_DATE_UNKNOWN"

# --- ST ---
RULE_ST_EXCLUDE = "ST_EXCLUDE"       # 新开仓候选 ST -> 排除
RULE_ST_HOLD_NO_FORCE_LIQUIDATE = "ST_HOLD_NO_FORCE_LIQUIDATE"  # 持仓变 ST，仅事件不动作

# --- 停牌 ---
RULE_SUSPENDED_EXCLUDE = "SUSPENDED_EXCLUDE"    # 候选停牌 -> 排除
RULE_SUSPENDED_FREEZE = "SUSPENDED_FREEZE"      # 持仓停牌 -> 冻结

# --- 退市整理期 ---
RULE_DELISTING_PERIOD_EXCLUDE = "DELISTING_PERIOD_EXCLUDE"

# --- 退市强制清算 ---
RULE_DELISTING_LIQUIDATION_MANDATORY = "DELISTING_LIQUIDATION_MANDATORY"
RULE_DELISTING_PRICE_MISSING = "DELISTING_PRICE_MISSING"

# --- 正常通过 ---
RULE_INCLUDE_NORMAL = "INCLUDE_NORMAL"


# ========================================================================
# ACTION 枚举（写入 backtest_filter_events.action）
# ========================================================================
ACTION_INCLUDE: Literal["include"] = "include"
ACTION_EXCLUDE: Literal["exclude_candidate"] = "exclude_candidate"
ACTION_FREEZE: Literal["freeze_position"] = "freeze_position"
ACTION_FORCE_LIQUIDATE: Literal["force_liquidate"] = "force_liquidate"

FilterAction = Literal[
    "include",
    "exclude_candidate",
    "freeze_position",
    "force_liquidate",
]


# ========================================================================
# 异常类型
# ========================================================================
class FilterError(Exception):
    """过滤治理基类异常（带 rule_code + 证据）。"""

    def __init__(self, rule_code: str, detail: str, evidence: dict | None = None) -> None:
        super().__init__(f"[{rule_code}] {detail}")
        self.rule_code = rule_code
        self.detail = detail
        self.evidence = evidence or {}


class DelistingPriceMissingError(FilterError):
    """退市清算缺少最后有效收盘价 → 回测阻断（status=failed）。"""

    def __init__(self, symbol_id: int, trade_date: date, evidence: dict | None = None) -> None:
        detail = f"symbol_id={symbol_id} trade_date={trade_date.isoformat()} 缺少退市清算所需的最后有效收盘价"
        super().__init__(
            rule_code=RULE_DELISTING_PRICE_MISSING,
            detail=detail,
            evidence=dict(evidence or {}, symbol_id=symbol_id, trade_date=trade_date.isoformat()),
        )
        self.symbol_id = symbol_id
        self.trade_date = trade_date


class StatusUnknownBlockingError(FilterError):
    """PIT 状态为 UNKNOWN 且 production_fidelity=True → 生产阻断。"""

    def __init__(self, symbol_ids: list[int], trade_date: date, evidence: dict | None = None) -> None:
        detail = (
            f"trade_date={trade_date.isoformat()} 有 {len(symbol_ids)} 个 symbol 的证券状态=UNKNOWN，"
            "production_fidelity=True 时禁止进入候选池"
        )
        super().__init__(
            rule_code=RULE_STATUS_UNKNOWN_BLOCK,
            detail=detail,
            evidence=dict(evidence or {}, symbol_ids=symbol_ids, trade_date=trade_date.isoformat()),
        )
        self.symbol_ids = symbol_ids
        self.trade_date = trade_date


# ========================================================================
# DTO 类型
# ========================================================================

class FilterEventDTO(BaseModel):
    """过滤事件 DTO（1 股票日 1+ 事件）。"""
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: int | None = Field(default=None, description="回测 run_id；纯函数阶段可先写 None，持久化时再填")
    trade_date: date
    symbol_id: int
    action: FilterAction
    rule_code: str
    reason: str = Field(min_length=1, description="人类可读中文原因，含数值如 listing_age=119")
    raw_status_json: str = Field(default="{}", description="当日 PIT 状态快照 JSON 字符串")
    effective_status: str = Field(default="LISTED", description="PIT DTO 的 status 值（UNKNOWN/LISTED/ST/...）")
    price_used: float | None = Field(default=None, description="若为清算事件，记录使用的收盘价")
    config_hash: str = Field(default="", max_length=64, description="BacktestFilterConfig hash")
    data_batch_id: str | None = Field(default=None, description="对应 security_status 数据批次")
    # Task 32：结构化证据（legacy_baseline / 审计事件可放任意 JSON）
    evidence: dict | None = Field(default=None, description="结构化 JSON 证据；用于 legacy_mode 等兼容模式标记")


class FrozenPositionInfo(BaseModel):
    """持仓冻结记录（停牌等场景）。"""
    model_config = ConfigDict(frozen=True, extra="forbid")
    symbol_id: int
    reason_rule_code: str
    reason_detail: str
    opening_quantity: float = Field(ge=0, description="期初持仓量（冻结后 closing 应 = opening）")


class DelistingCandidate(BaseModel):
    """退市清算候选（正式摘牌日需要强制平仓的持仓）。"""
    model_config = ConfigDict(frozen=True, extra="forbid")
    symbol_id: int
    trade_date: date
    holding_quantity: float
    # 摘牌前最后有效收盘价（若当前未知则写 None，后续由清算器取价并校验）
    last_valid_close: float | None


class FilterOutcome(BaseModel):
    """apply_daily_filters 输出结构。"""
    model_config = ConfigDict(extra="forbid")

    eligible_candidates: list[int] = Field(
        default_factory=list,
        description="通过全部过滤的候选 symbol_ids（按输入相对顺序稳定排序）",
    )
    frozen_positions: dict[int, FrozenPositionInfo] = Field(
        default_factory=dict,
        description="symbol_id -> 冻结信息；持仓当日不参与买入/卖出撮合",
    )
    delisting_candidates: list[DelistingCandidate] = Field(
        default_factory=list,
        description="需要执行退市清算的持仓列表",
    )
    filter_events: list[FilterEventDTO] = Field(
        default_factory=list,
        description="完整审计事件（按 symbol_id 升序稳定排序，可重放）",
    )


# ========================================================================
# 规则分组（用于开关映射与单开关消融）
# ========================================================================

# 配置字段 -> 该开关控制的 rule_code 集合
FILTER_GATE_RULE_MAP: dict[str, set[str]] = {
    "filter_new_listing": {RULE_NEW_LISTING_EXCLUDE, RULE_NEW_LISTING_LISTING_DATE_UNKNOWN},
    "filter_st": {RULE_ST_EXCLUDE},
    "filter_suspended": {RULE_SUSPENDED_EXCLUDE, RULE_SUSPENDED_FREEZE},
    "delisting_period_excluded": {RULE_DELISTING_PERIOD_EXCLUDE},
    "force_delisting_liquidation": {RULE_DELISTING_LIQUIDATION_MANDATORY},
}
