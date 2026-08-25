"""扫描 tests/test_defect_baseline.py / 所有 pytest.ini xfail_strict=true 的测试文件中
所有 `@pytest.mark.xfail` / `xfail` / `expected_failure` 注解，输出 staleness 报告。

输出 JSON 报告到 reports/expected_failure_scan_<YYYYMMDD_HHMMSS>.json：
{
  "scanned_at": ISO8601,
  "total_expected_failures": 整数,
  "stale_candidates": [
    { "file": ".../tests/test_xxx.py", "line": N, "test_name": "test_xxx",
      "decorator": "@pytest.mark.xfail(reason=...)",
      "days_since_baseline": 整数, "action": "keep|investigate" }
  ]
}
同时输出 stdout 汇总：总条目数、可能过期（>7天未变更）条目数、建议跟进项数。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = PROJECT_ROOT / "tests"
REPORTS_DIR = PROJECT_ROOT / "reports"
DEFAULT_BASELINE_DATE = date(2026, 7, 19)

PATTERN_XFAIL_DECORATOR = re.compile(
    r"@pytest\.mark\.xfail(\s*\([^)]*\))?", re.MULTILINE
)
PATTERN_PARAM_XFAIL = re.compile(
    r"pytest\.param\([^)]*marks\s*=\s*pytest\.mark\.xfail[^)]*\)", re.MULTILINE
)
PATTERN_EXPECTED_FAILURE_STRICT = re.compile(
    r'mark_id\s*=\s*["\']expected_failure_strict[^"\']*["\']'
)
PATTERN_CUSTOM_XFAIL_MARK = re.compile(
    r"@pytest\.mark\.xfail[a-zA-Z0-9_]*(\s*\([^)]*\))?", re.MULTILINE
)

PATTERN_DEF_TEST = re.compile(
    r"^\s*def\s+(test_[a-zA-Z0-9_]+)\s*\(", re.MULTILINE
)
PATTERN_CLASS_TEST = re.compile(
    r"^\s*class\s+(Test[a-zA-Z0-9_]*)\s*[:(]", re.MULTILINE
)


def resolve_baseline_date() -> date:
    docs_dir = PROJECT_ROOT / "docs"
    if docs_dir.exists():
        for p in docs_dir.glob("migration-baseline-*.json"):
            m = re.search(r"migration-baseline-(\d{4}-\d{2}-\d{2})\.json", p.name)
            if m:
                try:
                    return datetime.strptime(m.group(1), "%Y-%m-%d").date()
                except ValueError:
                    pass
    return DEFAULT_BASELINE_DATE


def find_nearest_test_name(lines: list[str], decorator_line_idx: int) -> str:
    current_class = ""
    current_func = ""
    for i in range(decorator_line_idx):
        line = lines[i]
        cm = PATTERN_CLASS_TEST.match(line)
        if cm:
            current_class = cm.group(1)
            continue
        fm = PATTERN_DEF_TEST.match(line)
        if fm:
            current_func = fm.group(1)
    for i in range(decorator_line_idx, min(decorator_line_idx + 20, len(lines))):
        line = lines[i]
        fm = PATTERN_DEF_TEST.match(line)
        if fm:
            current_func = fm.group(1)
            break
    if current_class and current_func:
        return f"{current_class}.{current_func}"
    return current_func or "(unknown)"


def extract_reason(matched_text: str) -> str:
    m = re.search(r'reason\s*=\s*["\']([^"\']*)["\']', matched_text)
    if m:
        return m.group(1)
    return ""


def truncate_decorator(text: str, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def scan_file(file_path: Path, baseline_date: date) -> list[dict[str, Any]]:
    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    lines = text.splitlines()
    results: list[dict[str, Any]] = []

    file_mtime = datetime.fromtimestamp(file_path.stat().st_mtime).date()
    try:
        days_since = (file_mtime - baseline_date).days
    except TypeError:
        days_since = 0
    if days_since < 0:
        days_since = 0

    def record(match: re.Match[str], decorator: str, reason: str):
        line_no = text[: match.start()].count("\n") + 1
        test_name = find_nearest_test_name(lines, line_no - 1)
        action = "investigate" if days_since > 7 else "keep"
        results.append({
            "file": str(file_path),
            "line": line_no,
            "test_name": test_name,
            "decorator": truncate_decorator(decorator),
            "reason": reason,
            "days_since_baseline": days_since,
            "action": action,
        })

    for m in PATTERN_XFAIL_DECORATOR.finditer(text):
        decorator = m.group(0)
        reason = extract_reason(decorator)
        record(m, decorator, reason)

    for m in PATTERN_PARAM_XFAIL.finditer(text):
        decorator = m.group(0)
        reason = extract_reason(decorator)
        record(m, decorator, reason)

    for m in PATTERN_EXPECTED_FAILURE_STRICT.finditer(text):
        decorator = m.group(0)
        record(m, decorator, "")

    for m in PATTERN_CUSTOM_XFAIL_MARK.finditer(text):
        full = m.group(0)
        if PATTERN_XFAIL_DECORATOR.match(full):
            continue
        record(m, full, "")

    return results


def main() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    baseline_date = resolve_baseline_date()
    scanned_at = datetime.now(timezone.utc).isoformat()

    all_candidates: list[dict[str, Any]] = []
    if TESTS_DIR.exists():
        test_files = sorted(TESTS_DIR.rglob("test_*.py"))
        for tf in test_files:
            all_candidates.extend(scan_file(tf, baseline_date))

    total = len(all_candidates)
    stale_count = sum(1 for c in all_candidates if c["days_since_baseline"] > 7)
    investigate_count = sum(1 for c in all_candidates if c["action"] == "investigate")

    report: dict[str, Any] = {
        "scanned_at": scanned_at,
        "baseline_date": baseline_date.isoformat(),
        "total_expected_failures": total,
        "stale_threshold_days": 7,
        "stale_candidates": all_candidates,
    }

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORTS_DIR / f"expected_failure_scan_{timestamp}.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print("=" * 60)
    print("Expected Failure Staleness Scan Report")
    print("=" * 60)
    print(f"  Scanned at          : {scanned_at}")
    print(f"  Baseline date       : {baseline_date.isoformat()}")
    print(f"  Stale threshold     : > 7 days")
    print(f"  Total xfail entries : {total}")
    print(f"  Stale candidates    : {stale_count} (>7 days since file mtime)")
    print(f"  Suggest follow-ups  : {investigate_count}")
    print(f"  Report file         : {report_path}")
    print("-" * 60)
    if investigate_count > 0:
        print("  Top investigate candidates:")
        for c in sorted(
            (x for x in all_candidates if x["action"] == "investigate"),
            key=lambda x: -x["days_since_baseline"],
        )[:10]:
            rel_file = Path(c["file"]).name
            print(
                f"    [{c['days_since_baseline']:>3}d] "
                f"{rel_file}:{c['line']} {c['test_name']}"
            )
    else:
        print("  No stale candidates detected.")
    print("=" * 60)


if __name__ == "__main__":
    main()
