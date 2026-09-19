"""候选去重：四层机制的第 1~3 层（向导 §6.3.8；任务 T19）。

四层分工（**第 4 层不在本任务**）
==============================
| 层 | 手段 | 在哪实现 |
|---|---|---|
| 1 | **生成时规避**（事前，最重要） | 各生成器（T17/T18）消费本模块的 `build_avoidance_index` |
| 2 | **`formula_hash` 精确去重**（完全相同，含交换律归一） | 本模块 `dedup_candidates` |
| 3 | **AST 结构相似度聚类**（近似重复，>0.9 归一组留 1 个代表） | 本模块 `dedup_candidates` |
| 4 | 因子值相关性去重（运行时） | 每代短样本评估（T23/T24），**本任务不做** |

跨来源合并优先级（任务卡硬规则）
==============================
    经典 > AI > 随机

（需求 §6.3.8：「三来源候选合并后统一做一次，解决跨来源重复，保留优先级：
经典 > AI > 随机」。未知来源排最后 —— 不让未知来源抢代表位。）

淘汰**不物理删除**（任务卡「明确不做」）
======================================
所有被淘汰候选都进 `eliminated`，带 `eliminated_reason`（`eliminated_duplicate`
或 `eliminated_similar`）与 `duplicate_of`（代表公式）。库里对应列即
`factor_mining_candidates.eliminated_reason`。

单一实现纪律（R28）
==================
- 方言归一 → 复用编译器 `normalize_dialect`（**不自己维护别名表**）
- 结构相似度 → 复用 `random_generator.structure_similarity`（T18 已实现）
- 摘要算法 → 复用 `config_hash.compute_config_hash`
本模块只**新增**编译器没有的能力：交换律归一（`a+b == b+a`）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from app.services.factors.mining import config_hash as CH
from app.services.factors.mining.random_generator import structure_similarity

# ══════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════

#: 第 3 层结构相似度阈值（向导 §6.3.8：>0.9 归为一个重复组）
SIMILARITY_THRESHOLD = 0.9

#: 第 3 层的聚类范围（2026-09-17 需求方裁决 ①）
#:
#: - `same_source`（**默认**）：只在**同来源**内聚类 —— 经典族/ AI 族 / 随机族各自成组。
#:   理由：§6.3.8 第 3 层的标题是「规避 **AI/随机的高重复**」，而**同一模板的参数变体
#:   不是「重复」而是同一因子的参数族**（GA 正是靠它们做参数寻优）；
#:   全聚掉会让进化只能改结构、不能调参。跨来源的同一因子由**第 2 层**（哈希）负责，
#:   高相关近似因子由**第 4 层**（因子值相关性 ≥0.95）负责。
#: - `global`：跨来源一起聚类（旧行为）。仅用于「只关心结构唯一性」的场景。
SIMILARITY_SCOPE_SAME_SOURCE = "same_source"
SIMILARITY_SCOPE_GLOBAL = "global"
VALID_SIMILARITY_SCOPES: tuple[str, ...] = (SIMILARITY_SCOPE_SAME_SOURCE,
                                            SIMILARITY_SCOPE_GLOBAL)

#: 淘汰原因（写入 `factor_mining_candidates.eliminated_reason`）
ELIM_REASON_DUPLICATE = "eliminated_duplicate"
ELIM_REASON_SIMILAR = "eliminated_similar"
ELIM_REASON_ZH: dict[str, str] = {
    ELIM_REASON_DUPLICATE: "与已有候选公式完全相同（规范化后哈希一致）",
    ELIM_REASON_SIMILAR: "与已有候选 AST 结构高度相似（>0.9），归为同一重复组",
}

#: 跨来源优先级：数字越小越优先（经典 > AI > 随机）
SOURCE_PRIORITY: dict[str, int] = {
    "template": 0, "classic": 0, "enumerated": 0, "manual": 0,
    "ai": 1, "ai_generated": 1, "llm": 1,
    "random": 2,
}
#: 未识别来源的优先级（排最后，避免抢占代表位）
UNKNOWN_SOURCE_PRIORITY = 3

#: 交换律算子：操作数顺序不影响语义，可排序归一
COMMUTATIVE_BINARY_OPS: frozenset[str] = frozenset({"+", "*"})
#: 交换律函数：参数顺序不影响语义
COMMUTATIVE_FUNCTIONS: frozenset[str] = frozenset({"min", "max"})


# ══════════════════════════════════════════════════════════
# 迷你解析器（只为交换律归一与去冗余括号）
# ══════════════════════════════════════════════════════════

_TOKEN_RE = re.compile(r"\s*(\d+\.\d+|\d+|[A-Za-z_][A-Za-z_0-9]*|[(),+\-*/])")


class FormulaSyntaxError(ValueError):
    """公式无法解析（调用方应视为「不可规范化」，退回原文本）。"""


def _tokenize(text: str) -> list[str]:
    out: list[str] = []
    pos = 0
    src = text or ""
    while pos < len(src):
        m = _TOKEN_RE.match(src, pos)
        if not m:
            if src[pos].isspace():
                pos += 1
                continue
            raise FormulaSyntaxError(f"无法识别的字符 {src[pos]!r} @{pos}")
        out.append(m.group(1))
        pos = m.end()
    return out


class _Parser:
    """递归下降：expr → term → factor → primary。"""

    def __init__(self, tokens: Sequence[str]) -> None:
        self.tokens = list(tokens)
        self.i = 0

    def peek(self) -> str | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self) -> str:
        tok = self.peek()
        if tok is None:
            raise FormulaSyntaxError("公式意外结束")
        self.i += 1
        return tok

    def expect(self, tok: str) -> None:
        got = self.take()
        if got != tok:
            raise FormulaSyntaxError(f"期望 {tok!r}，实得 {got!r}")

    def parse(self) -> Any:
        node = self.expr()
        if self.peek() is not None:
            raise FormulaSyntaxError(f"多余记号 {self.peek()!r}")
        return node

    def expr(self) -> Any:
        node = self.term()
        while self.peek() in ("+", "-"):
            op = self.take()
            node = ("bin", op, node, self.term())
        return node

    def term(self) -> Any:
        node = self.factor()
        while self.peek() in ("*", "/"):
            op = self.take()
            node = ("bin", op, node, self.factor())
        return node

    def factor(self) -> Any:
        if self.peek() == "-":
            self.take()
            return ("neg", self.factor())
        if self.peek() == "+":
            self.take()
            return self.factor()
        return self.primary()

    def primary(self) -> Any:
        tok = self.peek()
        if tok == "(":
            self.take()
            node = self.expr()
            self.expect(")")
            return node
        if tok is None:
            raise FormulaSyntaxError("公式意外结束")
        if re.fullmatch(r"\d+\.\d+|\d+", tok):
            self.take()
            return ("num", tok)
        if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", tok):
            name = self.take()
            if self.peek() != "(":
                return ("field", name)
            self.take()
            args: list[Any] = []
            if self.peek() != ")":
                args.append(self.expr())
                while self.peek() == ",":
                    self.take()
                    args.append(self.expr())
            self.expect(")")
            return ("call", name, args)
        raise FormulaSyntaxError(f"无法解析的记号 {tok!r}")


# ── 渲染（按优先级最小加括号 + 交换律排序）──

_PREC = {"+": 1, "-": 1, "*": 2, "/": 2}


def _fmt_number(raw: str) -> str:
    """数字归一：`20.0` → `20`；`0.50` → `0.5`。"""
    if "." in raw:
        try:
            f = float(raw)
        except ValueError:
            return raw
        if f.is_integer():
            return str(int(f))
        return repr(f)
    return raw


def _render(node: Any, parent_prec: int = 0) -> str:
    kind = node[0]
    if kind == "num":
        return _fmt_number(node[1])
    if kind == "field":
        return node[1]
    if kind == "call":
        _k, name, args = node
        rendered = [_render(a) for a in args]
        if name in COMMUTATIVE_FUNCTIONS:
            rendered = sorted(rendered)
        return f"{name}({','.join(rendered)})"
    if kind == "neg":
        inner = _render(node[1], 3)
        return f"-{inner}"
    if kind == "bin":
        _k, op, left, right = node
        prec = _PREC[op]
        if op in COMMUTATIVE_BINARY_OPS:
            # 交换律：展平同算子链后**排序**，使 a+b 与 b+a 渲染一致
            parts: list[str] = []
            for side in (left, right):
                if isinstance(side, tuple) and side[0] == "bin" and side[1] == op:
                    parts.extend(_flatten_bin(side, op))
                else:
                    parts.append(_render(side, prec))
            body = op.join(sorted(parts))
        else:
            body = f"{_render(left, prec)}{op}{_render(right, prec + 1)}"
        return f"({body})" if prec < parent_prec else body
    raise FormulaSyntaxError(f"未知节点 {kind!r}")


def _flatten_bin(node: Any, op: str) -> list[str]:
    _k, _op, left, right = node
    out: list[str] = []
    for side in (left, right):
        if isinstance(side, tuple) and side[0] == "bin" and side[1] == op:
            out.extend(_flatten_bin(side, op))
        else:
            out.append(_render(side, _PREC[op]))
    return out


# ══════════════════════════════════════════════════════════
# 规范化与哈希
# ══════════════════════════════════════════════════════════


def normalize_dialect_safe(formula: str) -> tuple[str, str | None]:
    """方言归一（复用编译器实现）；失败时退回原文并返回原因。

    编译器 `normalize_dialect` 对**无法判定行情/财报的 `delta`** 会抛
    `DialectError`（见 T05 的裁决）。去重不应因方言歧义而崩 ——
    退回原文即可（该公式在编译阶段会被明确拒绝）。
    """
    try:
        from app.services.factors.factor_compiler import normalize_dialect

        return normalize_dialect(formula), None
    except Exception as exc:  # noqa: BLE001 - 方言歧义/未知字段都退回原文
        return formula, f"{type(exc).__name__}: {exc}"


def canonicalize_formula(formula: str) -> str:
    """公式规范化（第 2 层的输入）：方言归一 → 交换律归一 → 去冗余括号。

    - 交换律：`a+b` 与 `b+a`、`a*b` 与 `b*a`、`min(x,y)` 与 `min(y,x)` 归一为同一串
    - 去冗余括号：重新渲染时只保留**运算优先级必需**的括号
    - 数字归一：`20.0` → `20`
    - 空白：全部去掉

    解析失败时退回「去空白 + 小写算子名之外原样」，**不抛异常**
    （去重是批处理，不能因单条脏公式中断整批）。
    """
    text = (formula or "").strip()
    if not text:
        return ""
    normalized, _why = normalize_dialect_safe(text)
    try:
        node = _Parser(_tokenize(normalized)).parse()
    except FormulaSyntaxError:
        return re.sub(r"\s+", "", normalized)
    return _render(node)


def formula_hash(formula: str) -> str:
    """`formula_hash`：**规范化后**再算摘要（第 2 层的键）。

    摘要算法复用 `config_hash.compute_config_hash`（R28：不另起实现）——
    这里与 T18 的 `_formula_hash` 共用同一个摘要函数，差别只在
    **是否先规范化**（T18 是实时拦截，为省算力用原文；T19 是最终判定，
    必须吃掉交换律等写法差异）。
    """
    return CH.compute_config_hash({"formula": canonicalize_formula(formula)})


# ══════════════════════════════════════════════════════════
# 第 1 层辅助：给生成器的回避索引
# ══════════════════════════════════════════════════════════


def build_avoidance_index(candidates: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """构造「生成时规避」索引（第 1 层）：已有候选的哈希 + 结构键。

    生成器（T17/T18）拿它做两件事：
    - **哈希拦截**：新公式的 `formula_hash` 在其中 → 立即弃用重生成
    - **结构限变体**：同结构键已有 N 个 → 达到上限就换结构
    """
    hashes: set[str] = set()
    structure_counts: dict[str, int] = {}
    for c in candidates:
        f = str(c.get("formula") or c.get("formula_expr") or "")
        if not f:
            continue
        hashes.add(str(c.get("formula_hash") or formula_hash(f)))
        key = str(c.get("structure_key") or _structure_key_of(f))
        structure_counts[key] = structure_counts.get(key, 0) + 1
    return {
        "hashes": hashes,
        "structure_counts": structure_counts,
        "count": len(hashes),
    }


def _structure_key_of(formula: str) -> str:
    """结构键：规范化后把数字字面量替换为 `#`（与 T18 同语义）。"""
    return re.sub(r"(?<![A-Za-z_0-9.])\d+(?:\.\d+)?(?![A-Za-z_0-9])", "#",
                  canonicalize_formula(formula))


def tree_shape_key(formula: str) -> str:
    """**树形骨架**：规范化后把**数字 → `#`**，**字段名保留**。

    ⚠️ 字段名必须保留（不能抽象成 `$`）：`mean(volume,5)/mean(volume,20)-1`
    与 `mean(close,5)/mean(close,20)-1` 形状结构相同，但分属 **volume_price 与
    trend 两个类别**（T17 `category.py`），合并会直接丢掉一个类别。
    （第一版把字段也抽象了，实测把这两条判为同结构 —— 错。）

    于是：
    - `mean(close,20)` 与 `mean(close,10)` → 都是 `mean(close,#)`（**该聚**，§6.3.8 原例）
    - `mean(close,5)/mean(close,20)-1` 与 `(mean(close,5)-mean(close,20))/20`
      → `mean(close,#)/mean(close,#)-#` vs `(mean(close,#)-mean(close,#))/#`（**该分**）
    """
    return _structure_key_of(formula)


_SHAPE_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*|\d+(?:\.\d+)?|[^\w\s]")


def _shape_tokens(formula: str) -> list[str]:
    """形状键的**记号序列**（保序、含括号与运算符）。"""
    return _SHAPE_TOKEN_RE.findall(tree_shape_key(formula))


def structural_similarity(a: str, b: str) -> float:
    """**聚类用**的 AST 结构相似度 ∈ [0,1]（向导 §6.3.8 第 3 层的判定函数）。

    取**两个判据的较小值**（保守：两个都要认为像，才归为一组）：

    1. **骨架序列相似度**：形状键的记号序列做 `SequenceMatcher` 比值
       —— 保序，能识别「只差一个符号/层级」的结构差异（如 `x` 与 `-x`）
    2. **算子/字段集合相似度**：复用 T18 `structure_similarity`
       （算子多重集 + 字段多重集 Jaccard）—— 抓「字段被换掉」这类差异

    与 T18 `structure_similarity` 的关系（**组合而非重复实现**，R28）：
    本函数把它当作第二判据复用，只**新增**它没有的「保序骨架」维度。

    为什么用 min 而不是加权平均：加权平均会把「骨架只差一个负号（0.888）但
    算子字段完全一致（1.0）」平均到 0.93 → 越过 0.9 阈值 → 把 `x` 与 `-x`
    误判为重复（实测）。取 min 则是「任一侧不像就不合并」。
    """
    if canonicalize_formula(a) == canonicalize_formula(b):
        return 1.0
    sa, sb = _shape_tokens(a), _shape_tokens(b)
    if not sa or not sb:
        return 0.0
    from difflib import SequenceMatcher

    seq_sim = SequenceMatcher(None, sa, sb).ratio()
    field_sim = structure_similarity(a, b)
    return round(min(seq_sim, field_sim), 6)


# ══════════════════════════════════════════════════════════
# 跨来源优先级
# ══════════════════════════════════════════════════════════


def source_priority(candidate: Mapping[str, Any]) -> int:
    """跨来源优先级（经典 0 < AI 1 < 随机 2 < 未知 3）。

    取值来源按序：`source` → `operation` → `logic_source`。
    （契约 `Individual` 里三者分别表示「来源 / 操作类型 / 经济逻辑来源」。）
    """
    for key in ("source", "operation", "logic_source"):
        raw = candidate.get(key)
        if raw is None:
            continue
        name = str(raw).strip().lower()
        if name in SOURCE_PRIORITY:
            return SOURCE_PRIORITY[name]
    return UNKNOWN_SOURCE_PRIORITY


def _source_label(candidate: Mapping[str, Any]) -> str:
    for key in ("source", "operation", "logic_source"):
        if candidate.get(key):
            return str(candidate[key])
    return "unknown"


def _sort_key(candidate: Mapping[str, Any]) -> tuple[int, float, str]:
    return (source_priority(candidate),
            float(candidate.get("complexity") or 0.0),
            str(candidate.get("formula") or candidate.get("formula_expr") or ""))


# ══════════════════════════════════════════════════════════
# 结果
# ══════════════════════════════════════════════════════════


@dataclass
class DedupResult:
    kept: list[dict[str, Any]] = field(default_factory=list)
    eliminated: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def kept_count(self) -> int:
        return len(self.kept)

    def to_dict(self) -> dict[str, Any]:
        return {"kept": self.kept, "eliminated": self.eliminated,
                "stats": dict(self.stats)}


def _decorate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """补上 `canonical_formula` / `formula_hash` / `structure_key`。"""
    row = dict(candidate)
    formula = str(row.get("formula") or row.get("formula_expr") or "")
    row.setdefault("formula", formula)
    row["formula_expr"] = formula
    row["canonical_formula"] = canonicalize_formula(formula)
    row["formula_hash"] = formula_hash(formula)
    row["structure_key"] = _structure_key_of(formula)
    row["source_priority"] = source_priority(candidate)
    return row


def _eliminate(row: Mapping[str, Any], *, reason: str,
               duplicate_of: str, similarity: float | None = None,
               ) -> dict[str, Any]:
    """标记淘汰（**不物理删除** —— 任务卡硬规则）。"""
    out = dict(row)
    out["status"] = "eliminated"
    out["eliminated_reason"] = reason
    out["eliminated_reason_zh"] = ELIM_REASON_ZH[reason]
    out["duplicate_of"] = duplicate_of
    if similarity is not None:
        out["similarity"] = similarity
    return out


# ══════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════


def dedup_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *, similarity_threshold: float = SIMILARITY_THRESHOLD,
    use_similarity: bool = True,
    similarity_scope: str = SIMILARITY_SCOPE_SAME_SOURCE,
) -> DedupResult:
    """第 2 层（`formula_hash` 精确去重）+ 第 3 层（结构相似度聚类）。

    顺序与代表选择（向导 §6.3.8）：
    1. 先按 **(来源优先级, 复杂度, 公式)** 排序 —— 保证「经典 > AI > 随机」，
       且同优先级时低复杂度优先（初始种群的代表选择口径）
    2. 第 2 层：哈希相同只留第一个（其余 `eliminated_duplicate`）
    3. 第 3 层：对幸存者做贪心聚类，与已有代表相似度 > 阈值 → 归入该组
       （`eliminated_similar`），否则自己成为新代表

    `similarity_scope`（2026-09-17 裁决 ①，默认 `same_source`）：
    只在**同来源**内做结构聚类，保留同模板的参数族；跨来源同一因子由第 2 层处理。
    传 `global` 可恢复「跨来源一起聚」的旧行为。

    `use_similarity=False` 用于「只做精确去重」的场景（例如 T20 缓存键）。
    """
    if similarity_scope not in VALID_SIMILARITY_SCOPES:
        raise ValueError(
            f"similarity_scope 必须是 {list(VALID_SIMILARITY_SCOPES)} 之一，"
            f"收到 {similarity_scope!r}。")

    decorated = [_decorate(c) for c in candidates]
    decorated.sort(key=_sort_key)

    stats: dict[str, Any] = {
        "input_count": len(decorated),
        "exact_duplicates": 0,
        "similar_duplicates": 0,
        "similarity_threshold": similarity_threshold,
        "similarity_enabled": bool(use_similarity),
        "similarity_scope": similarity_scope,
        "by_source": {},
    }
    for row in decorated:
        label = _source_label(row)
        stats["by_source"][label] = stats["by_source"].get(label, 0) + 1

    # ── 第 2 层：精确去重（**跨来源**，权威来源优先保留）──
    seen: dict[str, dict[str, Any]] = {}
    survivors: list[dict[str, Any]] = []
    eliminated: list[dict[str, Any]] = []
    for row in decorated:
        h = str(row["formula_hash"])
        if h in seen:
            stats["exact_duplicates"] += 1
            eliminated.append(_eliminate(
                row, reason=ELIM_REASON_DUPLICATE,
                duplicate_of=str(seen[h].get("canonical_formula")
                                 or seen[h].get("formula"))))
            continue
        seen[h] = row
        survivors.append(row)

    # ── 第 3 层：结构相似度聚类（按 scope 决定是否跨来源）──
    representatives: list[dict[str, Any]] = []
    if not use_similarity:
        representatives = survivors
    else:
        # 每个来源桶各自维护代表列表：`same_source` 下只在桶内比较
        buckets: dict[int, list[dict[str, Any]]] = {}
        for row in survivors:
            bucket = (int(row["source_priority"])
                      if similarity_scope == SIMILARITY_SCOPE_SAME_SOURCE else 0)
            reps = buckets.setdefault(bucket, [])
            formula = str(row["canonical_formula"])
            best: tuple[float, dict[str, Any]] | None = None
            for rep in reps:
                # ⚠️ 必须用 `structural_similarity`（含树形骨架 + 字段集合两判据）。
                # T19 第一版这里直接调了 T18 的 `structure_similarity`（只比算子/字段
                # 集合），于是「均线趋势」与「均线斜率」被判为同结构（相似度 1.0）
                # 而误杀其一 —— 与 R29 同类：**写了新实现却没接进调用点**。
                sim = structural_similarity(
                    formula, str(rep["canonical_formula"]))
                if best is None or sim > best[0]:
                    best = (sim, rep)
            if best is not None and best[0] > similarity_threshold:
                stats["similar_duplicates"] += 1
                eliminated.append(_eliminate(
                    row, reason=ELIM_REASON_SIMILAR,
                    duplicate_of=str(best[1].get("canonical_formula")
                                     or best[1].get("formula")),
                    similarity=best[0]))
            else:
                reps.append(row)
                representatives.append(row)

    kept = []
    for row in representatives:
        out = dict(row)
        out["status"] = "active"
        out["is_representative"] = True
        kept.append(out)

    stats["kept_count"] = len(kept)
    stats["eliminated_count"] = len(eliminated)
    stats["dedup_rate"] = (
        round(len(eliminated) / len(decorated), 6) if decorated else 0.0)
    stats["unique_ratio"] = (
        round(len(kept) / len(decorated), 6) if decorated else 0.0)
    return DedupResult(kept=kept, eliminated=eliminated, stats=stats)


def dedup_across_sources(
    *, classic: Sequence[Mapping[str, Any]] = (),
    ai: Sequence[Mapping[str, Any]] = (),
    random: Sequence[Mapping[str, Any]] = (),
    similarity_threshold: float = SIMILARITY_THRESHOLD,
    use_similarity: bool = True,
    similarity_scope: str = SIMILARITY_SCOPE_SAME_SOURCE,
) -> DedupResult:
    """三来源合并去重（向导 §6.3.8：「三来源候选合并后统一做一次」）。

    合并顺序即优先级：经典 → AI → 随机（后者撞前者时被淘汰 —— **第 2 层是跨来源的**）。
    第 3 层默认只在**同来源内**聚类（裁决 ①），故「经典 40 个参数变体 + 随机 20 个」
    不会被跨来源互相吃掉，各自保留结构代表。
    """
    merged: list[Mapping[str, Any]] = []
    for rows, default_source in ((classic, "classic"), (ai, "ai"),
                                 (random, "random")):
        for row in rows:
            item = dict(row)
            item.setdefault("source", default_source)
            merged.append(item)
    return dedup_candidates(merged, similarity_threshold=similarity_threshold,
                            use_similarity=use_similarity,
                            similarity_scope=similarity_scope)


__all__ = [
    "SIMILARITY_THRESHOLD",
    "SIMILARITY_SCOPE_SAME_SOURCE",
    "SIMILARITY_SCOPE_GLOBAL",
    "VALID_SIMILARITY_SCOPES",
    "ELIM_REASON_DUPLICATE",
    "ELIM_REASON_SIMILAR",
    "ELIM_REASON_ZH",
    "SOURCE_PRIORITY",
    "UNKNOWN_SOURCE_PRIORITY",
    "COMMUTATIVE_BINARY_OPS",
    "COMMUTATIVE_FUNCTIONS",
    "FormulaSyntaxError",
    "DedupResult",
    "normalize_dialect_safe",
    "canonicalize_formula",
    "formula_hash",
    "build_avoidance_index",
    "source_priority",
    "dedup_candidates",
    "dedup_across_sources",
]
