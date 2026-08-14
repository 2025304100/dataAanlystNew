"""WP2: 因子公式编译、校验与执行计划。

对齐 docs/专业因子库开发计划.md §WP2 和 docs/因子设置与专业因子库改造方案.md。

本模块把现有 AST 沙箱（app/services/indicator_ast_sandbox.py）升级为可复用的因子编译能力，
形成"原始表达式 + 截面后处理"两层执行计划。

核心能力（WP2-01 ~ WP2-04）：
- AST 编译核心：白名单、节点数、深度、依赖收集、错误码
- 原始表达式 DSL：字段和函数能力目录、参数范围、类型检查
- 后处理配置：winsorize、rank/zscore、neutralize、missing policy
- 稳定执行计划和哈希：canonical JSON、content_hash、compiler_version

安全约束（参照 project_memory 硬约束）：
- ast.Pow 计算结果 > 1e100 返回 None（防 OOM）
- 禁止属性访问、导入、任意函数调用、eval、递归、动态函数名、未来引用、负数 lag
- AST 最大深度默认 4，函数调用数量默认不超过 12，单因子最大回看窗口默认 250 个交易日
"""
from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


# ══════════════════════════════════════════════════════════
# WP2-04: 编译器版本与限制常量
# ══════════════════════════════════════════════════════════

COMPILER_VERSION = "wp2-1.0.0"

# 公式长度限制（字符数）
MAX_FORMULA_LENGTH = 2000

# AST 深度限制（默认 4，对齐 spec §公式编译与静态校验）
MAX_AST_DEPTH = 6

# 函数调用数量限制（默认 12）
MAX_FUNCTION_CALLS = 12

# AST 总节点数限制（防爆炸性公式）
MAX_TOTAL_NODES = 200

# 单因子最大回看窗口（交易日，默认 250）
MAX_LOOKBACK_WINDOW = 250

# 幂运算结果上限（复用 indicator_ast_sandbox）
POW_RESULT_LIMIT = 1e100


# ══════════════════════════════════════════════════════════
# WP2-01: 错误码体系
# ══════════════════════════════════════════════════════════


# 稳定错误码（英文枚举，前端 i18n 本地化）
ERROR_CODES = frozenset({
    "formula_syntax_error",        # 语法错误
    "formula_too_long",            # 公式超长
    "formula_empty",               # 公式为空
    "ast_node_forbidden",          # 不支持的 AST 节点
    "ast_depth_exceeded",          # AST 深度超限
    "ast_node_count_exceeded",     # AST 节点数超限
    "function_call_count_exceeded", # 函数调用数超限
    "function_not_allowed",        # 函数不在白名单
    "function_keyword_args",       # 不允许关键字参数
    "function_arg_count",          # 函数参数数量错误
    "function_window_invalid",     # 滚动窗口参数无效
    "field_not_in_catalog",        # 未知字段
    "function_not_in_catalog",     # 未知函数
    "lookback_window_exceeded",    # 回看窗口超限
    "negative_lag",                # 负数 lag
    "attribute_access_forbidden",  # 属性访问被禁止
    "import_forbidden",            # import 被禁止
    "subscript_forbidden",         # 下标被禁止
    "lambda_forbidden",            # lambda 被禁止
    "assignment_forbidden",        # 赋值被禁止
    "comprehension_forbidden",     # 推导式被禁止
    "params_invalid",              # 参数无效
    "postprocess_invalid",         # 后处理配置无效
    "missing_policy_invalid",      # 缺失策略无效
})


@dataclass(frozen=True)
class CompileError:
    """编译错误（结构化，稳定错误码）。"""

    error_code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "message": self.message,
            "detail": self.detail,
        }


# ══════════════════════════════════════════════════════════
# WP2-02: 原始表达式 DSL — 字段和函数能力目录
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True)
class FieldSpec:
    """字段规格（DSL 字段目录条目）。"""

    name: str
    source_table: str
    dtype: str  # "float" | "int" | "bool"
    description: str
    point_in_time: bool = False  # 是否需要 point-in-time 处理
    layer: str = "A"  # A_continuous | B_event | C_blocked | D_regime


@dataclass(frozen=True)
class FunctionSpec:
    """函数规格（DSL 函数目录条目）。"""

    name: str
    category: str  # "math" | "rolling" | "conditional"
    min_args: int
    max_args: int
    description: str
    # 参数位置约束：{arg_index: (min, max)}，用于窗口/lag 参数范围校验
    window_arg_index: int | None = None  # 哪个位置参数是窗口/lag


# ── 字段能力目录 ──────────────────────────────────────────
# 对齐 factor_engine.py SQL 和 definitions.py _SOURCE_MAPPINGS

FIELD_CATALOG: dict[str, FieldSpec] = {
    # A 层日线技术（raw_daily_bars）
    "open": FieldSpec("open", "raw_daily_bars", "float", "开盘价", layer="A"),
    "high": FieldSpec("high", "raw_daily_bars", "float", "最高价", layer="A"),
    "low": FieldSpec("low", "raw_daily_bars", "float", "最低价", layer="A"),
    "close": FieldSpec("close", "raw_daily_bars", "float", "收盘价", layer="A"),
    "volume": FieldSpec("volume", "raw_daily_bars", "float", "成交量", layer="A"),
    "amount": FieldSpec("amount", "raw_daily_bars", "float", "成交额", layer="A"),
    "turnover_rate": FieldSpec(
        "turnover_rate", "raw_daily_bars", "float", "换手率", layer="A"
    ),
    "prev_close": FieldSpec(
        "prev_close", "raw_daily_bars", "float", "前收盘价", layer="A"
    ),
    # A 层估值（raw_valuation_snapshots，point-in-time）
    "pe_ttm": FieldSpec(
        "pe_ttm", "raw_valuation_snapshots", "float", "TTM 市盈率",
        point_in_time=True, layer="A",
    ),
    "pb": FieldSpec(
        "pb", "raw_valuation_snapshots", "float", "市净率",
        point_in_time=True, layer="A",
    ),
    # B 层资金流（raw_fund_flows）
    "main_net_inflow": FieldSpec(
        "main_net_inflow", "raw_fund_flows", "float", "主力净流入", layer="B"
    ),
    # B 层财报（raw_financial_reports，point-in-time）
    "roe_ttm": FieldSpec(
        "roe_ttm", "raw_financial_reports", "float", "TTM ROE",
        point_in_time=True, layer="B",
    ),
    # B 层情绪/事件（raw_sentiment）
    "lhb_institution_net": FieldSpec(
        "lhb_institution_net", "raw_sentiment", "float",
        "龙虎榜机构净额", layer="B",
    ),
    "hot_rank_pct": FieldSpec(
        "hot_rank_pct", "raw_sentiment", "float", "热度百分比", layer="B"
    ),
    # C 层尾盘代理（raw_tail_proxy）
    "proxy_score": FieldSpec(
        "proxy_score", "raw_tail_proxy", "float", "尾盘代理分数", layer="C"
    ),
}

# 允许的字段名集合（快速查找）
_ALLOWED_FIELD_NAMES: frozenset[str] = frozenset(FIELD_CATALOG.keys())

# 允许的布尔常量名（非数据字段）
_BOOLEAN_CONSTANTS: frozenset[str] = frozenset({"True", "False"})


# ── 函数能力目录 ──────────────────────────────────────────

FUNCTION_CATALOG: dict[str, FunctionSpec] = {
    # 数学函数（category="math"）
    "abs": FunctionSpec("abs", "math", 1, 1, "绝对值"),
    "min": FunctionSpec("min", "math", 2, 2, "最小值"),
    "max": FunctionSpec("max", "math", 2, 2, "最大值"),
    "round": FunctionSpec("round", "math", 1, 2, "四舍五入"),
    "log": FunctionSpec("log", "math", 1, 1, "自然对数"),
    "sqrt": FunctionSpec("sqrt", "math", 1, 1, "平方根"),
    "exp": FunctionSpec("exp", "math", 1, 1, "指数"),
    # 滚动/时序函数（category="rolling"）
    # 约定：第一个参数为字段/表达式，第二个参数为窗口（正整数 ≤ 250）
    "sma": FunctionSpec("sma", "rolling", 2, 2, "简单移动平均", window_arg_index=1),
    "ema": FunctionSpec("ema", "rolling", 2, 2, "指数移动平均", window_arg_index=1),
    "stddev": FunctionSpec("stddev", "rolling", 2, 2, "滚动标准差", window_arg_index=1),
    "sum": FunctionSpec("sum", "rolling", 2, 2, "滚动求和", window_arg_index=1),
    "mean": FunctionSpec("mean", "rolling", 2, 2, "滚动均值", window_arg_index=1),
    "count": FunctionSpec("count", "rolling", 2, 2, "非空计数", window_arg_index=1),
    "highest": FunctionSpec("highest", "rolling", 2, 2, "滚动最高", window_arg_index=1),
    "lowest": FunctionSpec("lowest", "rolling", 2, 2, "滚动最低", window_arg_index=1),
    "ref": FunctionSpec("ref", "rolling", 2, 2, "历史引用（lag）", window_arg_index=1),
    "pct_change": FunctionSpec(
        "pct_change", "rolling", 2, 2, "百分比变化", window_arg_index=1
    ),
}

_ALLOWED_FUNCTION_NAMES: frozenset[str] = frozenset(FUNCTION_CATALOG.keys())


# ══════════════════════════════════════════════════════════
# WP2-01: AST 节点白名单（扩展自 indicator_ast_sandbox）
# ══════════════════════════════════════════════════════════

# 允许的 AST 节点类型
_ALLOWED_NODES: tuple[type, ...] = (
    # 结构
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Tuple,
    ast.List,
    # 二元算术运算符
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.Pow,
    # 一元运算符
    ast.USub,
    ast.UAdd,
    ast.Not,
    # 比较运算符（WP2 新增）
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    # 布尔运算符（WP2 新增）
    ast.BoolOp,
    ast.And,
    ast.Or,
    # 条件表达式（WP2 新增：x if cond else y）
    ast.IfExp,
)

# 明确禁止的节点类型（显式拦截，给出精确错误码）
_FORBIDDEN_NODE_MAP: dict[type, str] = {
    ast.Attribute: "attribute_access_forbidden",
    ast.Subscript: "subscript_forbidden",
    ast.Import: "import_forbidden",
    ast.ImportFrom: "import_forbidden",
    ast.Lambda: "lambda_forbidden",
    ast.Assign: "assignment_forbidden",
    ast.AugAssign: "assignment_forbidden",
    ast.AnnAssign: "assignment_forbidden",
    ast.ListComp: "comprehension_forbidden",
    ast.SetComp: "comprehension_forbidden",
    ast.DictComp: "comprehension_forbidden",
    ast.GeneratorExp: "comprehension_forbidden",
}


# ══════════════════════════════════════════════════════════
# WP2-03: 后处理配置校验
# ══════════════════════════════════════════════════════════

# 有效的缺失策略
VALID_MISSING_POLICIES: frozenset[str] = frozenset({
    "exclude", "impute_zero", "ignore",
})

# 有效的 winsorize 方法
VALID_WINSORIZE_METHODS: frozenset[str] = frozenset({"mad", "quantile"})

# 有效的 rank 方法
VALID_RANK_METHODS: frozenset[str] = frozenset({"percentile", "ordinal"})

# 有效的 neutralize 方法
VALID_NEUTRALIZE_METHODS: frozenset[str] = frozenset({"industry", "style"})


def validate_postprocess(config: dict[str, Any] | None) -> list[CompileError]:
    """校验后处理配置（WP2-03）。

    配置结构：
        {
          "winsorize": {"method": "mad", "mad_multiplier": 3.0, ...},
          "rank": {"method": "percentile", "ascending": True},
          "zscore": {"ddof": 0, "epsilon": 1e-8},
          "neutralize": {"by": "industry", "method": "ols"},
          "missing_policy": "exclude"
        }

    Returns:
        错误列表，空列表表示通过
    """
    if config is None:
        return []
    if not isinstance(config, dict):
        return [CompileError(
            error_code="postprocess_invalid",
            message="postprocess must be a dict",
            detail={"actual_type": type(config).__name__},
        )]

    errors: list[CompileError] = []

    # winsorize
    winsor = config.get("winsorize")
    if winsor is not None:
        if not isinstance(winsor, dict):
            errors.append(CompileError(
                error_code="postprocess_invalid",
                message="winsorize must be a dict",
            ))
        else:
            method = winsor.get("method", "mad")
            if method not in VALID_WINSORIZE_METHODS:
                errors.append(CompileError(
                    error_code="postprocess_invalid",
                    message=f"winsorize.method must be one of {VALID_WINSORIZE_METHODS}",
                    detail={"actual": method},
                ))
            mult = winsor.get("mad_multiplier", 3.0)
            if not isinstance(mult, (int, float)) or not (1.0 <= mult <= 5.0):
                errors.append(CompileError(
                    error_code="postprocess_invalid",
                    message="winsorize.mad_multiplier must be in [1.0, 5.0]",
                    detail={"actual": mult},
                ))
            lq = winsor.get("lower_quantile", 0.01)
            if not isinstance(lq, (int, float)) or not (0.0 <= lq <= 0.1):
                errors.append(CompileError(
                    error_code="postprocess_invalid",
                    message="winsorize.lower_quantile must be in [0.0, 0.1]",
                    detail={"actual": lq},
                ))
            uq = winsor.get("upper_quantile", 0.99)
            if not isinstance(uq, (int, float)) or not (0.9 <= uq <= 1.0):
                errors.append(CompileError(
                    error_code="postprocess_invalid",
                    message="winsorize.upper_quantile must be in [0.9, 1.0]",
                    detail={"actual": uq},
                ))

    # rank
    rank_cfg = config.get("rank")
    if rank_cfg is not None:
        if not isinstance(rank_cfg, dict):
            errors.append(CompileError(
                error_code="postprocess_invalid",
                message="rank must be a dict",
            ))
        else:
            method = rank_cfg.get("method", "percentile")
            if method not in VALID_RANK_METHODS:
                errors.append(CompileError(
                    error_code="postprocess_invalid",
                    message=f"rank.method must be one of {VALID_RANK_METHODS}",
                    detail={"actual": method},
                ))

    # zscore
    zscore_cfg = config.get("zscore")
    if zscore_cfg is not None:
        if not isinstance(zscore_cfg, dict):
            errors.append(CompileError(
                error_code="postprocess_invalid",
                message="zscore must be a dict",
            ))
        else:
            ddof = zscore_cfg.get("ddof", 0)
            if ddof not in (0, 1):
                errors.append(CompileError(
                    error_code="postprocess_invalid",
                    message="zscore.ddof must be 0 or 1",
                    detail={"actual": ddof},
                ))

    # neutralize
    neutralize_cfg = config.get("neutralize")
    if neutralize_cfg is not None:
        if not isinstance(neutralize_cfg, dict):
            errors.append(CompileError(
                error_code="postprocess_invalid",
                message="neutralize must be a dict",
            ))
        else:
            by = neutralize_cfg.get("by")
            if by not in VALID_NEUTRALIZE_METHODS:
                errors.append(CompileError(
                    error_code="postprocess_invalid",
                    message=f"neutralize.by must be one of {VALID_NEUTRALIZE_METHODS}",
                    detail={"actual": by},
                ))

    # missing_policy
    policy = config.get("missing_policy", "exclude")
    if policy not in VALID_MISSING_POLICIES:
        errors.append(CompileError(
            error_code="missing_policy_invalid",
            message=f"missing_policy must be one of {VALID_MISSING_POLICIES}",
            detail={"actual": policy},
        ))

    return errors


# ══════════════════════════════════════════════════════════
# WP2-01: AST 工具函数
# ══════════════════════════════════════════════════════════


def _is_context_node(node: ast.AST) -> bool:
    """判断是否为表达式上下文节点（Load/Store/Del）。

    这些节点是 Python AST 的上下文标记，不增加结构深度，
    在深度和节点数计算中应排除，避免实际深度被夸大。
    """
    return isinstance(node, ast.expr_context)


def _ast_depth(node: ast.AST) -> int:
    """计算 AST 最大深度（根节点不计入，排除 expr_context 节点）。"""
    max_child_depth = 0
    has_structural_child = False
    for child in ast.iter_child_nodes(node):
        if _is_context_node(child):
            continue
        has_structural_child = True
        child_depth = _ast_depth(child)
        if child_depth > max_child_depth:
            max_child_depth = child_depth
    return max_child_depth + 1 if has_structural_child else 0


def _count_nodes(node: ast.AST) -> int:
    """统计 AST 总节点数（排除 expr_context 节点）。"""
    return sum(
        1 for n in ast.walk(node) if not _is_context_node(n)
    )


def _count_function_calls(node: ast.AST) -> int:
    """统计函数调用数量。"""
    return sum(1 for n in ast.walk(node) if isinstance(n, ast.Call))


def _serialize_ast(node: ast.AST) -> dict[str, Any]:
    """将 AST 节点序列化为可 JSON 化的字典（稳定结构）。"""
    if isinstance(node, ast.Expression):
        return {"type": "Expression", "body": _serialize_ast(node.body)}
    if isinstance(node, ast.BinOp):
        return {
            "type": "BinOp",
            "op": type(node.op).__name__,
            "left": _serialize_ast(node.left),
            "right": _serialize_ast(node.right),
        }
    if isinstance(node, ast.UnaryOp):
        return {
            "type": "UnaryOp",
            "op": type(node.op).__name__,
            "operand": _serialize_ast(node.operand),
        }
    if isinstance(node, ast.Call):
        return {
            "type": "Call",
            "func": (
                node.func.id if isinstance(node.func, ast.Name) else "<unknown>"
            ),
            "args": [_serialize_ast(a) for a in node.args],
            "keywords": [
                {"arg": kw.arg, "value": _serialize_ast(kw.value)}
                for kw in node.keywords
            ],
        }
    if isinstance(node, ast.Name):
        return {"type": "Name", "id": node.id}
    if isinstance(node, ast.Constant):
        return {"type": "Constant", "value": node.value}
    if isinstance(node, ast.Compare):
        return {
            "type": "Compare",
            "ops": [type(o).__name__ for o in node.ops],
            "left": _serialize_ast(node.left),
            "comparators": [
                _serialize_ast(c) for c in node.comparators
            ],
        }
    if isinstance(node, ast.BoolOp):
        return {
            "type": "BoolOp",
            "op": type(node.op).__name__,
            "values": [_serialize_ast(v) for v in node.values],
        }
    if isinstance(node, ast.IfExp):
        return {
            "type": "IfExp",
            "test": _serialize_ast(node.test),
            "body": _serialize_ast(node.body),
            "orelse": _serialize_ast(node.orelse),
        }
    if isinstance(node, ast.Tuple):
        return {
            "type": "Tuple",
            "elts": [_serialize_ast(e) for e in node.elts],
        }
    if isinstance(node, ast.List):
        return {
            "type": "List",
            "elts": [_serialize_ast(e) for e in node.elts],
        }
    return {"type": type(node).__name__}


# ══════════════════════════════════════════════════════════
# WP2-01: 依赖收集
# ══════════════════════════════════════════════════════════


@dataclass
class CollectedDependencies:
    """从公式 AST 收集的依赖信息。"""

    fields: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    source_tables: list[str] = field(default_factory=list)
    max_lookback: int = 1
    point_in_time_fields: list[str] = field(default_factory=list)
    unknown_names: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fields": sorted(self.fields),
            "functions": sorted(self.functions),
            "source_tables": sorted(self.source_tables),
            "max_lookback": self.max_lookback,
            "point_in_time_fields": sorted(self.point_in_time_fields),
            "unknown_names": sorted(self.unknown_names),
        }


def _collect_dependencies(node: ast.AST) -> CollectedDependencies:
    """从 AST 收集字段和函数依赖。

    - ast.Name → 字段名（如果在 FIELD_CATALOG 中）或布尔常量
    - ast.Call → 函数名
    - 滚动函数的窗口参数 → max_lookback
    """
    deps = CollectedDependencies()
    field_set: set[str] = set()
    func_set: set[str] = set()
    table_set: set[str] = set()
    pit_set: set[str] = set()
    unknown_set: set[str] = set()

    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            name = child.id
            if name in _BOOLEAN_CONSTANTS:
                continue
            if name in FIELD_CATALOG:
                field_set.add(name)
                spec = FIELD_CATALOG[name]
                table_set.add(spec.source_table)
                if spec.point_in_time:
                    pit_set.add(name)
            elif name not in _ALLOWED_FUNCTION_NAMES:
                # 未知字段名（可能是用户自定义参数，后续由 params 解析）
                # 排除函数名（ast.walk 会单独访问 Call.func 的 Name 节点）
                unknown_set.add(name)

        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name):
                func_name = child.func.id
                if func_name in FUNCTION_CATALOG:
                    func_set.add(func_name)
                    spec = FUNCTION_CATALOG[func_name]
                    # 提取窗口参数
                    if (
                        spec.window_arg_index is not None
                        and len(child.args) > spec.window_arg_index
                    ):
                        window_arg = child.args[spec.window_arg_index]
                        if isinstance(window_arg, ast.Constant) and isinstance(
                            window_arg.value, (int, float)
                        ):
                            window_val = int(window_arg.value)
                            if window_val > deps.max_lookback:
                                deps.max_lookback = window_val

    deps.fields = sorted(field_set)
    deps.functions = sorted(func_set)
    deps.source_tables = sorted(table_set)
    deps.point_in_time_fields = sorted(pit_set)
    deps.unknown_names = sorted(unknown_set)
    return deps


# ══════════════════════════════════════════════════════════
# WP2-04: 执行计划与哈希
# ══════════════════════════════════════════════════════════


@dataclass
class ExecutionPlan:
    """稳定执行计划（canonical JSON 可序列化）。"""

    compiler_version: str
    formula: str
    formula_ast: dict[str, Any]
    params: dict[str, Any]
    postprocess: dict[str, Any] | None
    direction: str
    data_dependencies: dict[str, Any]
    max_lookback: int
    complexity_score: float
    ast_depth: int
    node_count: int
    function_call_count: int

    def to_canonical_json(self) -> str:
        """生成 canonical JSON（排序键，确保确定性）。"""
        payload = {
            "compiler_version": self.compiler_version,
            "formula": self.formula,
            "formula_ast": self.formula_ast,
            "params": self.params,
            "postprocess": self.postprocess,
            "direction": self.direction,
            "data_dependencies": self.data_dependencies,
            "max_lookback": self.max_lookback,
            "complexity_score": round(self.complexity_score, 6),
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)

    def content_hash(self) -> str:
        """计算 content_hash（SHA256 前 16 位）。"""
        canonical = self.to_canonical_json()
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "compiler_version": self.compiler_version,
            "formula": self.formula,
            "formula_ast": self.formula_ast,
            "params": self.params,
            "postprocess": self.postprocess,
            "direction": self.direction,
            "data_dependencies": self.data_dependencies,
            "max_lookback": self.max_lookback,
            "complexity_score": self.complexity_score,
            "ast_depth": self.ast_depth,
            "node_count": self.node_count,
            "function_call_count": self.function_call_count,
            "execution_plan_hash": self.content_hash(),
        }


def _compute_complexity_score(
    *,
    ast_depth: int,
    node_count: int,
    function_call_count: int,
    max_lookback: int,
) -> float:
    """计算复杂度分数。

    公式：depth * 10 + node_count + calls * 5 + max_lookback / 25
    范围约 [0, 100+]，用于排序和门禁参考。
    """
    return (
        ast_depth * 10.0
        + node_count
        + function_call_count * 5.0
        + max_lookback / 25.0
    )


# ══════════════════════════════════════════════════════════
# WP2-01~04: 编译结果
# ══════════════════════════════════════════════════════════


@dataclass
class CompilationResult:
    """编译结果。"""

    success: bool
    execution_plan: ExecutionPlan | None = None
    errors: list[CompileError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return self.success and len(self.errors) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "is_valid": self.is_valid,
            "execution_plan": (
                self.execution_plan.to_dict() if self.execution_plan else None
            ),
            "errors": [e.to_dict() for e in self.errors],
        }


# ══════════════════════════════════════════════════════════
# WP2-01~04: 因子编译器
# ══════════════════════════════════════════════════════════


class FactorCompiler:
    """因子公式编译器。

    用法：
        compiler = FactorCompiler()
        result = compiler.compile(
            formula="1 / pe_ttm",
            params={},
            direction="higher_better",
            postprocess=None,
        )
        if result.is_valid:
            plan = result.execution_plan
            print(plan.content_hash())

    编译流程：
    1. 公式基本检查（非空、长度）
    2. AST 解析（语法错误 → formula_syntax_error）
    3. AST 节点白名单校验（禁止属性访问/导入/lambda 等）
    4. AST 深度/节点数/函数调用数限制检查
    5. 函数白名单和参数校验
    6. 字段目录校验（未知字段 → field_not_in_catalog）
    7. 依赖收集（字段、函数、源表、回看窗口、point-in-time）
    8. 后处理配置校验
    9. 构建执行计划（canonical JSON + content_hash）
    """

    def __init__(
        self,
        *,
        max_depth: int = MAX_AST_DEPTH,
        max_function_calls: int = MAX_FUNCTION_CALLS,
        max_total_nodes: int = MAX_TOTAL_NODES,
        max_lookback: int = MAX_LOOKBACK_WINDOW,
        max_formula_length: int = MAX_FORMULA_LENGTH,
    ):
        self.max_depth = max_depth
        self.max_function_calls = max_function_calls
        self.max_total_nodes = max_total_nodes
        self.max_lookback = max_lookback
        self.max_formula_length = max_formula_length

    def compile(
        self,
        *,
        formula: str,
        params: dict[str, Any] | None = None,
        direction: str = "higher_better",
        postprocess: dict[str, Any] | None = None,
        strict_fields: bool = True,
    ) -> CompilationResult:
        """编译因子公式，生成执行计划。

        Args:
            formula: 公式表达式（Python 语法子集）
            params: 公式参数（可在公式中引用的变量）
            direction: 因子方向
            postprocess: 后处理配置
            strict_fields: True 时未知字段报错，False 时仅记录到 unknown_names

        Returns:
            CompilationResult
        """
        errors: list[CompileError] = []
        params = params or {}

        # 1. 基本检查
        if not isinstance(formula, str) or not formula.strip():
            return CompilationResult(
                success=False,
                errors=[CompileError(
                    error_code="formula_empty",
                    message="formula must be a non-empty string",
                )],
            )
        if len(formula) > self.max_formula_length:
            errors.append(CompileError(
                error_code="formula_too_long",
                message=f"formula length {len(formula)} exceeds limit {self.max_formula_length}",
                detail={"length": len(formula), "limit": self.max_formula_length},
            ))
            return CompilationResult(success=False, errors=errors)

        # 2. AST 解析
        try:
            tree: ast.Expression = ast.parse(formula.strip(), mode="eval")
        except SyntaxError as exc:
            return CompilationResult(
                success=False,
                errors=[CompileError(
                    error_code="formula_syntax_error",
                    message=f"syntax error: {exc.msg}",
                    detail={"line": exc.lineno, "offset": exc.offset},
                )],
            )

        # 3. AST 节点白名单校验
        node_errors = self._validate_nodes(tree)
        errors.extend(node_errors)

        # 4. 深度/节点数/函数调用数限制
        depth = _ast_depth(tree)
        node_count = _count_nodes(tree)
        call_count = _count_function_calls(tree)

        if depth > self.max_depth:
            errors.append(CompileError(
                error_code="ast_depth_exceeded",
                message=f"AST depth {depth} exceeds limit {self.max_depth}",
                detail={"depth": depth, "limit": self.max_depth},
            ))
        if node_count > self.max_total_nodes:
            errors.append(CompileError(
                error_code="ast_node_count_exceeded",
                message=f"AST node count {node_count} exceeds limit {self.max_total_nodes}",
                detail={"count": node_count, "limit": self.max_total_nodes},
            ))
        if call_count > self.max_function_calls:
            errors.append(CompileError(
                error_code="function_call_count_exceeded",
                message=f"function call count {call_count} exceeds limit {self.max_function_calls}",
                detail={"count": call_count, "limit": self.max_function_calls},
            ))

        # 5. 函数白名单和参数校验
        func_errors = self._validate_functions(tree)
        errors.extend(func_errors)

        # 6. 依赖收集
        deps = _collect_dependencies(tree)

        # 未知字段检查
        if strict_fields:
            param_keys = set(params.keys()) if params else set()
            for name in deps.unknown_names:
                if name not in param_keys:
                    errors.append(CompileError(
                        error_code="field_not_in_catalog",
                        message=f"unknown field: {name}",
                        detail={"field": name},
                    ))

        # 回看窗口限制
        if deps.max_lookback > self.max_lookback:
            errors.append(CompileError(
                error_code="lookback_window_exceeded",
                message=f"lookback window {deps.max_lookback} exceeds limit {self.max_lookback}",
                detail={"lookback": deps.max_lookback, "limit": self.max_lookback},
            ))

        # 7. 后处理配置校验
        postprocess_errors = validate_postprocess(postprocess)
        errors.extend(postprocess_errors)

        if errors:
            return CompilationResult(success=False, errors=errors)

        # 8. 构建执行计划
        complexity = _compute_complexity_score(
            ast_depth=depth,
            node_count=node_count,
            function_call_count=call_count,
            max_lookback=deps.max_lookback,
        )

        plan = ExecutionPlan(
            compiler_version=COMPILER_VERSION,
            formula=formula.strip(),
            formula_ast=_serialize_ast(tree),
            params=_normalize_params(params),
            postprocess=_normalize_postprocess(postprocess),
            direction=direction,
            data_dependencies=deps.to_dict(),
            max_lookback=deps.max_lookback,
            complexity_score=complexity,
            ast_depth=depth,
            node_count=node_count,
            function_call_count=call_count,
        )

        return CompilationResult(success=True, execution_plan=plan, errors=[])

    # ── AST 节点校验 ──────────────────────────────────────

    def _validate_nodes(self, tree: ast.AST) -> list[CompileError]:
        """校验 AST 节点白名单。"""
        errors: list[CompileError] = []
        for child in ast.walk(tree):
            # 显式禁止的节点（精确错误码）
            forbidden_code = _FORBIDDEN_NODE_MAP.get(type(child))
            if forbidden_code is not None:
                errors.append(CompileError(
                    error_code=forbidden_code,
                    message=f"forbidden AST node: {type(child).__name__}",
                    detail={"node_type": type(child).__name__},
                ))
                continue
            # 不在白名单中的节点
            if not isinstance(child, _ALLOWED_NODES):
                errors.append(CompileError(
                    error_code="ast_node_forbidden",
                    message=f"unsupported AST node: {type(child).__name__}",
                    detail={"node_type": type(child).__name__},
                ))
        return errors

    # ── 函数校验 ──────────────────────────────────────────

    def _validate_functions(self, tree: ast.AST) -> list[CompileError]:
        """校验函数调用白名单和参数。"""
        errors: list[CompileError] = []
        for child in ast.walk(tree):
            if not isinstance(child, ast.Call):
                continue
            # 函数名必须是 ast.Name（禁止属性调用）
            if not isinstance(child.func, ast.Name):
                errors.append(CompileError(
                    error_code="function_not_allowed",
                    message="only direct function calls are allowed (no attribute calls)",
                ))
                continue
            func_name = child.func.id
            # 关键字参数禁止
            if child.keywords:
                errors.append(CompileError(
                    error_code="function_keyword_args",
                    message=f"keyword arguments are not allowed in function: {func_name}",
                    detail={"function": func_name},
                ))
            # 函数必须在目录中
            spec = FUNCTION_CATALOG.get(func_name)
            if spec is None:
                errors.append(CompileError(
                    error_code="function_not_in_catalog",
                    message=f"unknown function: {func_name}",
                    detail={"function": func_name},
                ))
                continue
            # 参数数量检查
            arg_count = len(child.args)
            if arg_count < spec.min_args or arg_count > spec.max_args:
                errors.append(CompileError(
                    error_code="function_arg_count",
                    message=f"function {func_name} expects {spec.min_args}-{spec.max_args} args, got {arg_count}",
                    detail={
                        "function": func_name,
                        "expected_min": spec.min_args,
                        "expected_max": spec.max_args,
                        "actual": arg_count,
                    },
                ))
            # 窗口参数校验（滚动函数）
            if spec.window_arg_index is not None and arg_count > spec.window_arg_index:
                window_arg = child.args[spec.window_arg_index]
                if not isinstance(window_arg, ast.Constant):
                    errors.append(CompileError(
                        error_code="function_window_invalid",
                        message=f"function {func_name} window must be a constant integer",
                        detail={"function": func_name},
                    ))
                elif not isinstance(window_arg.value, (int, float)):
                    errors.append(CompileError(
                        error_code="function_window_invalid",
                        message=f"function {func_name} window must be numeric, got {type(window_arg.value).__name__}",
                        detail={"function": func_name},
                    ))
                else:
                    window_val = int(window_arg.value)
                    if window_val <= 0:
                        errors.append(CompileError(
                            error_code="negative_lag",
                            message=f"function {func_name} window must be positive, got {window_val}",
                            detail={"function": func_name, "window": window_val},
                        ))
                    elif window_val > self.max_lookback:
                        errors.append(CompileError(
                            error_code="lookback_window_exceeded",
                            message=f"function {func_name} window {window_val} exceeds limit {self.max_lookback}",
                            detail={"function": func_name, "window": window_val},
                        ))
        return errors


# ══════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════


def _normalize_params(params: dict[str, Any]) -> dict[str, Any]:
    """规范化参数（排序键，确保确定性）。"""
    if not params:
        return {}
    # 递归排序嵌套 dict
    def _sort(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: _sort(v) for k, v in sorted(obj.items())}
        if isinstance(obj, (list, tuple)):
            return [_sort(v) for v in obj]
        return obj
    return _sort(params)


def _normalize_postprocess(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """规范化后处理配置（排序键）。"""
    if config is None:
        return None
    def _sort(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: _sort(v) for k, v in sorted(obj.items())}
        if isinstance(obj, (list, tuple)):
            return [_sort(v) for v in obj]
        return obj
    return _sort(config)


# ══════════════════════════════════════════════════════════
# 便捷函数
# ══════════════════════════════════════════════════════════

# 模块级单例（无状态，线程安全）
_default_compiler: FactorCompiler | None = None


def get_default_compiler() -> FactorCompiler:
    """获取默认编译器单例。"""
    global _default_compiler
    if _default_compiler is None:
        _default_compiler = FactorCompiler()
    return _default_compiler


def compile_formula(
    *,
    formula: str,
    params: dict[str, Any] | None = None,
    direction: str = "higher_better",
    postprocess: dict[str, Any] | None = None,
    strict_fields: bool = True,
) -> CompilationResult:
    """编译因子公式（便捷函数）。"""
    return get_default_compiler().compile(
        formula=formula,
        params=params,
        direction=direction,
        postprocess=postprocess,
        strict_fields=strict_fields,
    )


def validate_formula(formula: str, *, params: dict[str, Any] | None = None) -> list[CompileError]:
    """仅校验公式（不生成执行计划）。"""
    result = compile_formula(
        formula=formula, params=params, strict_fields=False
    )
    return result.errors


__all__ = [
    # 常量
    "COMPILER_VERSION",
    "MAX_AST_DEPTH",
    "MAX_FUNCTION_CALLS",
    "MAX_LOOKBACK_WINDOW",
    "MAX_TOTAL_NODES",
    "MAX_FORMULA_LENGTH",
    "POW_RESULT_LIMIT",
    "ERROR_CODES",
    # DSL 目录
    "FieldSpec",
    "FunctionSpec",
    "FIELD_CATALOG",
    "FUNCTION_CATALOG",
    "VALID_MISSING_POLICIES",
    "VALID_WINSORIZE_METHODS",
    "VALID_RANK_METHODS",
    "VALID_NEUTRALIZE_METHODS",
    # 错误与结果
    "CompileError",
    "CollectedDependencies",
    "ExecutionPlan",
    "CompilationResult",
    # 编译器
    "FactorCompiler",
    "get_default_compiler",
    "compile_formula",
    "validate_formula",
    "validate_postprocess",
]
