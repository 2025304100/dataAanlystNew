"""G2 公共子表达式缓存（SD-v2.0 §7.3 / 向导 §6.11.1；任务 T20）。

流程（向导 §6.11.1 五步）
======================
1. **AST 遍历提取**：递归提取所有子表达式（含叶子字段引用），后序（子→根）
2. **SHA-256 强哈希去重**：算子 + 参数 + 字段 + **数据快照版本号** 序列化后算 SHA-256
3. **向量化批量计算**：由调用方注入 `compute_fn`（本模块不做数值实现，只管缓存语义）
4. **主进程内存缓存**：**不写 DuckDB**（任务卡「明确不做」——规避单写锁），
   生命周期与挖掘任务绑定
5. **组装时递归查缓存**：命中即取，**不触发新计算**（`assemble` 恒 0 次 compute）

正确性兜底（向导 §6.11.1 硬要求，全部落实）
========================================
| 风险 | 本模块的措施 |
|---|---|
| 哈希碰撞 / 语义不等价 | **SHA-256**（任务卡：禁用内置 `hash()`，有随机化种子）；`verify_sample` 每代抽若干因子与直接计算对比，差异 > `1e-6` → 报警 + **废弃缓存** |
| 缓存污染 | `put` 检查 NaN/Inf/空值比例，异常 → 不写值、标 `failed`；记录带 `status`（valid/failed/computing） |
| 版本不兼容 | 缓存带 `schema_version` + `hash_algorithm_version`；`from_snapshot` 不匹配 → 废弃并给原因 |

生命周期与隔离
============
- 预筛阶段与全量验证阶段**分别建缓存**（数据范围不同，不混用）：
  `CacheRegistry.for_scope(...)` 保证每个 scope 一个实例，跨 scope 使用抛
  `ScopeMismatchError`。
- 缓存键**含数据快照版本号** → 换快照自动全部 miss（不会拿到旧数据的结果）。

复用而非重写（R28）
================
- 子表达式**规范化**（交换律等）复用 `dedup.canonicalize_formula`
- 规范化序列化复用 `config_hash.canonical_config_payload`
本模块只新增：AST 遍历、SHA-256 全量摘要、状态机、采样校验、探针。
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from app.services.factors.mining import config_hash as CH
from app.services.factors.mining import dedup as DD

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
# 版本常量
# ══════════════════════════════════════════════════════════

#: 缓存结构版本（结构变更必须 +1，否则旧快照会被误用）
SCHEMA_VERSION = 1

#: 哈希算法版本（算法/序列化口径变更必须改，恢复时不匹配即废弃）
HASH_ALGORITHM_VERSION = "sha256-full-v1"

# ── 缓存作用域（预筛 / 全量验证，**不混用**）──
SCOPE_PRESCREEN = "prescreen"
SCOPE_FULL_VALIDATION = "full_validation"
VALID_SCOPES: tuple[str, ...] = (SCOPE_PRESCREEN, SCOPE_FULL_VALIDATION)

# ── 记录状态 ──
STATUS_VALID = "valid"
STATUS_FAILED = "failed"
STATUS_COMPUTING = "computing"
VALID_STATUSES: tuple[str, ...] = (STATUS_VALID, STATUS_FAILED, STATUS_COMPUTING)

#: NaN 比例上限（超过即视为污染，不写缓存）。可配置。
DEFAULT_MAX_NAN_RATIO = 0.5

#: 采样校验容差（向导 §6.11.1：差异 > 1e-6 报警并废弃缓存）
VERIFY_TOLERANCE = 1e-6

#: 每代采样校验的因子数（向导 §6.11.1：「随机抽 5 个因子」）
VERIFY_SAMPLE_SIZE = 5


class UnsupportedAstError(ValueError):
    """遇到无法渲染的 AST 节点（调用方应退回「整式单子表达式」）。"""


class CacheMissError(Exception):
    """组装时缺少可用子表达式（调用方应重算，**不得静默产出错误因子**）。"""

    def __init__(self, missing: Sequence[str], *, reason: str = "") -> None:
        self.missing = list(missing)
        super().__init__(
            f"缓存缺少 {len(self.missing)} 个子表达式（{reason or '未计算或已失败'}）："
            f"{self.missing[:3]}{'...' if len(self.missing) > 3 else ''}")


class ScopeMismatchError(Exception):
    """跨作用域使用缓存（预筛缓存与全量验证缓存**不得混用**）。"""


# ══════════════════════════════════════════════════════════
# AST 渲染（把编译器 AST 还原成公式文本）
# ══════════════════════════════════════════════════════════

_BIN_OP_SYMBOL: dict[str, str] = {
    "Add": "+", "Sub": "-", "Mult": "*", "Div": "/",
    "Mod": "%", "Pow": "**",
}
_UNARY_OP_SYMBOL: dict[str, str] = {"USub": "-", "UAdd": "+"}


def render_ast(node: Mapping[str, Any]) -> str:
    """把编译器 `ExecutionPlan.formula_ast` 的节点渲染成公式文本。

    只支持 `Expression` / `BinOp` / `UnaryOp` / `Call` / `Name` / `Constant`
    （编译器实际产出的全集，T20 实测确认）。未知节点抛 `UnsupportedAstError`，
    由调用方降级处理 —— **不猜**。
    """
    kind = node.get("type")
    if kind == "Expression":
        return render_ast(node["body"])
    if kind == "BinOp":
        op = _BIN_OP_SYMBOL.get(str(node.get("op")))
        if op is None:
            raise UnsupportedAstError(f"未知二元算子 {node.get('op')!r}")
        return f"({render_ast(node['left'])}{op}{render_ast(node['right'])})"
    if kind == "UnaryOp":
        op = _UNARY_OP_SYMBOL.get(str(node.get("op")))
        if op is None:
            raise UnsupportedAstError(f"未知一元算子 {node.get('op')!r}")
        return f"{op}{render_ast(node['operand'])}"
    if kind == "Call":
        func = str(node.get("func"))
        args = ",".join(render_ast(a) for a in (node.get("args") or []))
        kw = node.get("keywords") or []
        if kw:
            raise UnsupportedAstError("暂不支持关键字参数")
        return f"{func}({args})"
    if kind == "Name":
        return str(node.get("id"))
    if kind == "Constant":
        value = node.get("value")
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    raise UnsupportedAstError(f"未知 AST 节点类型 {kind!r}")


def _walk_post_order(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """后序遍历（子节点在前，根在最后）。"""
    kind = node.get("type")
    out: list[Mapping[str, Any]] = []
    if kind == "Expression":
        return _walk_post_order(node["body"])
    if kind == "BinOp":
        out.extend(_walk_post_order(node["left"]))
        out.extend(_walk_post_order(node["right"]))
    elif kind == "UnaryOp":
        out.extend(_walk_post_order(node["operand"]))
    elif kind == "Call":
        for arg in (node.get("args") or []):
            out.extend(_walk_post_order(arg))
    elif kind in ("Name", "Constant"):
        pass
    else:
        raise UnsupportedAstError(f"未知 AST 节点类型 {kind!r}")
    out.append(node)
    return out


def extract_subexpressions(
    *, formula: str | None = None, plan: Any | None = None,
    compiler: Callable[..., Any] | None = None,
    include_root: bool = True,
    include_constants: bool = False,
) -> list[str]:
    """提取一个因子的所有子表达式（**规范化 + 去重 + 后序**）。

    - 规范化用 `dedup.canonicalize_formula` → `close+volume` 与 `volume+close`
      得到**同一个**子表达式键（跨因子共享缓存的前提）
    - 返回顺序为后序（叶子在前、根在最后），便于 `assemble` 取根值
    - 无法渲染的 AST → 降级为「整式作为唯一子表达式」（不抛，避免打断整批）
    - `include_constants=False`（默认）**跳过纯数字常量**：`mean(close,20)` 里的
      `20` 是编译期常量，组装时内联即可，**不该占缓存条目、也不该让 compute_fn
      去"计算"它**（T20 实测踩到：数字被登记成子表达式，`probe_subexpr_total`
      也被虚高）
    """
    if formula is None and plan is None:
        raise ValueError("必须提供 formula 或 plan 之一。")
    if plan is None:
        if compiler is None:
            from app.services.factors.factor_compiler import compile_formula

            compiler = lambda *, formula, params=None: compile_formula(  # noqa: E731
                formula=formula, params=params)
        result = compiler(formula=str(formula), params=None)
        if not (getattr(result, "success", False)
                and getattr(result, "execution_plan", None) is not None):
            raise UnsupportedAstError(f"公式无法编译，无法提取子表达式: {formula}")
        plan = result.execution_plan

    ast = getattr(plan, "formula_ast", None)
    root_text = str(getattr(plan, "formula", formula or ""))
    try:
        nodes = _walk_post_order(ast)
        texts = [render_ast(n) for n in nodes]
        if not include_constants:
            texts = [t for n, t in zip(nodes, texts)
                     if str(n.get("type")) != "Constant"]
    except (UnsupportedAstError, KeyError, TypeError) as exc:
        logger.warning("AST 渲染失败，降级为整式单子表达式: %s", exc)
        texts = [root_text]

    out: list[str] = []
    seen: set[str] = set()
    for text in texts:
        canon = DD.canonicalize_formula(text)
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)
    if not include_root and out:
        out = out[:-1]
    return out


def extract_batch(
    factors: Iterable[Mapping[str, Any] | str],
    *, compiler: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """批量提取：返回总数 / 唯一数 / 每个因子的子表达式（向导 §6.11.1 第 1~2 步）。"""
    per_factor: dict[str, list[str]] = {}
    all_texts: list[str] = []
    for factor in factors:
        if isinstance(factor, str):
            formula = factor
            key = factor
        else:
            formula = str(factor.get("canonical_formula")
                          or factor.get("formula") or factor.get("formula_expr"))
            key = str(factor.get("id") or formula)
        subs = extract_subexpressions(formula=formula, compiler=compiler)
        per_factor[key] = subs
        all_texts.extend(subs)

    unique = list(dict.fromkeys(all_texts))
    total = len(all_texts)
    return {
        "per_factor": per_factor,
        "total": total,
        "unique": len(unique),
        "unique_texts": unique,
        "dedup_rate": round(1 - len(unique) / total, 6) if total else 0.0,
    }


# ══════════════════════════════════════════════════════════
# 值检查
# ══════════════════════════════════════════════════════════


def value_quality(value: Any) -> tuple[bool, float, str | None]:
    """检查缓存值的健康度 → `(是否可缓存, NaN/非有限比例, 拒绝原因)`。

    规则（向导 §6.11.1「NaN/Inf/空值比例异常不写入」）：
    - `None` / 空数组 → 拒绝
    - 含 `Inf` / `-Inf` → 拒绝（Inf 永远不可接受）
    - NaN 比例 > `max_nan_ratio`（由调用方传阈值）→ 拒绝
    """
    if value is None:
        return False, 1.0, "值为 None"
    try:
        import numpy as np

        arr = np.asarray(value)
        if arr.size == 0:
            return False, 1.0, "值为空数组"
        if not np.issubdtype(arr.dtype, np.number):
            return True, 0.0, None      # 非数值（如对象）不参与 NaN 检查
        finite = np.isfinite(arr)
        bad = float(1.0 - finite.mean())
        if np.isinf(arr).any():
            return False, bad, "含 Inf/-Inf"
        return True, bad, None
    except Exception:  # noqa: BLE001 - 非 numpy 可处理的值按「非数值」放行
        return True, 0.0, None


# ══════════════════════════════════════════════════════════
# 记录与缓存
# ══════════════════════════════════════════════════════════


@dataclass
class SubexprRecord:
    key: str
    text: str
    status: str = STATUS_COMPUTING
    value: Any = None
    nan_ratio: float = 0.0
    error: str | None = None
    compute_ms: float = 0.0
    hits: int = 0

    def to_dict(self, *, with_value: bool = False) -> dict[str, Any]:
        out = {
            "key": self.key, "text": self.text, "status": self.status,
            "nan_ratio": round(self.nan_ratio, 6), "error": self.error,
            "compute_ms": round(self.compute_ms, 3), "hits": self.hits,
        }
        if with_value:
            out["value"] = self.value
        return out


class SubexpressionCache:
    """任务级内存缓存（**不写 DuckDB**）。一个实例绑定一个 scope + 一个数据快照版本。"""

    def __init__(
        self, *, scope: str, data_snapshot_version: str,
        max_nan_ratio: float = DEFAULT_MAX_NAN_RATIO,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if scope not in VALID_SCOPES:
            raise ValueError(f"scope 必须是 {list(VALID_SCOPES)} 之一，收到 {scope!r}。")
        self.scope = scope
        self.data_snapshot_version = str(data_snapshot_version)
        self.max_nan_ratio = float(max_nan_ratio)
        self._clock = clock
        self.schema_version = SCHEMA_VERSION
        self.hash_algorithm_version = HASH_ALGORITHM_VERSION
        self.records: dict[str, SubexprRecord] = {}
        self.discarded = False
        self.discard_reason: str | None = None
        # ── 探针计数（向导 §6.11.2）──
        self.compute_calls = 0
        self.hits = 0
        self.misses = 0
        self.rejected = 0
        self.compute_ms_total = 0.0
        #: 提取阶段的总数 / 唯一数（向导 §6.11.2 的两个独立探针）
        self.extract_total: int | None = None
        self.extract_unique: int | None = None

    # ── 键 ──

    def key_of(self, text: str) -> str:
        """子表达式键 = SHA-256(**规范化文本 + 数据快照版本 + 算法版本**)。

        - 规范化复用 `dedup.canonicalize_formula`（交换律等）
        - 序列化复用 `config_hash.canonical_config_payload`（键排序 + 紧凑）
        - **必须 SHA-256**：内置 `hash()` 有随机化种子，跨进程不稳定（铁律 R4）
        """
        payload = CH.canonical_config_payload({
            "subexpr": DD.canonicalize_formula(text),
            "data_snapshot_version": self.data_snapshot_version,
            "schema_version": self.schema_version,
        })
        digest = hashlib.sha256(
            f"{self.hash_algorithm_version}|{payload}".encode("utf-8")
        ).hexdigest()
        return digest

    # ── 读写 ──

    def register(self, text: str) -> SubexprRecord:
        """登记子表达式（状态 `computing`），已存在则原样返回。"""
        key = self.key_of(text)
        rec = self.records.get(key)
        if rec is None:
            rec = SubexprRecord(key=key, text=DD.canonicalize_formula(text))
            self.records[key] = rec
        return rec

    def status_of(self, text: str) -> str | None:
        rec = self.records.get(self.key_of(text))
        return rec.status if rec else None

    def get(self, text: str) -> Any:
        """读取（命中计数）。未就绪/失败 → `None` 并记 miss。"""
        key = self.key_of(text)
        rec = self.records.get(key)
        if rec is None:
            self.misses += 1
            return None
        if rec.status != STATUS_VALID:
            self.misses += 1
            return None
        rec.hits += 1
        self.hits += 1
        return rec.value

    def put(self, text: str, value: Any) -> SubexprRecord:
        """写入（**先做 NaN/Inf/空值检查**，异常则不写值并标 `failed`）。"""
        rec = self.register(text)
        ok, nan_ratio, why = value_quality(value)
        rec.nan_ratio = nan_ratio
        if not ok or nan_ratio > self.max_nan_ratio:
            rec.status = STATUS_FAILED
            rec.error = why or f"NaN 比例 {nan_ratio:.2%} > 上限 {self.max_nan_ratio:.0%}"
            rec.value = None
            self.rejected += 1
            logger.warning("子表达式缓存拒绝写入 %s：%s", rec.text[:60], rec.error)
            return rec
        rec.status = STATUS_VALID
        rec.value = value
        rec.error = None
        return rec

    def mark_failed(self, text: str, error: str) -> SubexprRecord:
        rec = self.register(text)
        rec.status = STATUS_FAILED
        rec.error = str(error)[:500]
        rec.value = None
        self.rejected += 1
        return rec

    def is_ready(self, text: str) -> bool:
        return self.status_of(text) == STATUS_VALID

    def missing(self, subexprs: Sequence[str]) -> list[str]:
        """列出尚未就绪（未登记 / computing / failed）的子表达式。"""
        return [s for s in subexprs if not self.is_ready(s)]

    def invalidate(self, text: str) -> None:
        """单条失效（用于采样校验发现不一致时局部重算）。"""
        key = self.key_of(text)
        rec = self.records.get(key)
        if rec is not None:
            rec.status = STATUS_COMPUTING
            rec.value = None

    def discard(self, reason: str) -> None:
        """废弃整份缓存（向导 §6.11.1：采样校验差异超限时「废弃缓存重算」）。"""
        self.discarded = True
        self.discard_reason = reason
        self.records.clear()
        logger.warning("G2 缓存已废弃：%s", reason)

    # ── 探针 / 统计 ──

    def record_extraction(self, *, total: int, unique: int) -> None:
        """记录提取阶段的规模（供 `probe_subexpr_total/unique` 如实上报）。

        `total` = 提取到的子表达式**总次数**（含重复），
        `unique` = 去重后数量。不调用时探针退回「当前缓存条目数」。
        """
        self.extract_total = int(total)
        self.extract_unique = int(unique)

    def probes(self) -> dict[str, Any]:
        """向导 §6.11.2 要求的 G2 相关探针。"""
        total_access = self.hits + self.misses
        return {
            "probe_subexpr_total": (self.extract_total
                                    if self.extract_total is not None
                                    else len(self.records)),
            "probe_subexpr_unique": (self.extract_unique
                                     if self.extract_unique is not None
                                     else len(self.records)),
            "probe_g2_hit_rate": round(self.hits / total_access, 6) if total_access else 0.0,
            "probe_subexpr_compute_ms": round(self.compute_ms_total, 3),
            "probe_g2_compute_calls": self.compute_calls,
            "probe_g2_rejected": self.rejected,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hash_algorithm_version": self.hash_algorithm_version,
            "scope": self.scope,
            "data_snapshot_version": self.data_snapshot_version,
            "max_nan_ratio": self.max_nan_ratio,
            "discarded": self.discarded,
            "extract_total": self.extract_total,
            "extract_unique": self.extract_unique,
            "records": [r.to_dict(with_value=False) for r in self.records.values()],
        }


def restore_cache(
    payload: Mapping[str, Any], *, scope: str | None = None,
) -> tuple[SubexpressionCache, str | None]:
    """从快照恢复缓存；**版本不匹配则废弃**并返回原因。

    返回 `(cache, 废弃原因或 None)`。数值本身不随快照持久化（内存缓存），
    故恢复出来的只是「键 + 状态」骨架，需重算标 `computing` 的条目。
    """
    if payload.get("schema_version") != SCHEMA_VERSION:
        return _empty_cache(scope or str(payload.get("scope") or "")), (
            f"schema_version 不匹配（快照 {payload.get('schema_version')} ≠ "
            f"当前 {SCHEMA_VERSION}）")
    if payload.get("hash_algorithm_version") != HASH_ALGORITHM_VERSION:
        return _empty_cache(scope or str(payload.get("scope") or "")), (
            f"hash_algorithm_version 不匹配（快照 "
            f"{payload.get('hash_algorithm_version')} ≠ 当前 {HASH_ALGORITHM_VERSION}）")

    resolved_scope = scope or str(payload.get("scope") or SCOPE_PRESCREEN)
    cache = SubexpressionCache(
        scope=resolved_scope,
        data_snapshot_version=str(payload.get("data_snapshot_version") or ""),
        max_nan_ratio=float(payload.get("max_nan_ratio", DEFAULT_MAX_NAN_RATIO)),
    )
    # 提取规模属于「事实」，随快照恢复（否则探针会退回记录数、失真）
    total = payload.get("extract_total")
    unique = payload.get("extract_unique")
    if total is not None and unique is not None:
        cache.record_extraction(total=int(total), unique=int(unique))
    for row in payload.get("records") or []:
        rec = SubexprRecord(
            key=str(row.get("key")), text=str(row.get("text")),
            status=STATUS_COMPUTING,     # 值不持久化 → 一律待重算
            nan_ratio=float(row.get("nan_ratio") or 0.0),
        )
        cache.records[rec.key] = rec
    return cache, None


def _empty_cache(scope: str) -> SubexpressionCache:
    resolved = scope if scope in VALID_SCOPES else SCOPE_PRESCREEN
    return SubexpressionCache(scope=resolved, data_snapshot_version="")


# ══════════════════════════════════════════════════════════
# 作用域注册表（预筛 / 全量验证不混用）
# ══════════════════════════════════════════════════════════


class CacheRegistry:
    """每个 (scope, data_snapshot_version) 一个独立缓存 —— 两阶段**不混用**。"""

    def __init__(self, *, task_id: str = "", max_nan_ratio: float = DEFAULT_MAX_NAN_RATIO,
                 ) -> None:
        self.task_id = task_id
        self.max_nan_ratio = max_nan_ratio
        self._caches: dict[tuple[str, str], SubexpressionCache] = {}

    def for_scope(self, scope: str, *, data_snapshot_version: str) -> SubexpressionCache:
        if scope not in VALID_SCOPES:
            raise ValueError(f"scope 必须是 {list(VALID_SCOPES)} 之一。")
        key = (scope, str(data_snapshot_version))
        cache = self._caches.get(key)
        if cache is None:
            cache = SubexpressionCache(
                scope=scope, data_snapshot_version=str(data_snapshot_version),
                max_nan_ratio=self.max_nan_ratio)
            self._caches[key] = cache
        return cache

    def release(self) -> None:
        """任务结束释放（向导 §6.11.1「生命周期与挖掘任务绑定」）。"""
        self._caches.clear()

    @property
    def scopes(self) -> list[str]:
        return sorted({s for s, _v in self._caches})


def assert_scope(cache: SubexpressionCache, *, expected: str) -> None:
    """跨作用域使用缓存 → 直接抛（不静默混用）。"""
    if cache.scope != expected:
        raise ScopeMismatchError(
            f"该缓存的作用域是 {cache.scope!r}，但当前阶段要求 {expected!r}；"
            "预筛缓存与全量验证缓存数据范围不同，不得混用（向导 §6.11.1）。")


# ══════════════════════════════════════════════════════════
# 计算与组装
# ══════════════════════════════════════════════════════════


def precompute(
    cache: SubexpressionCache, subexprs: Sequence[str],
    *, compute_fn: Callable[[str], Any],
    force: bool = False,
) -> dict[str, Any]:
    """批量计算尚未就绪的子表达式（**每个唯一子表达式只算一次**）。

    `compute_fn(text) -> value`：由调用方注入向量化实现（本模块不做数值计算）。
    返回统计（含 `computed` / `skipped_ready` / `failed`）。
    """
    computed = 0
    failed = 0
    skipped = 0
    for text in subexprs:
        if not force and cache.is_ready(text):
            skipped += 1
            continue
        cache.register(text)
        started = cache._clock()
        try:
            value = compute_fn(text)
        except Exception as exc:  # noqa: BLE001 - 单个子表达式失败不拖垮整批
            cache.mark_failed(text, f"{type(exc).__name__}: {exc}")
            failed += 1
            continue
        elapsed_ms = (cache._clock() - started) * 1000.0
        rec = cache.put(text, value)
        rec.compute_ms = elapsed_ms
        cache.compute_calls += 1
        cache.compute_ms_total += elapsed_ms
        if rec.status == STATUS_VALID:
            computed += 1
        else:
            failed += 1
    return {"computed": computed, "skipped_ready": skipped, "failed": failed,
            "unique": len(set(subexprs))}


def assemble(
    cache: SubexpressionCache, subexprs: Sequence[str],
    *, expected_scope: str | None = None,
) -> Any:
    """组装：全部子表达式已在缓存中则直接取**根表达式**的值，**恒不触发计算**。

    Raises:
        ScopeMismatchError: 缓存作用域与调用方声明不符
        CacheMissError: 有子表达式未就绪（调用方应重算，不得静默降级）
    """
    if expected_scope is not None:
        assert_scope(cache, expected=expected_scope)
    if not subexprs:
        raise CacheMissError([], reason="子表达式清单为空")
    missing = cache.missing(subexprs)
    if missing:
        raise CacheMissError(missing)
    root = subexprs[-1]          # 提取顺序为后序：根在最后
    return cache.get(root)


def assemble_batch(
    cache: SubexpressionCache, per_factor: Mapping[str, Sequence[str]],
    *, expected_scope: str | None = None,
) -> dict[str, Any]:
    """批量组装。缺失的因子进 `missing_factors`（**不产出错误值**）。"""
    values: dict[str, Any] = {}
    missing: dict[str, list[str]] = {}
    for key, subs in per_factor.items():
        try:
            values[key] = assemble(cache, subs, expected_scope=expected_scope)
        except CacheMissError as exc:
            missing[key] = exc.missing
    return {"values": values, "missing_factors": missing,
            "assembled": len(values), "missing": len(missing)}


# ══════════════════════════════════════════════════════════
# 采样校验（哈希碰撞 / 语义不等价兜底）
# ══════════════════════════════════════════════════════════


def verify_sample(
    factors: Sequence[Any],
    *, cache: SubexpressionCache,
    per_factor: Mapping[str, Sequence[str]],
    direct_compute: Callable[[str], Any],
    sample_size: int = VERIFY_SAMPLE_SIZE,
    tolerance: float = VERIFY_TOLERANCE,
    rng: Any | None = None,
    discard_on_mismatch: bool = True,
) -> dict[str, Any]:
    """每代随机抽 `sample_size` 个因子，把缓存组装结果与**直接计算**对比。

    差异 > `tolerance` → 报警，并按 `discard_on_mismatch` **废弃缓存**（向导 §6.11.1）。
    """
    import random

    chooser = rng or random.Random(0)
    keys = [str(f) if isinstance(f, str) else str(f.get("id") or f.get("formula") or f)
            for f in factors]
    keys = [k for k in keys if k in per_factor]
    if not keys:
        return {"checked": 0, "mismatches": [], "discarded": False,
                "note_zh": "无可用因子，跳过采样校验。"}

    picked = chooser.sample(keys, min(int(sample_size), len(keys)))
    import numpy as np

    mismatches: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    checked = 0
    for key in picked:
        subs = per_factor[key]
        try:
            cached = assemble(cache, subs)
        except CacheMissError as exc:
            # ⚠️ 「缓存里没有可比对的东西」≠「缓存结果算错了」（2026-09-23 修复）：
            # 子表达式因质量门禁（含 Inf/-Inf、NaN 比例超限）被拒绝写入后，
            # assemble 必然 cache_miss；若把它计入不一致，会**废弃整份缓存** →
            # 下一代冷启动 → 再次触发 → 缓存永远无效、每代全量重算（实测现场：
            # 20 代仅 3 代 cache_validation_passed=1）。故单独归入 `skipped`。
            skipped.append({"factor": key, "reason": "cache_miss",
                            "missing": exc.missing[:3]})
            continue
        fresh = direct_compute(key)
        checked += 1
        try:
            a = np.asarray(cached, dtype="float64")
            b = np.asarray(fresh, dtype="float64")
            if a.shape != b.shape:
                diff = float("inf")
            else:
                mask = np.isfinite(a) & np.isfinite(b)
                if mask.any():
                    diff = float(np.max(np.abs(a[mask] - b[mask])))
                elif np.array_equal(np.isnan(a), np.isnan(b)):
                    # 没有任何有限值可比较，但两侧 NaN 位置完全一致（缺失数据 /
                    # `cs_zscore` 的退化截面返回 NaN 是**有意设计**）→ 视为一致。
                    # 原实现此处取 inf → 全 NaN 面板被误判为不一致。
                    diff = 0.0
                else:
                    diff = float("inf")
        except Exception:  # noqa: BLE001 - 不可数值比较 → 视为不一致
            diff = float("inf")
        if not (diff <= tolerance):
            mismatches.append({"factor": key, "max_abs_diff": diff,
                               "tolerance": tolerance})

    result = {
        "checked": checked,
        "sampled": len(picked),
        "mismatches": mismatches,
        "skipped": skipped,
        "tolerance": tolerance,
        "discarded": False,
    }
    if skipped:
        logger.warning(
            "G2 采样校验跳过 %d 个因子（缓存缺失，多为质量门禁拒写的子表达式）：%s",
            len(skipped), skipped[:2],
        )
    if mismatches:
        logger.error("G2 采样校验发现 %d 处不一致（容差 %s）：%s",
                     len(mismatches), tolerance, mismatches[:2])
        if discard_on_mismatch:
            cache.discard(f"采样校验不一致：{mismatches[:2]}")
            result["discarded"] = True
    return result


__all__ = [
    "SCHEMA_VERSION",
    "HASH_ALGORITHM_VERSION",
    "SCOPE_PRESCREEN",
    "SCOPE_FULL_VALIDATION",
    "VALID_SCOPES",
    "STATUS_VALID",
    "STATUS_FAILED",
    "STATUS_COMPUTING",
    "VALID_STATUSES",
    "DEFAULT_MAX_NAN_RATIO",
    "VERIFY_TOLERANCE",
    "VERIFY_SAMPLE_SIZE",
    "UnsupportedAstError",
    "CacheMissError",
    "ScopeMismatchError",
    "render_ast",
    "extract_subexpressions",
    "extract_batch",
    "value_quality",
    "SubexprRecord",
    "SubexpressionCache",
    "restore_cache",
    "CacheRegistry",
    "assert_scope",
    "precompute",
    "assemble",
    "assemble_batch",
    "verify_sample",
]
