"""把 pytest 失败摘要变成**机器可读**的 CI 输出。

为什么需要它：Actions 的 job 日志接口对匿名请求返回 403，于是"CI 红了但自动化助手/脚本
读不到红因"，只能靠人登录去点。本脚本在失败步骤后跑，把关键失败行同时发到三个地方：
  1. `::error` workflow command —— 进 check-run annotations，可用 /check-runs 读到；
  2. `$GITHUB_STEP_SUMMARY` —— 跑完在页面顶部直接可读；
  3. stdout —— 兜底。

它自身绝不失败（永远 exit 0），否则会盖掉真正的红因。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

MAX_ANNOTATIONS = 12
# 300 字会把结构化错误的 technical_details 刚好切掉（实测：正好卡在 "technical_"
# 处），而那一段才是定位问题需要的约束名/原始错误 —— 宁可长一点也不要断在要害。
MAX_LEN = 900

FAILED_RE = re.compile(r"^(FAILED|ERROR)\s+\S+")
EXC_RE = re.compile(r"^E\s+\S")


def escape_workflow(value: str) -> str:
    """GitHub workflow command 的转义规则。"""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def digest(text: str) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if FAILED_RE.match(line):
            key = line[:MAX_LEN]
            if key not in seen:
                seen.add(key)
                lines.append(key)
    # 兜底：短摘要缺失时（某些 -q 组合不打印 FAILED 行），退化为抓异常行
    if not lines:
        for raw in text.splitlines():
            line = raw.strip()
            if EXC_RE.match(line):
                key = line[:MAX_LEN]
                if key not in seen:
                    seen.add(key)
                    lines.append(key)
            if len(lines) >= MAX_ANNOTATIONS:
                break
    return lines[:MAX_ANNOTATIONS]


def read_any(path: Path) -> str:
    """日志编码不统一（PowerShell 的 Tee-Object 默认写 UTF-16），按候选编码依次试。"""
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-16", "utf-16-le", "utf-16-be", "gbk", "latin-1"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        # 能解出足够的 ASCII 关键字才算选对了
        if "test" in text or "passed" in text or "ERROR" in text:
            return text
    return raw.decode("utf-8", errors="replace")


def main() -> int:
    if len(sys.argv) < 2:
        print("::warning::ci_digest 需要 pytest 输出文件路径，跳过")
        return 0
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"::warning::ci_digest 找不到输出文件 {path}，跳过")
        return 0

    text = read_any(path)
    lines = digest(text)
    tail = [l for l in text.splitlines() if re.search(r"\d+ (passed|failed|error)", l)]

    if not lines:
        print("::notice title=ci-digest::无 FAILED/ERROR 摘要行；结尾统计：" + (tail[-1][:200] if tail else "无"))
        summary = "### 失败摘要\n\n未解析到 FAILED/ERROR 行。结尾统计：\n\n```\n" + (tail[-1] if tail else "（无）") + "\n```\n"
    else:
        print(f"::group=CI 失败摘要（{len(lines)} 条）")
        for line in lines:
            print(f"::error title=pytest-failure::{escape_workflow(line)}")
        print("::endgroup::")
        summary = "### 失败摘要\n\n```text\n" + "\n".join(lines) + "\n```\n"
        if tail:
            summary += "\n结尾统计：`" + tail[-1].strip()[:200] + "`\n"

    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        try:
            with open(out, "a", encoding="utf-8") as fh:
                fh.write(summary)
        except OSError as exc:  # 摘要写不进也不能影响主流程
            print(f"::warning::写 step summary 失败：{exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
