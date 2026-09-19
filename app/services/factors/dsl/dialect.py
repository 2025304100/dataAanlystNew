"""方言归一（设计文档 §6.2，需求 §6.0，开发文档 §3.14 步骤 6，模块 M2）。

**作用域边界（本模块最重要的一条约束）**

    归一**仅在公式内生效**。后处理配置项 `rank` / `zscore` / `winsorize`
    （`factor_executor.apply_postprocess`，作用于最终因子值）语义不同，
    **保持不变**，两者并存。

    所以本模块只被 `factor_compiler.compile()` 调用；**绝不**碰
    `ExecutionPlan.postprocess` 或 `apply_postprocess`。

**归一表**（SD-v2.0 §6.2「实现唯一路径，不做双开关」）

| 输入写法 | 归一为 | 分类 |
|---------|--------|------|
| `std` / `stdev` | `stddev` | 标识符别名 |
| `llv` | `lowest` | 标识符别名 |
| `hhv` | `highest` | 标识符别名 |
| `turnover` | `turnover_rate` | 标识符别名 |
| `atr(x, n)` | `ts_atr(x, n)` | 函数改名 |
| `rank(x)` | `cs_rank(x)` | 函数改名 |
| `zscore(x)` | `cs_zscore(x)` | 函数改名 |
| `delta(行情字段, n)` | `ts_delta_bars(x, n)` | **按字段所属表判定** |
| `delta(财报字段, n)` | `ts_delta_periods(x, n)` | **按字段所属表判定** |

⚠️ `delta` 是本表唯一的**需要推断**的规则：行情字段与财报字段的采样频率不同
（交易日 vs 报告期），共用 `delta` 会让 AST 隐式解释 `n`（需求 §6.0「逻辑缺口 2」）。
本模块按首个参数的字段**所属表**（`FIELD_CATALOG[x].source_table`）判定：

    raw_daily_bars        → ts_delta_bars     （交易日）
    raw_financial_reports → ts_delta_periods  （报告期）

**判不出来就报错，不猜**：首参不是已知字段、或其所属表既非行情也非财报时，
抛 `DialectError`，提示用户改用显式算子。宁可让用户写清楚，
也不要猜错单位把质量类模板算歪。

⚠️ 实现要点：所有替换都在**引号之外**进行（字符串字面量原样保留），
且一律按**标识符边界**匹配 —— 不能把 `cs_rank` 里的 `rank`、
`turnover_rate` 里的 `turnover`、`hot_rank_pct` 里的 `rank` 误改。
"""
from __future__ import annotations

import re
from typing import Callable, Mapping

# ⚠️ 故意**不**在模块层 import `FIELD_CATALOG`：
#    `factor_compiler` 需要 import 本模块（在 compile() 里调 normalize_dialect），
#    若这里再顶层 import 它就会形成**循环导入**。
#    改在用到时惰性导入（`sys.modules` 查找，开销可忽略）。
#    反向依赖（本模块 → factor_compiler）只在函数内发生，不构成环。

#: 标识符别名（字段名 / 无参函数名 → 规范名）
DIALECT_ALIASES: dict[str, str] = {
    "std": "stddev",
    "stdev": "stddev",
    "llv": "lowest",
    "hhv": "highest",
    "turnover": "turnover_rate",
}

#: 函数改名（方言名 → 规范算子名），不带「按字段判定」逻辑
DIRECT_FUNCTION_RENAMES: dict[str, str] = {
    "rank": "cs_rank",
    "zscore": "cs_zscore",
    "atr": "ts_atr",
}

#: `delta` 家族：按首参字段所属表选择显式算子
DELTA_BY_SOURCE_TABLE: dict[str, str] = {
    "raw_daily_bars": "ts_delta_bars",          # 行情 → 交易日单位
    "raw_financial_reports": "ts_delta_periods",  # 财报 → 报告期单位
}

#: `delta` 的方言名
DELTA_DIALECT_NAME = "delta"

#: 新错误码（加入 factor_compiler.ERROR_CODES）
DIALECT_ERROR_CODE = "dialect_normalization_failed"


class DialectError(ValueError):
    """方言归一失败（通常是 `delta` 无法判定行情/财报）。

    路由与编译层应转成 `CompileError(error_code=DIALECT_ERROR_CODE)`。
    """

    def __init__(
        self,
        message: str,
        *,
        token: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.token = token
        self.detail = detail or {}


# ══════════════════════════════════════════════════════════
# 引号感知的文本处理
# ══════════════════════════════════════════════════════════

_STRING_LITERAL = re.compile(r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*\"""")


def _apply_outside_quotes(formula: str, transform: Callable[[str], str]) -> str:
    """只对**引号之外**的片段应用 `transform`，字符串字面量原样保留。

    与 `factor_compiler._normalize_dsl_surface` 同一约定（见其实现）。
    """
    parts: list[str] = []
    position = 0
    for match in _STRING_LITERAL.finditer(formula):
        parts.append(transform(formula[position:match.start()]))
        parts.append(match.group(0))
        position = match.end()
    parts.append(transform(formula[position:]))
    return "".join(parts)


def _identifier_pattern(name: str) -> re.Pattern[str]:
    """标识符边界匹配：`rank` 不会命中 `cs_rank` / `hot_rank_pct`。

    `\\b` 在 `_` 与字母之间**不成立**（`_` 是 word 字符），
    所以 `(?<![A-Za-z0-9_])` + `(?![A-Za-z0-9_])` 已足够，
    且比 `\\b` 更严格（也排除数字相邻的情况）。
    """
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")


def _function_call_pattern(name: str) -> re.Pattern[str]:
    """匹配 `name(` —— 允许中间有空白。"""
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])(\s*\()")


# ══════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════


def _default_field_table_resolver(field_name: str) -> str | None:
    """查 `FIELD_CATALOG` 得到字段所属表（惰性导入，避开循环依赖）。"""
    from app.services.factors.factor_compiler import FIELD_CATALOG

    spec = FIELD_CATALOG.get(field_name)
    return spec.source_table if spec is not None else None


def _rewrite_delta(
    formula: str,
    *,
    resolve_source_table: Callable[[str], str | None],
) -> str:
    """把 `delta(<字段>, n)` 改写为 `ts_delta_bars` / `ts_delta_periods`。

    无法判定时抛 `DialectError`（**不猜**）。
    """
    pattern = _function_call_pattern(DELTA_DIALECT_NAME)
    out: list[str] = []
    position = 0

    for match in pattern.finditer(formula):
        out.append(formula[position:match.start()])
        open_paren = match.end() - 1

        # 提取首个顶层参数
        depth = 0
        first_arg: str | None = None
        index = open_paren
        while index < len(formula):
            char = formula[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            elif char == "," and depth == 1:
                if first_arg is None:
                    first_arg = formula[open_paren + 1:index]
            index += 1

        if first_arg is None and index < len(formula):
            first_arg = formula[open_paren + 1:index]

        head = (first_arg or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", head):
            raise DialectError(
                f"{DELTA_DIALECT_NAME}() 的首个参数必须是字段名，实得 {head!r}；"
                "行情用 ts_delta_bars(x, n)、财报用 ts_delta_periods(x, n)",
                token=DELTA_DIALECT_NAME,
                detail={"first_argument": head},
            )

        source_table = resolve_source_table(head)
        target = DELTA_BY_SOURCE_TABLE.get(source_table or "")
        if target is None:
            raise DialectError(
                f"无法判定字段 {head!r} 是行情还是财报（来源表={source_table!r}），"
                "故不能确定 delta 的 n 是「交易日」还是「报告期」。"
                "请改用显式算子：行情字段用 ts_delta_bars(x, n)，"
                "财报字段用 ts_delta_periods(x, n)",
                token=head,
                detail={"field": head, "source_table": source_table},
            )

        out.append(target + match.group(1))
        position = match.end()

    out.append(formula[position:])
    return "".join(out)


def normalize_dialect(
    formula: str,
    *,
    field_source_table: Mapping[str, str] | None = None,
) -> str:
    """把公式里的方言写法归一为规范算子名（仅公式内生效）。

    Args:
        formula: 用户书写的公式
        field_source_table: 可选的「字段名 → 来源表」覆盖表；默认查 `FIELD_CATALOG`。
            单测用它可以脱离字段目录构造场景。

    Returns:
        归一后的公式。

    Raises:
        DialectError: `delta` 无法判定行情/财报（见模块 docstring）。
    """
    if not isinstance(formula, str):
        raise TypeError("formula must be a string")

    resolver: Callable[[str], str | None]
    if field_source_table is None:
        resolver = _default_field_table_resolver
    else:
        resolver = lambda name: field_source_table.get(name)  # noqa: E731

    # 顺序：delta 需要原始字段名，故先做；随后函数改名；最后标识符别名。
    # 三类规则的目标名互不冲突，故顺序只影响可读性，不影响结果。
    step_one = _apply_outside_quotes(
        formula, lambda text: _rewrite_delta(text, resolve_source_table=resolver)
    )

    def rename_functions(text: str) -> str:
        for dialect_name, canonical in DIRECT_FUNCTION_RENAMES.items():
            text = _function_call_pattern(dialect_name).sub(
                rf"{canonical}\1", text
            )
        return text

    step_two = _apply_outside_quotes(step_one, rename_functions)

    def rename_identifiers(text: str) -> str:
        for dialect_name, canonical in DIALECT_ALIASES.items():
            text = _identifier_pattern(dialect_name).sub(canonical, text)
        return text

    return _apply_outside_quotes(step_two, rename_identifiers)


__all__ = [
    "DIALECT_ALIASES",
    "DIRECT_FUNCTION_RENAMES",
    "DELTA_BY_SOURCE_TABLE",
    "DELTA_DIALECT_NAME",
    "DIALECT_ERROR_CODE",
    "DialectError",
    "normalize_dialect",
]
