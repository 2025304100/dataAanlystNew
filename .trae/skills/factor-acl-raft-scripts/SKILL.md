---
name: "factor-acl-raft-scripts"
description: "Generates safe ASCII-only Python patches and raft-style acceptance scripts for P1/P2 factor-ACL changes. Invoke when applying native->scoring facade migrations, simplifying alias blocks, or writing offline+TestClient smoke scripts where heredoc/encoding syntax errors are a risk."
---

# Factor ACL Raft Scripts (Safe Patch + Acceptance Script Pattern)

This skill captures the durable pattern used repeatedly in the factor-ACL anticorruption workflow to apply targeted code patches and run offline+TestClient acceptance scripts without falling afoul of two known pitfalls on Windows PowerShell:

1. Full-width punctuation inside heredoc-delimited Python source causes SyntaxError invalid character even when saving as UTF-8. The pattern avoids this entirely.
2. __facade__.py / DTO / TestClient raft scripts need stable recipes for dataclass fixtures, smoke allowlist iteration, and non-5xx route assertions.

## When to Invoke

Invoke immediately when doing any of the following inside this workspace:

- Simplify a broken wrapper block in app/services/factors/__facade__.py into aliases (SessionLocal / _open_session cleanup fixes).
- Produce a new raft-style tmp/_p0*_raft*.py acceptance script for P0.4-1 / P2.2 / P0.4-4 style verification.
- Write any Python file emitted from a PowerShell heredoc that contains Chinese or non-ASCII punctuation in docstrings.
- Rebuild DTO shape assertions (ALLOWED_MODEL_BRIEF / ALLOWED_FS / REQUIRED_DRAFT) from the master smoke file before running assertions.

Typical trigger phrases:
- fix __facade__ alias wrapper
- write a TestClient raft smoke
- work around heredoc SyntaxError invalid character
- write acceptance for P1.4 / P2.x tail

## 1. ASCII-Safe Patch File Recipe

Rule: if a Python patch/helper file is written by Set-Content -Value heredoc, keep the entire file ASCII-only outside markers that are read back from the target file on disk.

### 1.1 Pattern: Replace an exact block in a big Python file

Skeleton:

```python
from pathlib import Path

TARGET = Path("app/services/factors/__facade__.py")
src = TARGET.read_text(encoding="utf-8")

# Locate boundaries by stable markers (NEVER rely on line numbers across runs)
MARKER_START = "# \u2500\u2500 Facade \u516c\u5171\u5305\u88c5"
MARKER_END = "get_score_factor_definition = get_scoring_factor_definition"
start = src.find(MARKER_START)
end_line = src.find(MARKER_END)
assert start != -1 and end_line != -1, "marker not found"
noqa_end = src.find("\n", end_line + len(MARKER_END))
if noqa_end == -1:
    noqa_end = len(src)
old = src[start:noqa_end]

new = (
    "# Aliases: dual scoring_* / score_* naming exposed; FactorSets reuse list_factorsets.\n"
    "list_score_factor_sets = list_factorsets  # noqa: F401\n"
    "list_score_factor_definitions = list_scoring_factor_definitions  # type: ignore  # noqa: F401\n"
    "get_score_factor_definition = get_scoring_factor_definition  # type: ignore  # noqa: F401\n"
)
assert new != old, "no change"
src = src[:start] + new + src[noqa_end:]
TARGET.write_text(src, encoding="utf-8")
print(f"replaced {len(old)} bytes -> {len(new)} bytes")
```

Apply via PowerShell:

```powershell
Set-Content -Encoding UTF8 -Path tmp/_simplify_ascii.py -Value @
''
< ASCII-only script body here >
''
@
python tmp/_simplify_ascii.py
python -m py_compile $targetFile
```

## 2. Raft Acceptance Script Recipe

For P0.4-1-style OFFLINE+TestClient smoke, reuse the section order below. Order is battle-tested against: no-DB fallback, TestClient lazy init, ScoreModelDetailDTO.factors vs weights drift, and smoke allowlist parsing duplication.

Mandatory sections in order:

1. Smoke allowlist sets parser (ALLOWED_MODEL_BRIEF, ALLOWED_FS, REQUIRED_DRAFT). Source of truth: tmp/_p04_final_smoke.py. Parse with regex + ast.parse. Assert all three sets contain schema_version; print a [KEY FIX] line with n= counts.
2. DTO instantiation offline (NO DB required for A-block and G-block). Construct ScoreModelBriefDTO, ScoreFactorSetDTO, FactorDraftDTO from dataclass defaults plus sample values. If DB initializes cleanly, merge live samples with try/except fallback to synthetic DTOs.
3. Shape assertions. Use dataclasses.asdict(obj) -> set of keys. Extra keys not in allowlist set MUST be empty. Required key lists use tuple literals for stable failure output ordering.
4. E-1 cross-domain URL self-check. Walk lines of _p04_final_smoke.py; skip comment lines and lines containing E-1. Regex /api/v1/factor[^\s]* count MUST be 0.
5. TestClient HTTP assertions (B / C / F blocks). Always wrap with TestClient(app, raise_server_exceptions=False). B-4 members alignment: prefer factors over weights; accept factorset_member_count OR member_count. C-block POST /scoring/tasks: non-exceptional status 200/201/400/409/422/500 PASS; critical test is zero factor_pipeline leakage in error bodies. F-block keyword leakage: 404 body lower() MUST NOT contain factor_pipeline. HARD GATE.

### 2.1 chk() helper + all_pass convention

```python
all_pass = True

def chk(label, ok, d=""):
    global all_pass
    all_pass &= bool(ok)
    prefix = "  [PASS] " if ok else "  [FAIL] "
    print(prefix + f"{label} - {str(d)}"[:220])
```

At tail end:

```python
import time, sys
print()
print(f"<RAFT_NAME> smoke: ALL_PASS={all_pass} ({time.strftime(\"%Y-%m-%d %H:%M:%S\")})")
sys.exit(0 if all_pass else 1)
```

## 3. Validation Checklist After Script Creation

After writing the patch or raft file, run commands in order before the triple audit:

1. python -m py_compile <target_file> — MUST exit 0.
2. python tmp/<raft>.py 2>&1 | Select-Object -Last 30 — MUST end ALL_PASS=True.
3. If target was __facade__.py or scoring_facade.py: run python scripts/audit-backend-cross-domain.py immediately to catch accidental new near-relatives without exemption comments.
4. If target was any frontend component: run tsc --noEmit on the three modified files; 0 NEW errors is the gate (4 legacy errors are acceptable: ImportMeta.env x1, AppContextValue.currentUser x2, ModalProps.disabled x1).

## 4. Anti-patterns to Avoid

- NEVER embed Chinese full-width punctuation inside Python triple-quoted strings within PowerShell heredocs; they corrupt into SyntaxError invalid character.
- NEVER hardcode ALLOWED_MODEL_BRIEF / ALLOWED_FS / REQUIRED_DRAFT in a raft. P2-3 schema_version updates change the master list. Always parse from tmp/_p04_final_smoke.py.
- NEVER assume DTO field names are stable across DETAIL vs LIST payloads. B-4 specifically must tolerate both factors and weights, both member_count and factorset_member_count.
- NEVER use raise_server_exceptions=True on TestClient in a raft smoke; you want 404s and 500s as HTTP status codes, not Python tracebacks that skip F-1 keyword inspection.

## 5. Relationship to factor-acl-governance

This skill is a micro-skill complement to factor-acl-governance. The latter owns the big-stage order (Preflight -> P1.1 -> P1.2 -> P1.3 -> P1.4 -> P2.x -> Section 5 triple); this skill owns the low-level script construction pattern inside each stage that actually writes the patch and raft artifacts correctly on the Windows heredoc + Python 3.12 code path used in this repo.

