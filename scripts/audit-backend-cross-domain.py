#!/usr/bin/env python3
"""P0.4-3 backend cross-domain import audit (non-factor-domain vs forbidden imports)."""
from __future__ import annotations
import csv, re, sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

FILE_PATHS = [
    APP / "services" / "alerts.py",
    APP / "services" / "candidate_promote.py",
    APP / "services" / "discovery_data_prep.py",
]
GLOB_RULES = [
    (APP / "services", "candidate_*.py"),
    (APP / "routes", "portfolio*.py"),
    (APP / "routes", "backtest*.py"),
    (APP / "routes", "dashboard*.py"),
    (APP / "routes", "custom_indicator*.py"),
]

FORBIDDEN_RE = [
    (r"from\s+app\.models\.factor_model\s+import", "direct-factor_model-orm"),
    (r"import\s+app\.models\.factor_model\b", "direct-factor_model-orm"),
    (r"from\s+app\.models\.factor\s+import", "direct-factor-orm"),
    (r"import\s+app\.models\.factor\b[^_]", "direct-factor-orm"),
    (r"from\s+app\.models\.factor_runtime\s+import.*ActiveScoreScope", "direct-active-score-scope"),
    (r"from\s+app\.services\.factors\.ridge_model\b", "direct-ridge-model"),
    (r"from\s+app\.services\.factors\.pipeline_task\b", "direct-pipeline-task"),
    (r"from\s+app\.services\.factors\.warehouse_locks\b", "direct-warehouse-locks"),
]
ALLOWED_ONLY = "from app.services.factors.__facade__ import"
NEAR_HINT = "near-relative coupling"


def collect_files():
    seen, out = set(), []
    def add(p):
        if not p.exists(): return
        r = p.resolve()
        if r in seen: return
        seen.add(r); out.append(p)
    for p in FILE_PATHS: add(p)
    for root, pat in GLOB_RULES:
        for p in root.glob(pat): add(p)
    return sorted(out)


def main() -> int:
    today = date.today().strftime("%Y%m%d")
    out = ROOT / "tmp" / f"p043-backend-audit-{today}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    hits = []
    for file in collect_files():
        rel = str(file.relative_to(ROOT)).replace("\\", "/")
        try:
            lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if not s or s.startswith("#"): continue
            if ALLOWED_ONLY in line: continue
            for pat, rule in FORBIDDEN_RE:
                if re.search(pat, line):
                    prev = lines[i-2] if i-2 >= 0 else ""
                    g = (NEAR_HINT in line) or (NEAR_HINT in prev)
                    hits.append({"file":rel,"line":i,"rule":rule,"pattern":pat,
                                 "snippet":s[:240],
                                 "exempt":("yes: near-relative" if g else "NO")})
                    break
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["file","line","rule","pattern",
                                          "exempt","snippet"])
        w.writeheader(); w.writerows(hits)
    bad = sum(1 for h in hits if h["exempt"]=="NO")
    nr = sum(1 for h in hits if h["exempt"]!="NO")
    print(f"[p043] forbidden (non-exempt): {bad}  near-rel exempt: {nr}")
    print(f"[p043] report: {out}")
    for h in hits[:60]:
        print(f"  - {h['file']}:{h['line']} {h['rule']} exempt={h['exempt']} {h['snippet'][:100]}")
    ok = (bad == 0)
    print(f"[p043] Verdict: {'PASS' if ok else 'FAIL'} (forbidden == 0)")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
