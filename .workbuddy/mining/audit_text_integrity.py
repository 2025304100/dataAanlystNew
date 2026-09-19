"""文本完整性审计：检测「反引号经 shell 命令替换被吞掉」留下的残缺文本。

背景（**真实事故，已发生两次**）
--------------------------------
用 `bash -c "...python -c '...含反引号的文本...'"` 写文件时，Git Bash 会**先做命令替换**：
把 `` `内容` `` 当 shell 命令执行并**替换成空**。结果是落盘的中文说明里
**标识符全部消失**，而 shell 只报一堆 `xxx: command not found`（极易被忽略），
写入动作本身**成功返回 0**、不报错。

- 第一次（T03）：往 `tasks.json` 写任务说明，被静默截断成 `（ 在逐标的序列上...`。
- 第二次（T06）：往 `2026-09-16.md` 追加日志，整段标识符消失，
  出现 `## T06 · 字段注册 +3（ 保留）+ ...`、`- （26 passed / exit 0）`。

**防线**：① 含反引号的长文本一律先 `Write` 成 `.py` 再执行（MEMORY.md §1.2）；
② 写完**回读校验**；③ 本脚本做机械扫描兜底。

检测规则（**只保留零误报的子集** —— 宁可漏报也不要变成噪声门禁）
------------------------------------------------------------------
只匹配「标识符被吞掉」留下的、且**不会与正常中文写作混淆**的特征签名：

B. **标点后 2+ 连续 ASCII 空格再接 CJK**（Markdown 正文通常单空格）：
   `1.  把它投影为` ← 原文 `1. `FactorExecutor…` 把它投影为`
C. **整行只有标点或空白**：`   ，`
D. **CJK 与 CJK 之间 2+ 连续 ASCII 空格**：`字段注册   保留`

已在 5 个真实语料文件上验证：**B/C/D 零误报**。

⚠️ **已知盲区（务必知道，别误以为"扫过就没事"）**
------------------------------------------------
**「全角左括号后标识符消失」检测不到**，例如真实损坏里的
`（ 保留）`、`（26 passed…`、`（/ 未采集，禁用…`。

原因：该签名（`（` 后接空白或数字）**与正常中文写作不可区分** ——
`（**26 passed…**）`、`（2026-09-16）`、`（pkgutil 扫描）` 全是合法写法。
实测加这条规则会在 5 个文件上产生 **23 处误报**，其中 0 处是真损坏。
**一个每次跑都误报 20+ 条的门禁，比没有门禁更差**（会训练人忽略它），故删除该规则并在此记录，
**防止有人再加回来**。同样删除的还有「空反引号壳 ` `` `」——被吞掉的反引号是**整个消失**，
不留空壳；语料里匹配到的 ` `` ` 全是在引述这个事故本身的文档（零真阳性）。

**因此本工具只是兜底，不是主防线。**主防线是：
① 含反引号的长文本一律先 `Write` 成 `.py` 再执行（MEMORY.md §1.2）；
② 写完**回读校验**（T06 那次事故正是靠回读发现的，不是靠任何扫描）。

⚠️ **预期命中**：`MEMORY.md` 与 `2026-09-16.md` 里**引述本次事故**的段落会稳定命中
（例如 `1.  把它投影为` 就是被当作样例写进文档的残骸）。
每次扫描看到这 1~2 处属正常，**不要当成新的损坏**；判断标准是「这一行是不是在讲事故本身」。

用法
----
    .venv/Scripts/python.exe .workbuddy/mining/audit_text_integrity.py
    .venv/Scripts/python.exe .workbuddy/mining/audit_text_integrity.py --path docs --path .workbuddy

退出码：0 = 无命中；1 = 有命中（需人工确认）。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

DEFAULT_PATHS = (".workbuddy",)
CJK = r"[\u4e00-\u9fff]"
# B：标点后 2+ 连续 ASCII 空格再接 CJK
RULE_B = re.compile(rf"[.、，,；;:：]\s{{2,}}{CJK}")
# C：整行只有标点/空白
RULE_C = re.compile(r"^\s*[，、。：；,;:.]+\s*$")
# D：CJK 与 CJK 之间 2+ 连续 ASCII 空格
RULE_D = re.compile(rf"{CJK}[^\S\n]{{2,}}{CJK}")
RULES = (("B 标点后连续空格", RULE_B),
         ("C 纯标点行", RULE_C),
         ("D CJK 间连续空格", RULE_D))
SKIP_DIRS = {"__pycache__", ".git", "node_modules", "evidence"}
SKIP_SUFFIX = {".pyc", ".db", ".duckdb", ".png", ".jpg", ".har", ".jsonl"}


def scan_file(path: pathlib.Path) -> list[tuple[int, str, str]]:
    hits: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return hits
    for i, line in enumerate(text.splitlines(), 1):
        raw = line.rstrip()
        if not raw.strip():
            continue
        stripped = raw.lstrip()
        # 排除 Markdown 表格 / 缩进块（代码块、列表续行）
        if stripped.startswith("|") or raw.startswith(("    ", "\t")):
            continue
        for label, rule in RULES:
            if rule.search(raw):
                hits.append((i, label, raw))
                break
    return hits


KNOWN_BAD = [
    # 2026-09-16 T06 事故里**真实落盘**的残骸（逐字取自损坏后的文件）
    "1.  把它投影为",
    "   ，",
]

# **构造**样本（不是真实残骸）：用于确认预防性规则 D 仍然生效。
# 明确区分真实 / 构造，避免把"我以为会这样坏"当成"真的这样坏过"。
KNOWN_BAD_CONSTRUCTED = [
    "字段注册   保留",
]

# 已知**测不到**的损坏形态（记录在此，避免给人"扫过就没事"的错觉）
KNOWN_UNCOVERABLE = [
    "- 改 （+3 字段、 加澄清注释）、（标签+策略）",
    "## T06 · 字段注册 +3（ 保留）+ 25 模板编译率",
]

# 正常文本：这些**不能**被判为损坏（否则门禁变噪声）
KNOWN_GOOD = [
    "- **`align_factor_with_target()`** 安全约束：t 日因子值只对齐 t+horizon 收益。",
    "- **`app/models/__init__.py:84` 有 `_auto_discover_models()`**（pkgutil 扫描）",
    "| `app/services/factors/factor_compiler.py` | T03, T04, T05, T06 | **完全串行** |",
    "- 路由：`app/api/router.py` 用 `api_prefix=\"/api/v1\"` + `include_router`。",
    "    这是一段缩进代码块里的    双空格    不应误报。",
    "- **错误契约**：代码实际是 `FactorSevenError.to_7field()`，不是 `fix_action{...}`。",
    "- 组合级 **65/65 = 100%**、模板级 **23/23 = 100%**（门槛 >=90%）。",
    "- `app/services/factors/factor_compiler.py`：+3 字段、`prev_close` 补澄清注释",
    "（done），但解析时后面的 `pending` 覆盖它，随后一个脚本把重复键去重",
    "## 二、数据库（**极易误判**）",
    "- MySQL：127 → **141 表**（+14，**无丢失**）",
]


def selftest() -> int:
    """验证检测器：捕获所有**声称能捕获**的残骸，且对正常文本零误报（防假绿 + 防噪声）。"""
    print("=== 自检 1：真实残骸必须命中（逐字取自 T06 损坏后的文件）===")
    missed = []
    for sample in KNOWN_BAD:
        got = [label for label, r in RULES if r.search(sample)]
        mark = "OK  " if got else "MISS"
        if not got:
            missed.append(sample)
        print(f"  {mark} {sample[:76]}")

    print()
    print("=== 自检 1b：构造样本必须命中（确认预防性规则未失效）===")
    for sample in KNOWN_BAD_CONSTRUCTED:
        got = [label for label, r in RULES if r.search(sample)]
        mark = "OK  " if got else "MISS"
        if not got:
            missed.append(sample)
        print(f"  {mark} {sample[:76]}（构造）")

    print()
    print("=== 自检 2：正常文本不得误报（噪声门禁比没有门禁更差）===")
    noisy = []
    for sample in KNOWN_GOOD:
        if sample.lstrip().startswith("|") or sample.startswith(("    ", "\t")):
            print(f"  SKIP {sample[:76]}")
            continue
        got = [label for label, r in RULES if r.search(sample)]
        mark = "OK  " if not got else "NOISE"
        if got:
            noisy.append((sample, got))
        print(f"  {mark} {sample[:76]}")

    print()
    print("=== 自检 3：已知盲区（应当**测不到**，若测到了说明规则被误改窄）===")
    for sample in KNOWN_UNCOVERABLE:
        got = [label for label, r in RULES if r.search(sample)]
        mark = "OK  " if not got else "意外命中"
        print(f"  {mark} {sample[:76]}")

    print()
    if missed or noisy:
        print(f"FAILED —— 漏报 {len(missed)} 条、误报 {len(noisy)} 条")
        for s in missed:
            print(f"  漏报: {s}")
        for s, g in noisy:
            print(f"  误报: {s}  <- {g}")
        return 1
    print(f"ALL OK —— 捕获 {len(KNOWN_BAD)}/{len(KNOWN_BAD)} 条可捕获残骸，"
          f"对 {len(KNOWN_GOOD)} 条正常文本零误报，"
          f"盲区 {len(KNOWN_UNCOVERABLE)} 条已知且已记录。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="检测被 shell 命令替换吞掉的反引号残留")
    ap.add_argument("--path", action="append", default=None,
                    help="要扫描的路径（可重复）；默认 .workbuddy")
    ap.add_argument("--glob", action="append", default=None,
                    help="扩展名过滤（可重复）；默认 .md/.json")
    ap.add_argument("--selftest", action="store_true",
                    help="只跑检测器自身自检（用真实事故残骸），不扫描文件")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    roots = [pathlib.Path(p) for p in (args.path or DEFAULT_PATHS)]
    exts = set(args.glob or (".md", ".json"))

    total = 0
    scanned = 0
    by_rule: dict[str, int] = {}
    for root in roots:
        if not root.exists():
            print(f"  [跳过] {root} 不存在")
            continue
        walker = root.rglob("*") if root.is_dir() else [root]
        for path in sorted(walker):
            if not path.is_file():
                continue
            if path.suffix.lower() in SKIP_SUFFIX:
                continue
            if path.suffix.lower() not in exts:
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            scanned += 1
            for lineno, rule, line in scan_file(path):
                total += 1
                key = rule.split()[0]
                by_rule[key] = by_rule.get(key, 0) + 1
                print(f"  {path.as_posix()}:{lineno}  [{rule}]")
                print(f"      {line[:150]}")

    print()
    print(f"扫描 {scanned} 个文件（扩展名 {sorted(exts)}），命中 {total} 处。")
    if by_rule:
        print("  按规则分布:", dict(sorted(by_rule.items())))
    if total:
        print("⚠️ 命中**不代表**一定损坏（规则刻意收得很窄，但仍可能有零星正常用例）。")
        print("   若该行本该有字段名/文件名/函数名 → 基本就是被 shell 吞掉了：")
        print("   请用 Write 写成 .py 脚本重写该段，**不要**再用 bash -c 传含反引号的文本。")
        return 1
    print("OK —— 未发现「反引号被吞」的残留形态。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
