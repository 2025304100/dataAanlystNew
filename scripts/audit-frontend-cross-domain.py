#!/usr/bin/env python3
"""P0.4-2 front-end cross-domain audit (A-class external / B-class internal guard)."""
from __future__ import annotations
import csv, sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend" / "src" / "components"

ALL_FILES = sorted(p for p in FRONTEND.rglob("*")
                   if p.suffix in (".tsx", ".ts") and "__tests__" not in p.parts)

FORBIDDEN_PATTERNS = [
    ("createFactorPipelineTask", "native-pipeline-method"),
    ("getFactorPipelineTask", "native-pipeline-method"),
    ("listFactorPipelineTasks", "native-pipeline-method"),
    ("getFactorPipelineEta", "native-pipeline-method"),
    ("getFactorModels", "native-model-method"),
    ("getFactorModel", "native-model-method"),
    ("activateFactorModel", "native-model-method"),
    ("fallbackFactorModel", "native-model-method"),
    ("trainFactorModel", "native-model-method"),
    ("listFactorSets", "native-factorset-method"),
    ("freezeFactorSet", "native-factorset-method"),
    ("deprecateFactorSet", "native-factorset-method"),
    ("getFactorOverview", "native-overview-method"),
    ("updateFactorSystemConfig", "native-config-method"),
    ("initializeFactorWarehouse", "native-config-method"),
    ("listFactorDefinitions", "native-factorcrud-method"),
    ("getFactorDefinition", "native-factorcrud-method"),
    ("createFactorVersion", "native-factorcrud-method"),
    ("cancelFactorPipelineTask", "native-pipeline-method"),
    ("import type { FactorModelRun }", "cross-domain-type-ref"),
    ("import type {FactorModelRun}", "cross-domain-type-ref"),
    ("/api/v1/factor", "raw-url-bare-call"),
    ("requestJson(\"/api/v1/factor", "bypass-client"),
    ("requestJson(\'/api/v1/factor", "bypass-client"),
    ("requestJson(`/api/v1/factor", "bypass-client"),
]

GUARD_TOKENS = ("factor-domain internal", "do not use outside factor center",
                "internal: factor-center only")


def has_guard(prev: str, curr: str) -> bool:
    hay = (prev + "\n" + curr).lower()
    return any(tok in hay for tok in GUARD_TOKENS)


def classify(file: Path) -> str:
    if "factors" in file.relative_to(FRONTEND).parts \
            or file.name in ("FactorModelSettings.tsx", "FactorCenter.tsx"):
        return "B"
    return "A"


def main() -> int:
    today = date.today().strftime("%Y%m%d")
    out = ROOT / "tmp" / f"p042-frontend-audit-{today}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    hits = []
    for file in ALL_FILES:
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        cat = classify(file)
        rel = str(file.relative_to(ROOT)).replace("\\", "/")
        for i, line in enumerate(lines, 1):
            prev = lines[i-2] if i-2 >= 0 else ""
            for pattern, rule in FORBIDDEN_PATTERNS:
                if pattern not in line:
                    continue
                if cat == "A":
                    hits.append({"category":"A","file":rel,"line":i,"rule":rule,
                                 "pattern":pattern,
                                 "snippet":line.strip()[:200],
                                 "guarded":"n/a (A forbidden)"})
                else:
                    ok = has_guard(prev, line)
                    hits.append({"category":"B","file":rel,"line":i,"rule":rule,
                                 "pattern":pattern,
                                 "snippet":line.strip()[:200],
                                 "guarded":("yes" if ok else "NO")})
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["category","file","line","rule",
                                          "pattern","guarded","snippet"])
        w.writeheader(); w.writerows(hits)
    a_total = sum(1 for h in hits if h["category"]=="A")
    b_un = sum(1 for h in hits if h["category"]=="B" and h["guarded"]=="NO")
    b_total = sum(1 for h in hits if h["category"]=="B")
    print(f"[p042] A-class forbidden hits: {a_total}")
    print(f"[p042] B-class total={b_total} unguarded={b_un}")
    print(f"[p042] Report: {out}")
    for h in hits[:40]:
        print(f"  [{h['category']}] {h['file']}:{h['line']}  {h['rule']} "
              f"guard={h['guarded']}  {h['snippet'][:90]}")
    ok = (a_total == 0) and (b_un <= 5)
    print(f"[p042] Verdict: {'PASS' if ok else 'FAIL'} (A=0, B unguarded<=5)")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
