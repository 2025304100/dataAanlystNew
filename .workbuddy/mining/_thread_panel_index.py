"""把新增的可选参数 `panel_index` 串到 `_eval_ast` 的全部递归调用点。

只改 `_eval_ast(...)` 的调用，**不动** `_eval_rolling_func(...)` ——
后者签名不含 `panel_index`，误改会直接 TypeError。

改完立即回读校验：递归调用数、是否残留未串入的调用、以及
`_eval_rolling_func` 是否被误改。
"""
from __future__ import annotations

import pathlib
import re

TARGET = pathlib.Path("app/services/factors/factor_executor.py")

# `_eval_ast(<无括号的参数>, symbol_series=symbol_series)` —— 参数里不含括号
PATTERN = re.compile(r"(_eval_ast\([^()]*?symbol_series=symbol_series)\)")


def main() -> int:
    source = TARGET.read_text(encoding="utf-8")
    already = len(re.findall(r"_eval_ast\([^()]*?panel_index=panel_index\)", source))
    matches = PATTERN.findall(source)
    print(f"待串入的 _eval_ast 调用数 = {len(matches)}（已串入 {already}）")

    updated = PATTERN.sub(r"\1, panel_index=panel_index)", source)
    TARGET.write_text(updated, encoding="utf-8")

    # ── 校验 ────────────────────────────────────────────────
    after = TARGET.read_text(encoding="utf-8")
    remaining = PATTERN.findall(after)
    print(f"写回后残留未串入 = {len(remaining)}（应为 0）")

    total_calls = len(re.findall(r"_eval_ast\(", after))
    with_index = len(re.findall(r"_eval_ast\([^;]*?panel_index=panel_index\)", after))
    print(f"_eval_ast 调用点总数 = {total_calls}，其中带 panel_index = {with_index}")

    broken_rolling = re.findall(r"_eval_rolling_func\([^()]*?panel_index=panel_index", after)
    print(f"误改 _eval_rolling_func 的次数 = {len(broken_rolling)}（应为 0）")

    syntax_ok = True
    try:
        import ast as _ast

        _ast.parse(after)
    except SyntaxError as exc:
        syntax_ok = False
        print(f"[FAIL] 语法错误：{exc}")
    print(f"语法校验：{'OK' if syntax_ok else 'FAIL'}")

    ok = not remaining and not broken_rolling and syntax_ok and with_index >= 10
    print()
    print("RESULT:", "ALL OK" if ok else "有异常，需人工检查")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
