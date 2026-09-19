"""性能探针（SD-v2.0 §7.4 / 向导 §6.11.2；任务 T21）。

**M1 必埋**（任务卡硬规则）：没有逐代探针数据，M3 的「压测后决定 G1（多进程并行）」
就无从谈起。故本模块在 M1 阶段**默认开启**（`enabled=True`），且不引入任何新依赖。

探针口径（与 `factor_mining_generations` 的列**一一对应**，零迁移）
==============================================================
6 个阶段耗时（Integer 毫秒）+ 3 个缓存指标：

| 探针列 | 含义 | 来源 |
|---|---|---|
| `probe_data_load_ms` | 数据加载 | `probe.stage("data_load")` |
| `probe_ast_eval_ms` | AST 求值 | `probe.stage("ast_eval")` |
| `probe_subexpr_compute_ms` | 子表达式计算 | `probe.stage("subexpr_compute")` |
| `probe_factor_assemble_ms` | 因子组装 | `probe.stage("factor_assemble")` |
| `probe_metric_calc_ms` | 指标计算（IC/ICIR/覆盖率/换手） | `probe.stage("metric_calc")` |
| `probe_db_write_ms` | 落库写入 | `probe.stage("db_write")` |
| `probe_subexpr_total` / `probe_subexpr_unique` | 子表达式总数 / 去重后唯一数 | **`absorb_cache(T20 的缓存)`** |
| `probe_g2_hit_rate` | G2 缓存命中率 | 同上 |

另有 G2 抽样校验两列（向导 §6.11.1 每代必写）：`cache_validation_passed` /
`cache_validation_max_diff` ← `absorb_validation(T20 的 verify_sample 结果)`。

三条实现纪律
==========
1. **异常也要计时**：`with probe.stage(...)` 内抛异常时**仍累加耗时**并原样抛出 ——
   否则「失败的那一代」恰好没有数据，而那正是最需要定位的一代。
2. **毫秒取整、非负、非空**：模型列是 `Integer default 0`，故 `to_columns()` 统一
   `max(0, int(round(ms)))`；NaN/无穷也归 0（**绝不写 NULL**，否则前端/统计要处理三种空值）。
3. **缓存三指标从 T20 缓存吸收**，不各算一套（R28）：内部直接调
   `SubexpressionCache.probes()`，口径天然一致。
"""
from __future__ import annotations

import math
import time
from app.core import db_numeric as DN
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Sequence

# ══════════════════════════════════════════════════════════
# 常量：与 factor_mining_generations 的列一一对应
# ══════════════════════════════════════════════════════════

#: 6 个阶段名（顺序即展示顺序）
STAGES: tuple[str, ...] = (
    "data_load", "ast_eval", "subexpr_compute", "factor_assemble",
    "metric_calc", "db_write",
)

#: 阶段 → 模型列名
STAGE_COLUMNS: dict[str, str] = {s: f"probe_{s}_ms" for s in STAGES}

#: 缓存指标列（从 T20 缓存吸收）
CACHE_COLUMNS: tuple[str, ...] = (
    "probe_subexpr_total", "probe_subexpr_unique", "probe_g2_hit_rate",
)

#: G2 抽样校验列（向导 §6.11.1 每代必写）
VALIDATION_COLUMNS: tuple[str, ...] = (
    "cache_validation_passed", "cache_validation_max_diff",
)

#: 本模块负责的全部列（9 探针 + 2 校验）
PROBE_COLUMNS: tuple[str, ...] = tuple(STAGE_COLUMNS.values()) + CACHE_COLUMNS

#: 与阶段耗时无关的「附加列」：总耗时（模型已有该列）
DURATION_COLUMN = "evaluation_duration_ms"

#: 「不可比较」时的 `cache_validation_max_diff` 取值。
#:
#: ⚠️ 必须落在 MySQL **`float`（单精度，可写安全上限 3.4e38）** 的安全区内 ——
#: 本列真实列型是 `float` 而不是 `double`（2026-09-17 在真实 MySQL 5.7 实测）：
#:
#: 1. `float("inf")` **根本发不出去**：PyMySQL 在**客户端**就抛
#:    `ProgrammingError: inf can not be used with MySQL`
#: 2. 曾误用 `1e308` 作哨兵 —— 同样被拒（`DataError 1264 Out of range`）
#:
#: 故哨兵取 `3e38`。**数值写入安全的口径已收敛到 `app/core/db_numeric.py`**
#: （同一套规则全项目共用，R28），本模块只保留「差值语义」的薄封装。
INCOMPARABLE_DIFF = DN.INCOMPARABLE_DIFF

#: MySQL `FLOAT`（单精度）上限 —— 哨兵与任何写入值都必须 ≤ 它。
MYSQL_FLOAT_MAX = DN.MYSQL_FLOAT_MAX


def clamp_to_float_column(value: Any) -> float:
    """**差值语义**的写入前归一化：非负；`NaN`/`-inf` → 0；`inf`/超范围 → 哨兵。

    与 `db_numeric.clamp_to_range` 的区别在于**语义**而非范围：
    本列是「缓存校验的最大绝对差」，负数无意义（→0），而「不可比较」要用
    **哨兵**表达（不是列型上限 3.4e38，以免与「恰好等于上限」混淆）。
    """
    if DN.is_special(value):
        try:
            f = float(value)
        except (TypeError, ValueError):
            return 0.0
        return INCOMPARABLE_DIFF if f > 0 else 0.0      # NaN/-inf → 0
    f = DN.clamp_to_range(value, max_abs=INCOMPARABLE_DIFF)
    return 0.0 if f < 0 else min(f, INCOMPARABLE_DIFF)


def _safe_ms(value: Any) -> int:
    """任意数值 → 非负整数毫秒（NaN/Inf/None → 0）。

    模型列是 `Integer default 0`：**写 NULL 会让「没埋点」与「耗时为 0」不可区分**，
    故这里一律归一到整数。
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0
    if math.isnan(f) or math.isinf(f):
        return 0
    return max(0, int(round(f)))


def _safe_float(value: Any, *, default: float = 0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


@dataclass
class StageTimer:
    """单个阶段的累计计时器（可多次进入同一阶段 → 累加）。"""

    name: str
    total_ms: float = 0.0
    calls: int = 0

    def add(self, ms: float) -> None:
        self.total_ms += max(0.0, _safe_float(ms))
        self.calls += 1

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.name, "total_ms": round(self.total_ms, 3),
                "calls": self.calls}


@dataclass
class GenerationProbe:
    """**一代**的探针收集器（可多次 `stage()` / 累加，最后一次性落库）。

    用法：
        probe = GenerationProbe()
        with probe.stage("data_load"):
            ...
        probe.absorb_cache(cache)              # G2 三指标
        probe.absorb_validation(result)        # 抽样校验两列
        fields = probe.to_columns()            # 直接更新 factor_mining_generations
    """

    enabled: bool = True
    clock: Callable[[], float] = time.perf_counter
    stage_timers: dict[str, StageTimer] = field(default_factory=dict)
    #: 缓存指标（吸收而来）
    subexpr_total: int = 0
    subexpr_unique: int = 0
    g2_hit_rate: float = 0.0
    #: 抽样校验（吸收而来）
    validation_passed: int | None = None
    validation_max_diff: float | None = None

    # ── 计时 ──

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """计时段（**异常也会计时并原样抛出**）。

        `enabled=False` 时零开销：不进计时器、不取时钟。
        """
        if name not in STAGE_COLUMNS:
            raise ValueError(
                f"未知阶段 {name!r}；合法阶段：{list(STAGES)}（新增阶段必须同时加模型列）。")
        if not self.enabled:
            yield
            return

        started = self.clock()
        try:
            yield
        finally:
            # 放在 finally：抛异常的那一代同样要留下耗时数据（最需要定位的一代）
            self.add(name, (self.clock() - started) * 1000.0)

    def add(self, name: str, ms: float) -> None:
        """手工累加某阶段耗时（用于无法用 with 包裹的场景，如异步等待）。"""
        timer = self.stage_timers.get(name)
        if timer is None:
            timer = StageTimer(name=name)
            self.stage_timers[name] = timer
        timer.add(ms)

    def stage_ms(self, name: str) -> float:
        timer = self.stage_timers.get(name)
        return timer.total_ms if timer else 0.0

    # ── 吸收（G2 缓存 / 抽样校验）──

    def absorb_cache(self, cache: Any) -> dict[str, Any]:
        """从 T20 的 `SubexpressionCache` 吸收三个缓存指标。

        内部调用 `cache.probes()` —— **口径由缓存模块定义**，本模块不重算
        （R28：同一口径只有一处实现）。
        """
        probes = {}
        try:
            probes = cache.probes() or {}
        except Exception:  # noqa: BLE001 - 探针不得因吸收失败而中断挖掘
            probes = {}
        self.subexpr_total = int(probes.get("probe_subexpr_total") or 0)
        self.subexpr_unique = int(probes.get("probe_subexpr_unique") or 0)
        self.g2_hit_rate = _safe_float(probes.get("probe_g2_hit_rate"))
        # 子表达式计算耗时**并入对应阶段**（若缓存已给出更精确的数值）
        compute_ms = probes.get("probe_subexpr_compute_ms")
        if compute_ms:
            self.add("subexpr_compute", compute_ms)
        return self.to_columns()

    def absorb_validation(self, result: Mapping[str, Any] | None) -> None:
        """吸收 T20 `verify_sample` 的返回值 → 两个校验列。

        口径：`passed = 1` ⇔ 校验执行过且无不一致；`max_diff` 取最大绝对差
        （无样本时可空 —— 这两列模型允许 NULL，与探针列要求不同）。
        """
        if not result:
            return
        mismatches: Sequence[Mapping[str, Any]] = result.get("mismatches") or []
        checked = int(result.get("checked") or 0)
        self.validation_passed = 1 if (checked > 0 and not mismatches) else 0
        best = 0.0
        for m in mismatches:
            diff = m.get("max_abs_diff")
            if diff is None:
                # 形状不一致 / 缓存缺失 → 不可比较，取哨兵（见 INCOMPARABLE_DIFF 注释：
                # 不能用 inf，否则写库被 MySQL 拒绝）
                best = max(best, INCOMPARABLE_DIFF)
            else:
                best = max(best, _safe_float(diff))
        self.validation_max_diff = best if mismatches else 0.0

    # ── 输出 ──

    @property
    def total_ms(self) -> float:
        return sum(t.total_ms for t in self.stage_timers.values())

    def to_columns(self) -> dict[str, Any]:
        """→ 可直接 `setattr` 到 `factor_mining_generations` 的字典（9 列全给）。

        耗时列一律 `int` 毫秒且非负；`probe_g2_hit_rate` 为 float。
        """
        out: dict[str, Any] = {}
        for stage, column in STAGE_COLUMNS.items():
            out[column] = _safe_ms(self.stage_ms(stage))
        out["probe_subexpr_total"] = max(0, int(self.subexpr_total))
        out["probe_subexpr_unique"] = max(0, int(self.subexpr_unique))
        out["probe_g2_hit_rate"] = _safe_float(self.g2_hit_rate)
        return out

    def to_validation_columns(self) -> dict[str, Any]:
        """→ 抽样校验两列（**未收集时返回空 dict**，不写 NULL 覆盖既有值）。"""
        out: dict[str, Any] = {}
        if self.validation_passed is not None:
            out["cache_validation_passed"] = int(self.validation_passed)
        if self.validation_max_diff is not None:
            # 刻意**不走** `_safe_float`（它把 inf/NaN 归 0，会抹平哨兵语义），
            # 改走 `clamp_to_float_column`：夹到 MySQL FLOAT 安全区。
            out["cache_validation_max_diff"] = clamp_to_float_column(
                self.validation_max_diff)
        return out

    def to_generation_fields(self) -> dict[str, Any]:
        """→ 落库字段集合（9 探针 + 2 校验 + 总耗时）。"""
        out = self.to_columns()
        out.update(self.to_validation_columns())
        out[DURATION_COLUMN] = _safe_ms(self.total_ms)
        return out

    def as_dict(self) -> dict[str, Any]:
        """→ 人读的诊断视图（含每阶段调用次数，便于发现「某阶段被调用 N 次」）。"""
        return {
            "enabled": self.enabled,
            "stages": {s: t.to_dict() for s, t in self.stage_timers.items()},
            "stage_ms": {s: round(self.stage_ms(s), 3) for s in STAGES},
            "total_ms": round(self.total_ms, 3),
            "subexpr_total": self.subexpr_total,
            "subexpr_unique": self.subexpr_unique,
            "g2_hit_rate": round(self.g2_hit_rate, 6),
            "validation_passed": self.validation_passed,
            "validation_max_diff": self.validation_max_diff,
        }


# ══════════════════════════════════════════════════════════
# 便捷入口
# ══════════════════════════════════════════════════════════


def new_probe(*, enabled: bool = True,
              clock: Callable[[], float] = time.perf_counter) -> GenerationProbe:
    """创建探针（`enabled` 默认 True —— M1 必埋）。"""
    return GenerationProbe(enabled=enabled, clock=clock)


@contextmanager
def measure_stage(probe: GenerationProbe | None, name: str) -> Iterator[None]:
    """无探针时静默跳过（让调用方不必到处判空）。"""
    if probe is None:
        yield
        return
    with probe.stage(name):
        yield


def import_stdlib_only_check() -> list[str]:
    """自检辅助：返回本模块**非 stdlib 且非本项目**的顶层 import（应为空）。

    用 `sys.modules` 的 `__file__` 判断来源，避免引入 `modulefinder` 之类重家伙。
    """
    import sys

    bad: list[str] = []
    this = sys.modules[__name__]
    for name in ("math", "time", "contextlib", "dataclasses", "typing"):
        mod = getattr(this, name, None)
        if mod is None:
            continue
        path = getattr(mod, "__file__", "") or ""
        lowered = path.lower()
        if ("site-packages" in lowered or "lib\\site" in lowered):
            bad.append(name)
    return bad


def generation_model_columns() -> tuple[str, ...]:
    """模型 `factor_mining_generations` 的**全部**列名。

    延迟 import：本模块在探针热路径上被调用，不应把 ORM 拖进 import 期。
    """
    from app.models.factor_mining import FactorMiningGeneration

    return tuple(c.name for c in FactorMiningGeneration.__table__.columns)


def generation_probe_columns() -> tuple[str, ...]:
    """模型里**探针相关**的列（`probe_*` / `cache_validation_*`）。

    用于「反向覆盖」守卫：模型有列但探针没埋 → 该列永远是默认值，事后无法回填。
    """
    return tuple(n for n in generation_model_columns()
                 if n.startswith("probe_") or n.startswith("cache_validation_"))


def validate_columns_against_model() -> None:
    """**逐名核对**：本模块输出的每个列都必须在模型里真实存在。

    （R30 的同类守卫：按名字引用别处定义的列，拼错会让写入静默失效
     或抛 `TypeError`，而不是「少了一列」这种可察觉的形态。）
    """
    # ⚠️ 必须用**全部列**校验：`DURATION_COLUMN`（evaluation_duration_ms）不以
    # `probe_` 开头，用「探针前缀列」校验会把它误判为不存在（第一版即此错）。
    model_cols = set(generation_model_columns())
    mine = set(PROBE_COLUMNS) | set(VALIDATION_COLUMNS) | {DURATION_COLUMN}
    unknown = sorted(mine - model_cols)
    if unknown:
        raise ValueError(
            f"探针列在模型 `factor_mining_generations` 中不存在：{unknown}；"
            "新增探针必须同时加模型列（并写迁移）。")


def write_generation_probe(
    db: Any, *, run_id: str, generation: int, probe: GenerationProbe,
    extra_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把探针写入该代的行（**不新建行** —— 代际行由 GA 主循环创建）。

    Raises:
        ValueError: 该 (run_id, generation) 行不存在，或有列名不在模型里。
    """
    from sqlalchemy import select

    from app.models.factor_mining import FactorMiningGeneration

    validate_columns_against_model()

    row = db.execute(
        select(FactorMiningGeneration).where(
            FactorMiningGeneration.run_id == str(run_id),
            FactorMiningGeneration.generation == int(generation),
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValueError(
            f"代际行不存在：run_id={run_id!r} generation={generation}；"
            "探针只负责写入既有行，创建行属 GA 主循环（T23）。")

    fields = probe.to_generation_fields()
    if extra_fields:
        model_cols = {c.name for c in FactorMiningGeneration.__table__.columns}
        unknown = [k for k in extra_fields if k not in model_cols]
        if unknown:
            raise ValueError(f"extra_fields 含模型不存在的列：{unknown}")
        fields.update(dict(extra_fields))

    for key, value in fields.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return fields


__all__ = [
    "STAGES",
    "STAGE_COLUMNS",
    "CACHE_COLUMNS",
    "VALIDATION_COLUMNS",
    "PROBE_COLUMNS",
    "DURATION_COLUMN",
    "INCOMPARABLE_DIFF",
    "MYSQL_FLOAT_MAX",
    "clamp_to_float_column",
    "generation_model_columns",
    "StageTimer",
    "GenerationProbe",
    "new_probe",
    "measure_stage",
    "generation_probe_columns",
    "validate_columns_against_model",
    "write_generation_probe",
    "import_stdlib_only_check",
    "_safe_ms",
    "_safe_float",
]
