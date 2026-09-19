# -*- coding: utf-8 -*-
"""T34 侦查：dump dedup/random_generator/genetic_algorithm/category 的公开定义。"""
import ast
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
BASE = ROOT / "app/services/factors/mining"

FILES = [
    "dedup.py",
    "random_generator.py",
    "genetic_algorithm.py",
    "category.py",
    "selection/__init__.py",
]


def sig(fn: ast.FunctionDef) -> str:
    a = fn.args
    args = list(a.posonlyargs) + list(a.args)
    defaults = list(a.defaults)
    nd = len(defaults)
    parts = []
    for i, x in enumerate(args):
        t = f": {ast.unparse(x.annotation)}" if x.annotation else ""
        d = ""
        if nd and i >= len(args) - nd:
            d = f"={ast.unparse(defaults[i - (len(args) - nd)])}"
        parts.append(f"{x.arg}{t}{d}")
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    if a.kwonlyargs:
        if not a.vararg:
            parts.append("*")
        for j, x in enumerate(a.kwonlyargs):
            t = f": {ast.unparse(x.annotation)}" if x.annotation else ""
            d = ""
            kd = a.kw_defaults[j] if j < len(a.kw_defaults) else None
            if kd is not None:
                d = f"={ast.unparse(kd)}"
            parts.append(f"{x.arg}{t}{d}")
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    ret = f" -> {ast.unparse(fn.returns)}" if fn.returns else ""
    return f"({', '.join(parts)}){ret}"


for name in FILES:
    p = BASE / name
    tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
    print("=" * 70)
    print(f"## {name}")
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            print(f"  {kind} {node.name}{sig(node)}")
        elif isinstance(node, ast.ClassDef):
            print(f"  class {node.name}:")
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef):
                    print(f"      def {sub.name}{sig(sub)}")
                elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                    ann = ast.unparse(sub.annotation) if sub.annotation else ""
                    val = ast.unparse(sub.value)[:60] if sub.value else ""
                    print(f"      {sub.target.id}: {ann} = {val}")
    # 模块级常量（全大写，<=6 个字符以上的名字）
    consts = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            n = node.targets[0].id
            if n.isupper():
                try:
                    consts.append(f"{n} = {ast.unparse(node.value)[:80]}")
                except Exception:
                    pass
    if consts:
        print("  -- constants --")
        for c in consts[:25]:
            print("     ", c)
