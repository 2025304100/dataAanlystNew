"""因子候选的 6 类归类（向导 §6.3.3 / §6.5.2；任务 T17）。

分类用途
========
1. **均衡覆盖**（向导 §6.3.4）：经典底座按类别配额分配名额，需要先知道每个候选属于哪类
2. **赛道竞争**（向导 §6.5.2，M2）：按类别分赛道竞争配额

优先级（任务卡硬规则，**不得改动顺序**）
====================================
    quality > valuation > volatility > volume_price > reversal > trend

即一个候选同时满足多类特征时，**取优先级最高的那一类**。
例：`ts_delta_periods(roe_ttm, 4)` 既像「质量」又没有波动特征 → quality；
    `stddev(close, 20)/mean(close, 60)` 有 stddev → volatility（不是 trend）。

为什么必须用「字段 + 算子」联合判定
==============================
- 只用字段判不出 trend/reversal/volatility —— 三者都用 close/high/low
- 只用算子判不出 quality/valuation/volume_price —— 它们靠字段区分
所以规则表是「字段优先（区分数据域）→ 算子兜底（区分价量形态）」。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

# ── 6 类常量 ──
CATEGORY_TREND = "trend"
CATEGORY_REVERSAL = "reversal"
CATEGORY_VOLATILITY = "volatility"
CATEGORY_VALUATION = "valuation"
CATEGORY_QUALITY = "quality"
CATEGORY_VOLUME_PRICE = "volume_price"

CATEGORY_LABELS_ZH: dict[str, str] = {
    CATEGORY_QUALITY: "质量",
    CATEGORY_VALUATION: "估值",
    CATEGORY_VOLATILITY: "波动率",
    CATEGORY_VOLUME_PRICE: "量价",
    CATEGORY_REVERSAL: "反转",
    CATEGORY_TREND: "趋势",
}

#: 归类优先级（高 → 低）。**顺序是契约**，改它等于改归类结果。
CATEGORY_PRIORITY: tuple[str, ...] = (
    CATEGORY_QUALITY,
    CATEGORY_VALUATION,
    CATEGORY_VOLATILITY,
    CATEGORY_VOLUME_PRICE,
    CATEGORY_REVERSAL,
    CATEGORY_TREND,
)
ALL_CATEGORIES: tuple[str, ...] = CATEGORY_PRIORITY

PRIORITY_INDEX: dict[str, int] = {c: i for i, c in enumerate(CATEGORY_PRIORITY)}

# ── 字段 → 类别（数据域判定，优先级相同）──
QUALITY_FIELDS: frozenset[str] = frozenset({
    "roe_ttm", "net_profit", "net_profit_yoy", "gross_margin",
    "asset_turnover", "revenue", "revenue_yoy", "eps",
})
VALUATION_FIELDS: frozenset[str] = frozenset({
    "pe_ttm", "pe", "pb", "ps", "dividend_yield",
    "total_market_cap", "circulating_market_cap",
})
VOLUME_PRICE_FIELDS: frozenset[str] = frozenset({
    "volume", "amount", "turnover_rate", "avg_amount", "avg_volume",
    "avg_turnover_rate",
})
PRICE_FIELDS: frozenset[str] = frozenset({
    "close", "open", "high", "low", "vwap", "pre_close",
})

#: 波动率特征算子（有它就归波动率类）
VOLATILITY_FUNCTIONS: frozenset[str] = frozenset({
    "stddev", "std", "ts_atr", "atr", "variance", "ts_std",
})
#: 趋势特征算子
TREND_FUNCTIONS: frozenset[str] = frozenset({
    "mean", "sma", "ema", "ts_mean", "lowest", "highest", "ts_max", "ts_min",
})


def priority_of(category: str) -> int:
    """优先级序号（越小越优先）；未知类别排最后。"""
    return PRIORITY_INDEX.get(category, len(CATEGORY_PRIORITY))


def is_known_category(category: str) -> bool:
    return category in PRIORITY_INDEX


def sort_by_category_priority(categories: Iterable[str]) -> list[str]:
    """按优先级排序（输入里的未知类别排在最后，保持稳定）。"""
    return sorted(categories, key=priority_of)


# ══════════════════════════════════════════════════════════
# 归类
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Classification:
    """归类结果 + 依据（面板上要能解释「为什么归到这一类」）。"""

    category: str
    reason_zh: str
    matched_rule: str
    #: 该候选同时命中的类别（按优先级排序），供审计/调试
    all_matches: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "category_label_zh": CATEGORY_LABELS_ZH.get(self.category, self.category),
            "reason_zh": self.reason_zh,
            "matched_rule": self.matched_rule,
            "all_matches": list(self.all_matches),
        }


def matched_categories(
    *, fields: Iterable[str] | None = None,
    functions: Iterable[str] | None = None,
    negated: bool = False,
) -> list[str]:
    """列出该候选同时命中的全部类别（已按优先级排序）。

    `negated`：表达式含整体取负（如 `-ts_delta_bars(close,5)`）→ 反转特征。
    """
    fset = {str(f).lower() for f in (fields or [])}
    fnset = {str(f).lower() for f in (functions or [])}

    hits: list[str] = []
    if fset & QUALITY_FIELDS:
        hits.append(CATEGORY_QUALITY)
    if fset & VALUATION_FIELDS:
        hits.append(CATEGORY_VALUATION)
    if fnset & VOLATILITY_FUNCTIONS:
        hits.append(CATEGORY_VOLATILITY)
    if fset & VOLUME_PRICE_FIELDS:
        hits.append(CATEGORY_VOLUME_PRICE)
    if negated:
        hits.append(CATEGORY_REVERSAL)
    # 趋势是兜底类：只要有价格字段（或趋势算子）就算命中，但不主动抢前面的类
    if (fset & PRICE_FIELDS) or (fnset & TREND_FUNCTIONS):
        hits.append(CATEGORY_TREND)

    return sort_by_category_priority(hits)


def classify(
    *, fields: Iterable[str] | None = None,
    functions: Iterable[str] | None = None,
    negated: bool = False,
    declared: str | None = None,
) -> Classification:
    """判定类别：**按优先级取最高的命中类**。

    Args:
        fields: 公式依赖的字段名
        functions: 公式调用的算子名
        negated: 表达式是否整体取负（反转特征）
        declared: 模板/用户显式声明的类别；**只在没有任何命中时**作为兜底
            （声明不得越过优先级规则，否则均衡覆盖的配额会被声明操纵）
    """
    hits = tuple(matched_categories(fields=fields, functions=functions,
                                    negated=negated))
    if hits:
        top = hits[0]
        return Classification(
            category=top,
            reason_zh=_reason_zh(top, fields=fields, functions=functions,
                                 negated=negated, ruled_by_priority=len(hits) > 1),
            matched_rule="priority" if len(hits) > 1 else "single_hit",
            all_matches=hits,
        )
    if declared and is_known_category(declared):
        return Classification(
            category=declared,
            reason_zh=f"无字段/算子特征命中，采用声明类别「{CATEGORY_LABELS_ZH[declared]}」。",
            matched_rule="declared_fallback",
            all_matches=(declared,),
        )
    return Classification(
        category=CATEGORY_TREND,
        reason_zh="无任何特征命中（既无已知字段也无已知算子），按兜底归入趋势类。",
        matched_rule="default_fallback",
        all_matches=(CATEGORY_TREND,),
    )


def _reason_zh(category: str, *, fields: Iterable[str] | None,
               functions: Iterable[str] | None, negated: bool,
               ruled_by_priority: bool) -> str:
    fset = {str(f).lower() for f in (fields or [])}
    fnset = {str(f).lower() for f in (functions or [])}
    hit_desc: list[str] = []
    if category == CATEGORY_QUALITY:
        hit_desc = sorted(fset & QUALITY_FIELDS)
        base = f"依赖财报字段 {hit_desc}"
    elif category == CATEGORY_VALUATION:
        hit_desc = sorted(fset & VALUATION_FIELDS)
        base = f"依赖估值字段 {hit_desc}"
    elif category == CATEGORY_VOLATILITY:
        hit_desc = sorted(fnset & VOLATILITY_FUNCTIONS)
        base = f"使用波动算子 {hit_desc}"
    elif category == CATEGORY_VOLUME_PRICE:
        hit_desc = sorted(fset & VOLUME_PRICE_FIELDS)
        base = f"依赖量价字段 {hit_desc}"
    elif category == CATEGORY_REVERSAL:
        base = "表达式整体取负，表达反转/均值回归含义"
    else:
        hit_desc = sorted(fset & PRICE_FIELDS)
        base = f"使用价格字段 {hit_desc} 且无更强特征"
    suffix = "；同时命中多类，按优先级取最高" if ruled_by_priority else ""
    return base + suffix


def classify_candidate(candidate: Mapping[str, Any]) -> str:
    """便捷入口：从候选记录里取 `fields` / `functions` / `negated` / `category`。"""
    return classify(
        fields=candidate.get("fields") or candidate.get("required_fields") or [],
        functions=candidate.get("functions") or [],
        negated=bool(candidate.get("negated")),
        declared=candidate.get("category"),
    ).category


def count_by_category(items: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    out = {c: 0 for c in ALL_CATEGORIES}
    for it in items:
        out[classify_candidate(it)] = out.get(classify_candidate(it), 0) + 1
    return out


__all__ = [
    "CATEGORY_TREND",
    "CATEGORY_REVERSAL",
    "CATEGORY_VOLATILITY",
    "CATEGORY_VALUATION",
    "CATEGORY_QUALITY",
    "CATEGORY_VOLUME_PRICE",
    "CATEGORY_LABELS_ZH",
    "CATEGORY_PRIORITY",
    "ALL_CATEGORIES",
    "PRIORITY_INDEX",
    "QUALITY_FIELDS",
    "VALUATION_FIELDS",
    "VOLUME_PRICE_FIELDS",
    "PRICE_FIELDS",
    "VOLATILITY_FUNCTIONS",
    "TREND_FUNCTIONS",
    "Classification",
    "priority_of",
    "is_known_category",
    "sort_by_category_priority",
    "matched_categories",
    "classify",
    "classify_candidate",
    "count_by_category",
]
