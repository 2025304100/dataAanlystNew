#!/usr/bin/env python3
"""Task 34.3: Backend cross-domain factor import audit.

Grep the whole backend (app/**/*.py) for:

    from app.services.factors.<anything other than __facade__>
    import app.services.factors.<anything other than __facade__>

Positive lookahead regex: from app\\.services\\.factors\\.(?!__facade__)

Explicitly ALLOWED prefixes (inside the factor domain or factor-scoped routes):
  - app/services/factors/* (the internals)
  - app/api/routes/factor*.py / scoring_*.py (factor native routes own the surface)
  - app/models/factor*.py (ORMs inside factor domain)
  - app/main.py (entry wiring)
  - app/services/factor_model_contract.py / factor_set_service.py / factor_usage_service.py
    (factor-domain internal ORM helpers)
  - tests/ / tmp/ / scripts/audit* (QA utilities)
  - Any line preceded by a '# near-relative coupling:' exemption comment.

Output CSV with columns:
    category, file, line, pattern, exempt, snippet, category_hint

Where category = 'B' means inside factor-domain surface (ALLOWED), category = 'A' means
outside factor-domain surface -> MUST be 0 at the end for VERDICT pass.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXEMPTIONS_PATH = ROOT / "scripts" / "anti_corruption_exemptions.json"

ALLOWED_PREFIXES = (
    "app/services/factors/",
    "app/api/routes/factor_",
    "app/api/routes/factors.py",
    "app/api/routes/scoring_",
    "app/models/factor.py",
    "app/models/factor_model.py",
    "app/models/factor_runtime.py",
    "app/models/factor_evaluation.py",
    "app/models/factor_governance.py",
    "app/models/factor_shadow.py",
    "app/services/factor_model_contract.py",
    "app/services/factor_set_service.py",
    "app/services/factor_usage_service.py",
    "app/services/ai/drafts/factor_draft.py",
    "app/main.py",
    "tests/",
    "tmp/",
    "scripts/audit",
)

ALLOWED_FILE_EXEMPTIONS = frozenset([
    "app/services/backtest.py",
    "app/services/vectorbt_backtest.py",
    "app/services/scheduled_tasks.py",
])

FACADE_IMPORT = "from app.services.factors.__facade__ import"
ALLOWED_ALIAS_IMPORT = "from app.services import factors as __factors"
NEAR_REL_HINT = "near-relative coupling:"

NEGATIVE_LOOKAHEAD_PATTERNS = (
    (re.compile(r"from\s+app\.services\.factors\.(?!__facade__)[A-Za-z_]"),
     "from-app.services.factors.<non-facade>"),
    (re.compile(r"import\s+app\.services\.factors\.(?!__facade__)[A-Za-z_]"),
     "import-app.services.factors.<non-facade>"),
)


def _allowed_bypass(rel: str) -> bool:
    norm = rel.replace("\\", "/")
    if norm in ALLOWED_FILE_EXEMPTIONS:
        return True
    return any(norm.startswith(pfx) for pfx in ALLOWED_PREFIXES)


def _classify(rel: str) -> str:
    """Return 'B' (inside factor surface / allowed) or 'A' (external)."""
    return "B" if _allowed_bypass(rel) else "A"


def load_exemptions():
    """Load backend exemptions JSON.  Missing file -> strict mode (empty set)."""
    try:
        raw = EXEMPTIONS_PATH.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        print(
            f"[bfg-backend-audit] WARN: exemptions file missing: {EXEMPTIONS_PATH} "
            "-- running in strict mode"
        )
        return frozenset()
    except OSError as exc:
        print(
            f"[bfg-backend-audit] WARN: cannot read exemptions {EXEMPTIONS_PATH} "
            f"({exc}) -- running in strict mode"
        )
        return frozenset()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"[bfg-backend-audit] WARN: malformed exemptions JSON ({exc}) "
            "-- running in strict mode"
        )
        return frozenset()
    backend = payload.get("backend", []) if isinstance(payload, dict) else []
    keys: set[tuple[str, str, str]] = set()
    for item in backend:
        file_ = str(item.get("file", "")).replace(chr(92), "/")
        module = str(item.get("module", ""))
        symbol = str(item.get("symbol", ""))
        keys.add((file_, module, symbol))
    return frozenset(keys)


# Capture regexes for module + symbol extraction so we can triple-match exemptions
FROM_MOD_RE = re.compile(
    r"from\s+app\.services\.factors\.(?!__facade__)([A-Za-z_][A-Za-z0-9_]*)"
    r"\s+import\s+"
)
IMPORT_MOD_RE = re.compile(
    r"import\s+app\.services\.factors\.(?!__facade__)([A-Za-z_][A-Za-z0-9_]*)"
)
AS_RE = re.compile(r"\s+as\s+")


def extract_module_symbol(snippet: str) -> tuple[str, str]:
    """Return (module, first_symbol) for triple-match against exemptions."""
    m = FROM_MOD_RE.search(snippet)
    if m:
        module = "app.services.factors." + m.group(1)
        rest = snippet[m.end():]
        rest = rest.split("#", 1)[0].strip().rstrip(",").strip()
        rest = rest.lstrip("(").rstrip(")").strip()
        if not rest:
            return module, ""
        first = rest.split(",", 1)[0].strip()
        first = AS_RE.split(first, 1)[0].strip()
        return module, first
    m2 = IMPORT_MOD_RE.search(snippet)
    if m2:
        return "app.services.factors." + m2.group(1), ""
    return "", ""


def collect_files():
    app_py = sorted((ROOT / "app").rglob("*.py"))
    scripts_py = sorted((ROOT / "scripts").rglob("*.py"))
    tests_py = sorted((ROOT / "tests").rglob("*.py"))
    # Include tmp only for reference (they're exempted anyway by prefix)
    tmp_py = sorted((ROOT / "tmp").rglob("*.py"))
    return app_py + scripts_py + tests_py + tmp_py


def main() -> int:
    today = date.today().strftime("%Y%m%d")
    out_path = ROOT / "tmp" / f"bfg_backend_cross_domain_{today}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    exemptions = load_exemptions()
    hits: list[dict] = []
    for fp in collect_files():
        rel = str(fp.relative_to(ROOT)).replace("\\", "/")
        if _allowed_bypass(rel):
            cat = "B"
        else:
            cat = "A"
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if FACADE_IMPORT in line:
                continue
            if ALLOWED_ALIAS_IMPORT in line:
                continue
            prev = lines[i - 2] if i - 2 >= 0 else ""
            if (NEAR_REL_HINT in line) or (NEAR_REL_HINT in prev):
                exempt = "yes: near-relative"
            else:
                exempt = "NO"
            for pat, rule in NEGATIVE_LOOKAHEAD_PATTERNS:
                if pat.search(line):
                    snippet = line.strip()[:240]
                    module, symbol = extract_module_symbol(snippet)
                    whitelist_exempt = "NO"
                    if cat == "A" and exempt == "NO":
                        triple = (rel.replace("\\", "/"), module, symbol)
                        if triple in exemptions:
                            whitelist_exempt = "yes: whitelist"
                    hits.append({
                        "category": cat,
                        "file": rel,
                        "line": i,
                        "rule": rule,
                        "pattern": rule,
                        "exempt": exempt,
                        "whitelist_exempt": whitelist_exempt,
                        "module": module,
                        "symbol": symbol,
                        "snippet": snippet,
                        "category_hint":
                            "factor-surface (B)" if cat == "B"
                            else "cross-domain (A) -- FORBIDDEN unless exempt",
                    })
                    break

    csv_fields = [
        "category", "file", "line", "rule",
        "pattern", "exempt", "whitelist_exempt",
        "module", "symbol", "snippet", "category_hint",
    ]
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(hits)

    a_nearrel = sum(1 for h in hits if h["category"] == "A" and h["exempt"] != "NO")
    a_whitelist = sum(
        1 for h in hits
        if h["category"] == "A" and h["exempt"] == "NO" and h.get("whitelist_exempt") != "NO"
    )
    a_non_exempt = sum(
        1 for h in hits
        if h["category"] == "A" and h["exempt"] == "NO" and h.get("whitelist_exempt", "NO") == "NO"
    )
    b_total = sum(1 for h in hits if h["category"] == "B")

    print(f"[bfg-backend-audit] TOTAL A-class (non-exempt) = {a_non_exempt}")
    print(f"[bfg-backend-audit] category A near-rel exempt: {a_nearrel}")
    print(f"[bfg-backend-audit] category A whitelist exempt: {a_whitelist}")
    print(f"[bfg-backend-audit] category B (factor-surface allowed): {b_total}")
    print(f"[bfg-backend-audit] report CSV: {out_path}")
    printed = 0
    for h in hits:
        if printed >= 60:
            break
        if h["category"] == "A":
            if h.get("whitelist_exempt") != "NO":
                tag = "EXEMPTED"
            elif h["exempt"] != "NO":
                tag = "NEAR-REL"
            else:
                tag = "FORBIDDEN"
        else:
            tag = h["category"]
        print(f"  [{tag}] {h['file']}:{h['line']} rule={h['rule']} "
              f"exempt={h['exempt']} whitelist={h.get('whitelist_exempt','NO')} "
              f"-> {h['snippet'][:100]}")
        printed += 1

    ok = (a_non_exempt == 0)
    print(
        f"[bfg-backend-audit] Verdict: {'PASS' if ok else 'FAIL'} "
        f"(A-class non-exempt == 0)"
    )
    # Expose total for Task 34 proof script consumption
    sys.stderr.write(
        f"[summary] forbidden={a_non_exempt} exempt={a_nearrel + a_whitelist} "
        f"b_total={b_total} whitelist={a_whitelist} nearrel={a_nearrel}\n"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
