"""清理仓库根的本地过程性产物（默认 dry-run，必须 --apply 才真删）。

为什么要有这个脚本而不是随手 rm：仓库根攒了 400+ 个诊断期产物（日志、临时 sqlite、
截图、一次性脚本），但同一层里躺着 start.bat / pytest.ini / README.md 这些必须保留的
工程文件。所以删除条件收紧到三条**同时**成立：

1. 未被 git 跟踪（tracked 的一律不碰 —— 那些该走 git rm，历史里可回滚）；
2. 被 .gitignore 忽略（git 自己判定，不是我猜后缀）；
3. 命中明确的产物形状白名单（日志/临时库/截图/一次性脚本/结果转储）。

另外可选 --dirs 清理根下的临时目录（只认 .pytest_tmp_* / .tmp-* / .pycache_tmp 等形状，
绝不碰 .venv / node_modules / tmp / docs / tests 这类有用途的目录）。

用法：
    python scripts/clean_root_artifacts.py            # 看清单与体积
    python scripts/clean_root_artifacts.py --apply    # 真删文件
    python scripts/clean_root_artifacts.py --apply --dirs  # 连临时目录一起清
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 允许删除的根文件形状（一律不区分大小写）
FILE_PATTERNS = (
    "*.log", "*.sqlite3", "*.sqlite", "*.db", "*.db-journal",
    "*.png", "*.jpg", "*.xml", "*.patch", "*.tmp", "*.tmp.*",
    ".tmp-*", "_tmp_*", "_dbg_*", "_debug_*", "_diag_*", "_dd_*",
    "_bb*", "_patch_*", "_verify_*", "_http_verify_*", "_tr*",
    "bb*_*.json", "*-result.txt", "result.txt", "test_results_*.txt",
    "qa_blockers_*", "api_bb*_report.json", "integration_*.sqlite3",
    "tmp_*.sqlite3", "*.db", "zhcn_head.tmp.ts", "1.2",
    # 补上的形状：都是诊断期一次性脚本（上一版只收了 _前缀，没收到这类名字）
    "check_*.py", "clean*.py", "find_*.py", "fix_*.py", "scan_*.py",
    "debug_*.py", "verify_fix.py", "verify_*.py",
    # 故意不收 *.md：报告类文字可能有人还要看（如 professional-test-report-*.md），
    # 需要清它时请手动移进 docs/ 或自行删除，实到实非可再生产物。
)

# 允许删除的根目录形状（仅在 --dirs 时生效）
# 故意不含 tmp/ —— 那是本仓库指定的 Scratch 目录（有 .gitkeep，脚本也往那里写）
DIR_PATTERNS = (
    ".pytest_tmp_*", ".tmp-*", ".pycache_tmp", ".tmp-chrome", ".tmp-edge",
    ".tmp-pytest*",
)

# 任何情况下都不碰（即便被上面的形状误捕）
NEVER = {
    ".venv", ".git", ".github", "node_modules", "frontend", "app", "tests",
    "docs", "scripts", "alembic", "config", "screenshots", "reports",
    "test_output", ".qoder", ".codex", "backups",
}


def git(args: list[str]) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=str(ROOT),
    ).stdout


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def main() -> int:
    parser = argparse.ArgumentParser(description="清理仓库根的本地过程性产物")
    parser.add_argument("--apply", action="store_true", help="真正删除（默认只列清单）")
    parser.add_argument("--dirs", action="store_true", help="连同根下临时目录一起清理")
    parser.add_argument("--list", action="store_true", help="分组列出**全部**候选，便于逐项审阅")
    args = parser.parse_args()

    tracked = {p.strip() for p in git(["ls-files"]).splitlines()}

    names_seen: set[str] = set()
    files: list[Path] = []
    for p in sorted(ROOT.iterdir()):
        if p.is_file() and p.name not in names_seen:
            names_seen.add(p.name)
            files.append(p)
    # 忽略口径用 `git ls-files --others --ignored --exclude-standard`（与
    # tests/test_ci_test_hygiene.py 一致）。实测本机 git 的 `check-ignore --stdin`
    # 行为异常：不带尾换行只返最后一行，带了尾换行反而全不返 —— 直接用它会得出
    # 「什么都没被忽略」的错误结论，从而一个文件都不敢删。
    ignored = {
        Path(line.strip().replace("\\", "/")).name
        for line in git(["ls-files", "--others", "--ignored", "--exclude-standard"]).splitlines()
        if line.strip()
    }

    targets: list[Path] = []
    for p in files:
        if p.name in tracked or p.name in NEVER:
            continue
        if p.name not in ignored:
            continue
        if not any(p.match(pat) for pat in FILE_PATTERNS):
            continue
        targets.append(p)

    total = sum(p.stat().st_size for p in targets)
    print(f"候选删除文件: {len(targets)} 个，合计 {human(total)}")

    # 按用途分组，先给概览再看明细：同后缀在不同形状下意义不同
    def bucket(p: Path) -> str:
        name = p.name.lower()
        ext = p.suffix.lower()
        if ext in (".sqlite3", ".sqlite", ".db", ".db-journal"):
            return "临时数据库（pytest/诊断跑出来的库，可重建）"
        if ext == ".log":
            return "运行与测试日志"
        if ext in (".png", ".jpg"):
            return "调试/测试截图"
        if ext == ".py":
            return "一次性诊断脚本（_tmp_/_dbg_/_diag_/_bb* 等形状）"
        if ext in (".xml", ".txt", ".json", ".js", ".ts"):
            return "测试结果与报告转储"
        return "其他（无扩展名/未归类）"

    grouped: dict[str, list[Path]] = {}
    for p in targets:
        grouped.setdefault(bucket(p), []).append(p)
    print()
    for label, items in sorted(grouped.items(), key=lambda kv: -sum(q.stat().st_size for q in kv[1])):
        size = sum(q.stat().st_size for q in items)
        print(f"  {len(items):>4} 个  {human(size):>8}  {label}")

    if args.list:
        for label, items in sorted(grouped.items()):
            print(f"\n--- {label} ---")
            for p in sorted(items, key=lambda q: q.name):
                print(f"  {human(p.stat().st_size):>8}  {p.name}")
    else:
        for p in targets[:25]:
            print(f"   {human(p.stat().st_size):>8}  {p.name}")
        if len(targets) > 25:
            print(f"   ... 另有 {len(targets) - 25} 个（加 --list 全部列出）")

    dirs: list[Path] = []
    if args.dirs:
        for p in sorted(ROOT.iterdir()):
            if not p.is_dir() or p.name in NEVER:
                continue
            if not any(p.match(pat) for pat in DIR_PATTERNS):
                continue
            dirs.append(p)
        print(f"\n候选删除目录: {len(dirs)} 个 -> {[d.name for d in dirs][:20]}")

    if not args.apply:
        print("\n[dry-run] 未做任何修改。确认清单后加 --apply 执行。")
        return 0

    removed = 0
    freed = 0
    for p in targets:
        try:
            freed += p.stat().st_size
            p.unlink()
            removed += 1
        except Exception as exc:  # 占用中的日志句柄等，跳过而不是中断
            print(f"   跳过 {p.name}: {exc}")
    print(f"已删除文件 {removed} 个，释放 {human(freed)}")

    if args.dirs:
        import shutil
        for d in dirs:
            try:
                shutil.rmtree(d)
                print(f"   已删目录 {d.name}")
            except Exception as exc:
                print(f"   跳过目录 {d.name}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
