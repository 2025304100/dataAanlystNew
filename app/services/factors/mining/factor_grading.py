# -*- coding: utf-8 -*-
"""因子质量分级（M2）—— 设计文档 §7.10 / 需求文档 §6.8。

```python
# 纯函数：8 维度指标 → (等级, 理由)。阈值可注入，默认读设置页配置。
def grade(metrics: dict, thresholds: dict | None = None) -> tuple[str, str]: ...
```

**8 个评分维度**：统计显著性、经济显著性（ICIR / 分层收益）、稳定性、覆盖率、
换手率、复杂度、与已有因子相关性、OOS 表现。

**五级阈值「每一级全部满足才定级」**（需求 §6.8 表）：

| 级 | ICIR | 显著性 | Bootstrap CI 下限 | 置换检验 | 覆盖率 | OOS | 相关性 | 换手率 | 复杂度 |
|---|------|--------|------------------|---------|-------|-----|-------|-------|-------|
| S | ≥0.5 | 校正后 p<0.01 | >0.1 | p<0.001 | ≥90% | ≥0.3 且同向 | <0.7 | <30% | ≤4 |
| A | ≥0.3 | 校正后 p<0.05 | >0 | p<0.01 | ≥80% | 同向 | <0.8 | <50% | ≤6 |
| B | ≥0.2 | 原始 p<0.1 | >−0.05 | — | ≥70% | 0<ICIR<0.2 | <0.9 | — | — |
| C | ≥0.15 | p<0.2 | — | — | ≥60% | 样本内有效但 OOS 不稳 | <0.95 | — | — |
| D | 其余（<0.15 / p≥0.2 / 覆盖率<60% / OOS 明显反向 / 相关性≥0.95） |

**硬约束**
- 衰减率 `decay_ratio < 0.5` → **不得评为 B 级以上**（降到 C）；
- 月频（或统计层 `degraded=True`）→ **最高 B 级**；
- `grade_manual_adjusted=1` 的因子**不被季度任务自动覆盖**（`review_quarterly` 判守）。

实现口径：
- **缺失维度一律视为不满足**（不猜、不填零）；
- 判定顺序 S → A → B → C，**第一个全满足即定级**，都不满足为 D；
- 纯函数、无 DB/无随机、完全可复现；阈值结构可整体注入（设置页可调）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

#: 月频（及统计层降级）因子的等级上限（需求 §6.8 月频特例）
MAX_GRADE_MONTHLY = "B"

#: 衰减率阈值：<0.5 疑似过拟合，不得 B 级以上（需求 §3.2 / §6.8 约束）
DECAY_FLOOR = 0.5

#: 定级顺序（从严到宽）
GRADE_ORDER: tuple[str, ...] = ("S", "A", "B", "C", "D")

#: 季度重评：连续多少季满足才触发升降
STABLE_QUARTERS = 2
DECLINE_QUARTERS = 2

#: ICIR 下滑判定阈值（降幅 >30% 计为下滑）
ICIR_DROP_RATIO = 0.3


#: 默认阈值（需求 §6.8 表；`None` 表示该级不检查此维度）
DEFAULT_THRESHOLDS: dict[str, dict[str, float | None]] = {
    "icir": {"S": 0.5, "A": 0.3, "B": 0.2, "C": 0.15, "D": None},
    # 显著性：S/A 看 Bonferroni 校正后 p，B/C 看原始 p
    "p_adj": {"S": 0.01, "A": 0.05, "B": None, "C": None, "D": None},
    "p_raw": {"S": None, "A": None, "B": 0.1, "C": 0.2, "D": None},
    "ci_lower": {"S": 0.1, "A": 0.0, "B": -0.05, "C": None, "D": None},
    "perm_p": {"S": 0.001, "A": 0.01, "B": None, "C": None, "D": None},
    "coverage": {"S": 0.90, "A": 0.80, "B": 0.70, "C": 0.60, "D": None},
    # OOS：S 要求 ≥0.3 且同向；A 要求同向；B 要求 0<ICIR<0.2；C 容忍轻微不稳
    "oos_icir_min": {"S": 0.3, "A": None, "B": 0.0, "C": -0.1, "D": None},
    "oos_icir_max": {"S": None, "A": None, "B": 0.2, "C": None, "D": None},
    "correlation": {"S": 0.7, "A": 0.8, "B": 0.9, "C": 0.95, "D": None},
    "turnover": {"S": 0.30, "A": 0.50, "B": None, "C": None, "D": None},
    "complexity": {"S": 4, "A": 6, "B": None, "C": None, "D": None},
}


def _num(metrics: Mapping[str, Any], key: str) -> float | None:
    """取数值指标；缺失 / None / 非有限值 → None（视为不满足）。"""
    if key not in metrics:
        return None
    raw = metrics[key]
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _ge(value: float | None, threshold: float | None) -> bool:
    if threshold is None:
        return True
    return value is not None and value >= threshold


def _lt(value: float | None, threshold: float | None) -> bool:
    if threshold is None:
        return True
    return value is not None and value < threshold


def _le(value: float | None, threshold: float | None) -> bool:
    if threshold is None:
        return True
    return value is not None and value <= threshold


def _gt(value: float | None, threshold: float | None) -> bool:
    if threshold is None:
        return True
    return value is not None and value > threshold


def _same_direction(a: float | None, b: float | None) -> bool:
    """同向：两者都非空且同号（0 视为非负方向）。"""
    if a is None or b is None:
        return False
    if a == 0 or b == 0:
        return True
    return (a > 0) == (b > 0)


def _meets_level(level: str, metrics: Mapping[str, Any],
                 th: Mapping[str, Mapping[str, float | None]]) -> bool:
    """该级**全部**维度都满足才返回 True（缺失维度视为不满足）。"""
    icir = _num(metrics, "icir")
    p_raw = _num(metrics, "p_value")
    p_adj = _num(metrics, "p_adj_bonferroni")
    ci_lower = _num(metrics, "ci_lower")
    perm_p = _num(metrics, "perm_p_value")
    coverage = _num(metrics, "coverage")
    oos = _num(metrics, "oos_icir")
    corr = _num(metrics, "correlation")
    turnover = _num(metrics, "turnover")
    complexity = _num(metrics, "complexity")

    # 显著性：S/A 用校正后 p（缺失则回退原始 p 也不算——口径从严），B/C 用原始 p
    if level in ("S", "A"):
        if not _lt(p_adj, th["p_adj"].get(level)):
            return False
    else:
        if not _lt(p_raw, th["p_raw"].get(level)):
            return False

    if not _ge(icir, th["icir"].get(level)):
        return False
    if not _gt(ci_lower, th["ci_lower"].get(level)):
        return False
    if not _lt(perm_p, th["perm_p"].get(level)):
        return False
    if not _ge(coverage, th["coverage"].get(level)):
        return False
    if not _lt(corr, th["correlation"].get(level)):
        return False
    if not _lt(turnover, th["turnover"].get(level)):
        return False
    if not _le(complexity, th["complexity"].get(level)):
        return False

    # OOS 表现：区间 + 同向
    if not _gt(oos, th["oos_icir_min"].get(level)):
        return False
    if not _lt(oos, th["oos_icir_max"].get(level)):
        return False
    if level in ("S", "A") and not _same_direction(oos, icir):
        return False
    return True


def decay_cap(grade_value: str, decay_ratio: float | None) -> str:
    """衰减率硬约束：`decay_ratio < 0.5` 的因子不得 B 级以上（降到 C）。"""
    if decay_ratio is None or not math.isfinite(decay_ratio):
        return grade_value
    if decay_ratio >= DECAY_FLOOR:
        return grade_value
    if grade_value in ("S", "A", "B"):
        return "C"
    return grade_value


def monthly_cap(grade_value: str) -> str:
    """月频（或统计层降级）最高 B 级：高于 B 的一律压到 B。"""
    if GRADE_ORDER.index(grade_value) < GRADE_ORDER.index(MAX_GRADE_MONTHLY):
        return MAX_GRADE_MONTHLY
    return grade_value


def grade(
    metrics: Mapping[str, Any],
    thresholds: Mapping[str, Mapping[str, float | None]] | None = None,
) -> tuple[str, str]:
    """8 维度指标 → `(等级, 理由)`（纯函数，阈值可注入）。

    Args:
        metrics: 8 维度指标字典（键见 `DEFAULT_THRESHOLDS` 与模块 docstring），
            另接受 `decay_ratio`、`frequency`、`degraded` 三个约束类字段。
        thresholds: 阈值表；缺省用 `DEFAULT_THRESHOLDS`。

    Returns:
        `(等级, 理由)`。判定顺序 S→A→B→C，第一个全满足即定级；
        再依次施加衰减率约束与月频上限；都不满足为 D。
    """
    th = thresholds if thresholds is not None else DEFAULT_THRESHOLDS
    level = "D"
    for candidate in GRADE_ORDER[:-1]:          # S, A, B, C
        if _meets_level(candidate, metrics, th):
            level = candidate
            break

    reasons = [f"按 8 维度阈值评定为 {level} 级"]

    # 硬约束 1：衰减率 <0.5 不得 B 级以上
    decay = _num(metrics, "decay_ratio")
    if decay is not None and decay < DECAY_FLOOR and level in ("S", "A", "B"):
        level = decay_cap(level, decay)
        reasons.append(f"衰减率 decay_ratio={decay:.3f} < {DECAY_FLOOR}，"
                       f"按约束压至 {level} 级（不得 B 级以上）")

    # 硬约束 2：月频 / 统计层降级 → 最高 B 级
    freq = str(metrics.get("frequency") or "").lower()
    degraded = bool(metrics.get("degraded"))
    if freq == "monthly" or degraded:
        capped = monthly_cap(level)
        if capped != level:
            reasons.append(f"月频（或统计层降级）最高 {MAX_GRADE_MONTHLY} 级，"
                           f"由 {level} 压至 {capped}")
        level = capped

    return level, "；".join(reasons)


@dataclass(frozen=True)
class QuarterlyReview:
    """季度重评结果（需求 §6.8 动态调整）。"""

    grade: str
    action: str                      # promote / demote / keep
    reason: str
    remove_from_factor_set: bool = False


def _next_lower(level: str) -> str:
    idx = GRADE_ORDER.index(level)
    return GRADE_ORDER[min(idx + 1, len(GRADE_ORDER) - 1)]


def review_quarterly(
    *,
    current_grade: str,
    history_grades: Sequence[str] = (),
    current_icir: float | None = None,
    previous_icir: float | None = None,
    oos_reversed: bool = False,
    decline_streak: int = 0,
    manual_adjusted: bool = False,
) -> QuarterlyReview:
    """季度重评（需求 §6.8）：B 连续 2 季稳定升 A；S/A 连续 2 季下滑降级并移出
    FactorSet；**`grade_manual_adjusted=1` 不被自动覆盖**。

    Args:
        current_grade: 本季自动评定等级。
        history_grades: 历史各季等级（**由近到远**）。
        current_icir / previous_icir: 本季与上季 ICIR（用于下滑幅度）。
        oos_reversed: OOS 方向是否反转。
        decline_streak: 已连续下滑季数（由调用方按历史累计）。
        manual_adjusted: 是否人工调整过（`grade_manual_adjusted=1`）。
    """
    # 人工调整优先：季度任务不得覆盖
    if manual_adjusted:
        return QuarterlyReview(
            grade=current_grade, action="keep",
            reason="该因子等级为人工调整（grade_manual_adjusted=1），季度重评不覆盖",
            remove_from_factor_set=False,
        )

    # 下滑判定：ICIR 降幅 >30% 或 OOS 反转
    dropped = False
    if current_icir is not None and previous_icir is not None and previous_icir > 0:
        dropped = (previous_icir - current_icir) / previous_icir > ICIR_DROP_RATIO
    if oos_reversed:
        dropped = True

    declining = dropped and decline_streak >= DECLINE_QUARTERS

    if current_grade in ("S", "A") and declining:
        demoted = _next_lower(current_grade)
        return QuarterlyReview(
            grade=demoted, action="demote",
            reason=(f"{current_grade} 级连续 {decline_streak} 季下滑"
                    f"（ICIR 降幅 >{ICIR_DROP_RATIO:.0%}"
                    f"{' 或 OOS 反转' if oos_reversed else ''}）→ 降为 {demoted} 级"),
            remove_from_factor_set=True,
        )

    # 升级：B 级连续 2 季稳定（无下滑）→ 升 A
    stable = (not dropped) and (not oos_reversed)
    if current_grade == "B" and stable:
        recent = list(history_grades)[:STABLE_QUARTERS]
        if len(recent) >= STABLE_QUARTERS and all(g == "B" for g in recent):
            return QuarterlyReview(
                grade="A", action="promote",
                reason=f"B 级连续 {STABLE_QUARTERS} 季稳定 → 升为 A 级",
                remove_from_factor_set=False,
            )

    return QuarterlyReview(
        grade=current_grade, action="keep",
        reason=f"{current_grade} 级本季无需调整",
        remove_from_factor_set=False,
    )


def run_quarterly_review(
    db: Any,
    *,
    actor: str = "system",
    limit: int | None = None,
) -> dict[str, Any]:
    """季度重评批量执行（供 `scheduled_tasks` 分派调用，**不新建调度器**）。

    逐因子（按 `factor_version_id` 聚合历史）：
    - 取最近 3 条历史（当前 + 前 2 季）计算升降级；
    - **`manual_adjusted=1` 直接跳过**（季度任务不覆盖人工结论）；
    - 每次重评落一行 `source="quarterly"`（含 action 与 remove 标记），保证可追溯；
    - 返回结果统计，交由调度器写入 `ScheduledTaskRun.message`。

    数值列写库前过 `to_db_float`（NaN/±Inf → None，不可归 0）。
    """
    from app.core.db_numeric import to_db_float
    from app.models.factor_grade_history import FactorGradeHistory

    rows = db.query(FactorGradeHistory).all()
    by_version: dict[str, list[Any]] = {}
    for row in rows:
        by_version.setdefault(row.factor_version_id, []).append(row)

    stats = {
        "reviewed": 0, "promoted": 0, "demoted": 0, "kept": 0,
        "skipped_manual": 0, "removed_from_factor_set": 0,
    }

    for version_id, items in by_version.items():
        items.sort(key=lambda r: (r.created_at is None, r.created_at), reverse=True)
        latest = items[0]
        # B2：季度重评消费真实列 —— `grade_manual_adjusted` 以 factor_versions
        # 当前态为准（B1 已落库）；列缺失（老库迁移未执行）回退历史行字段。
        from app.models.factor_model import FactorVersion
        version = db.get(FactorVersion, version_id)
        if version is not None and "grade_manual_adjusted" in \
                FactorVersion.__table__.columns.keys():
            manual = bool(version.grade_manual_adjusted)
        else:
            manual = bool(latest.manual_adjusted)
        if manual:
            stats["skipped_manual"] += 1
            continue

        prev = items[1] if len(items) > 1 else None
        prev_icir = float(prev.icir) if prev is not None and prev.icir is not None else None
        cur_icir = float(latest.icir) if latest.icir is not None else None

        # 连续下滑季数：历史里连续 action=demote 的条数 + 本季是否下滑
        dropped = False
        if cur_icir is not None and prev_icir is not None and prev_icir > 0:
            dropped = (prev_icir - cur_icir) / prev_icir > ICIR_DROP_RATIO
        streak = 0
        for older in items[1:]:
            if older.action == "demote":
                streak += 1
            else:
                break
        if dropped:
            streak += 1

        res = review_quarterly(
            current_grade=str(latest.grade),
            history_grades=[str(x.grade) for x in items[1:STABLE_QUARTERS + 1]],
            current_icir=cur_icir,
            previous_icir=prev_icir,
            decline_streak=streak,
            manual_adjusted=False,
        )

        db.add(FactorGradeHistory(
            id=f"{version_id}-q-{int(len(items))}-{abs(hash((version_id, len(items)))) % 10**8}",
            factor_version_id=version_id,
            grade=res.grade,
            previous_grade=str(latest.grade),
            reason=res.reason,
            metrics_snapshot_json=str(latest.metrics_snapshot_json or "{}"),
            thresholds_source="default",
            source="quarterly",
            action=res.action,
            manual_adjusted=0,
            adjusted_by=actor,
            removed_from_factor_set=1 if res.remove_from_factor_set else 0,
            icir=to_db_float(cur_icir),
            decay_ratio=to_db_float(latest.decay_ratio),
        ))
        # B1：自动评定结果同步落到 `factor_versions` 当前态 4 列（grade() 结果可落库）。
        # 孤立的 history version_id（版本行已被删）→ 尽力跳过，不阻断整批重评。
        try:
            from app.services.factors.mining.service import persist_grade_to_version
            persist_grade_to_version(
                db, factor_version_id=version_id,
                grade_value=res.grade, reason=res.reason,
                metrics={}, manual_adjusted=0,
            )
        except Exception:  # noqa: BLE001 - 当前态落库尽力而为，历史行已保证可追溯
            pass
        stats["reviewed"] += 1
        if res.action == "promote":
            stats["promoted"] += 1
        elif res.action == "demote":
            stats["demoted"] += 1
            if res.remove_from_factor_set:
                stats["removed_from_factor_set"] += 1
        else:
            stats["kept"] += 1

    db.commit()
    if limit:
        stats["limit"] = limit
    return stats


__all__ = [
    "MAX_GRADE_MONTHLY", "DECAY_FLOOR", "GRADE_ORDER", "DEFAULT_THRESHOLDS",
    "QuarterlyReview", "grade", "decay_cap", "monthly_cap", "review_quarterly",
    "run_quarterly_review",
]
