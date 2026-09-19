#!/usr/bin/env python3
"""Task 34.2 / Task 9: Frontend cross-domain factor audit + exemptions.

Audits frontend/src/components/**/*.{ts,tsx} for:

1. Python-pattern forbidden import (Task 34 literal requirement):
     `from app.services.factors.<not __facade__>`

2. TypeScript/TSX factor-domain native internal patterns outside FactorCenter /
   FactorModelSettings / Backtest UI components.

3. Reads scripts/anti_corruption_exemptions.json[].frontend to grant EXEMPTED
   status on matched A-class hits using (file, api_url, method) triple match.

Exit code: 0 if (non_exempt_A_frontend == 0) AND (B_unguarded == 0), else 1.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_SRC = ROOT / "frontend" / "src"
EXEMPT_PATH = ROOT / "scripts" / "anti_corruption_exemptions.json"

_YELLOW = "\033[33m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_RESET = "\033[0m"
_USE_COLOR = sys.stderr.isatty() or sys.stdout.isatty()


def _c(text: str, color: str) -> str:
    return color + text + _RESET if _USE_COLOR else text


def load_frontend_exemptions() -> tuple[set[tuple[str, str, str]], list[dict]]:
    if not EXEMPT_PATH.exists():
        return set(), []
    try:
        data = json.loads(EXEMPT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set(), []
    raw = data.get("frontend", []) or []
    s: set[tuple[str, str, str]] = set()
    for it in raw:
        f = it.get("file", "").replace("\\", "/")
        u = _norm_url(it.get("api_url", ""))
        m = (it.get("method", "GET") or "GET").upper()
        s.add((f, u, m))
    return s, raw


_PATH_PARAM_RE = re.compile(r"\{[^}]+\}")
_ENCODE_RE = re.compile(r"\$\{encodeURIComponent\([^)]*\)\}")
_INTERP_RE = re.compile(r"\$\{[^}]+\}")


_TRAILING_ORPHAN_P = re.compile(r"/([A-Za-z0-9_-]+)\{p\}$")

def _norm_url(u: str) -> str:
    """Normalize URL: strip /api/v1 prefix, replace interpolated path params with {p}, strip query.

    A trailing "{p}" DIRECTLY appended (no "/" separator) to a path segment indicates a
    query-string / suffix interpolation variable (e.g. ${suffix}) which should be stripped
    rather than treated as a path parameter.
    """
    if not u:
        return ""
    for prefix in ("/api/v1", "/api"):
        if u.startswith(prefix + "/"):
            u = u[len(prefix):]
        elif u == prefix:
            u = "/"
    # replace ${encodeURIComponent(x)} -> {p}  (before generic ${x})
    u = _ENCODE_RE.sub("{p}", u)
    # replace generic ${x} -> {p}
    u = _INTERP_RE.sub("{p}", u)
    # replace {name} placeholders -> {p}
    u = _PATH_PARAM_RE.sub("{p}", u)
    # strip query / fragment
    for sep in ("?", "#"):
        i = u.find(sep)
        if i >= 0:
            u = u[:i]
    # drop orphan {p} glued to the end of a path segment (no "/" before {p})
    # e.g. /factors{p} -> /factors    (but /factors/{p} -> /factors/{p}, preserved)
    while True:
        new_u = _TRAILING_ORPHAN_P.sub(r"/\1", u, count=1)
        if new_u == u:
            break
        u = new_u
    return u


if FRONTEND_SRC.exists():
    ALL_FILES = sorted(
        p for p in FRONTEND_SRC.rglob("*")
        if p.suffix in (".tsx", ".ts")
        and "__tests__" not in p.parts
        and "test/" not in p.parts
    )
else:
    ALL_FILES = []


PYTHON_CROSS_PAT = re.compile(r"from\s+app\.services\.factors\.(?!__facade__)[A-Za-z_]")

FACTOR_NATIVE_PATTERNS = [
    ("createFactorPipelineTask", "native-pipeline-method"),
    ("getFactorPipelineTask", "native-pipeline-method"),
    ("listFactorPipelineTasks", "native-pipeline-method"),
    ("getFactorPipelineEta", "native-pipeline-method"),
    ("cancelFactorPipelineTask", "native-pipeline-method"),
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
    ("/api/v1/factor", "raw-url-bare-call"),
    ('requestJson("/api/v1/factor', "bypass-client"),
    ("requestJson('/api/v1/factor", "bypass-client"),
    ("requestJson(`/api/v1/factor", "bypass-client"),
    ("import type { FactorModelRun }", "cross-domain-type-ref"),
    ("import type {FactorModelRun}", "cross-domain-type-ref"),
]

GUARD_TOKENS = (
    "factor-domain internal",
    "do not use outside factor center",
    "internal: factor-center only",
    "scoring facade passthrough",
    "allowed: inside factor-center page",
)


def classify(fp: Path) -> str:
    try:
        rel_parts = fp.relative_to(FRONTEND_SRC).parts
    except ValueError:
        rel_parts = fp.name.split("/")
    name = fp.name.lower()
    factor_center_names = ("factor", "factormodel", "factorset",
                           "factorcenter", "factorsettings",
                           "scoring", "factor_eval", "factoreval",
                           "factor_model", "backtest")
    nm = name.replace("_", "").replace("-", "").replace(".tsx", "").replace(".ts", "")
    for tok in factor_center_names:
        if tok in nm:
            return "B"
    joined_parts = "/".join(p.lower() for p in rel_parts)
    dir_keywords = ("/factor", "/scoring", "/backtest", "/factor_eval",
                    "/factormodel", "/factoreval", "/factor_model")
    for kw in dir_keywords:
        if kw in joined_parts:
            return "B"
    return "A"


def has_guard(prev: str, curr: str, prev2: str = "") -> bool:
    hay = "\n".join([prev2, prev, curr]).lower()
    return any(tok.lower() in hay for tok in GUARD_TOKENS)


_METHOD_TOKEN_RE = re.compile(r"""method\s*:\s*["'](GET|POST|PUT|PATCH|DELETE)["']""", re.IGNORECASE)


def _find_matching_paren(text: str, open_idx: int) -> int:
    depth = 0
    in_str = None
    escape = False
    i = open_idx
    while i < len(text):
        ch = text[i]
        if escape:
            escape = False
            i += 1
            continue
        if ch == "\\":
            escape = True
            i += 1
            continue
        if in_str:
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in ('"', "'", "`"):
            in_str = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _extract_url_from_scope(scope: str) -> str:
    """Extract URL from a requestJson argument scope.

    URLs look like:
      `${API}/factors/overview`                       (template literal, backtick-delimited)
      `${API}/factor-models/${encodeURIComponent(x)}/activate`   (with interpolation)
      '/api/v1/factors/config'                        (single-quote string)
      "/api/v1/factors/warehouse/initialize"          (double-quote string)
    """
    # Scan for ${API} and then find the closing quote of the string it lives in.
    api_idx = scope.find("${API}")
    raw_prefix_len = 0
    if api_idx < 0:
        # try /api/v1 bare
        for bare_pat in ("/api/v1/", "/api/v1"):
            idx = scope.find(bare_pat)
            if idx >= 0:
                api_idx = idx + len(bare_pat) - len("/api/v1")
                raw_prefix_len = 0
                break
        if api_idx < 0:
            return ""

    # find what kind of string we're in by scanning BACKWARDS for the opening quote
    start = api_idx
    open_quote = None
    open_quote_idx = -1
    for j in range(start - 1, -1, -1):
        ch = scope[j]
        if ch in ("`", '"', "'"):
            # consider it the opening quote unless it was escaped or preceded by same quote closing
            # (simple heuristic: last unmatched quote is the opener)
            open_quote = ch
            open_quote_idx = j
            break
    if open_quote_idx < 0:
        return ""

    # now scan FORWARD from open_quote_idx + 1 until we find closing open_quote (not escaped)
    url_raw_chars: list[str] = []
    k = open_quote_idx + 1
    escape2 = False
    encode_depth = 0
    while k < len(scope):
        ch = scope[k]
        if escape2:
            url_raw_chars.append(ch)
            escape2 = False
            k += 1
            continue
        if ch == "\\":
            escape2 = True
            url_raw_chars.append(ch)
            k += 1
            continue
        # track encodeURIComponent(...) paren depth so that ')' inside doesn't terminate
        if encode_depth > 0:
            if ch == "(":
                encode_depth += 1
            elif ch == ")":
                encode_depth -= 1
            url_raw_chars.append(ch)
            k += 1
            continue
        if ch == "$" and k + 1 < len(scope) and scope[k + 1] == "{":
            # peek: if encodeURIComponent( -> track depth
            rest = scope[k + 2:]
            if rest.startswith("encodeURIComponent("):
                encode_depth = 1  # the opening '('
                url_raw_chars.append("${encodeURIComponent(")
                k += 2 + len("encodeURIComponent(")
                continue
        if ch == open_quote:
            # reached the end
            break
        url_raw_chars.append(ch)
        k += 1
    raw_url = "".join(url_raw_chars)
    # Now strip ${API} prefix from the raw URL
    raw_url = raw_url.replace("${API}", "")
    # strip any leading /api/v1 inside (defensive)
    return _norm_url(raw_url)


def extract_api_ctx(lines: list[str], idx: int) -> tuple[str, str]:
    window_end = min(idx + 25, len(lines))
    haystack = "\n".join(lines[idx:window_end])

    rj_m = re.search(r"requestJson\s*(?:<[^>]*>)?\s*\(", haystack)
    if not rj_m:
        single_line = lines[idx] if idx < len(lines) else ""
        # fallback scan - useful when requestJson isn't in the window (e.g. B-guarded call sites)
        for token_prefix in ("${API}", "/api/v1/"):
            p = single_line.find(token_prefix)
            if p >= 0:
                return _norm_url(single_line[p:]), "GET"
        return "", "GET"

    open_paren = rj_m.end() - 1
    close_paren = _find_matching_paren(haystack, open_paren)
    if close_paren < 0:
        scope = haystack[open_paren + 1:open_paren + 800]
    else:
        scope = haystack[open_paren + 1:close_paren]

    url = _extract_url_from_scope(scope)
    mm = _METHOD_TOKEN_RE.search(scope)
    method = mm.group(1).upper() if mm else "GET"
    return url, method


def main() -> int:
    exempt_set, exempt_raw = load_frontend_exemptions()
    today = date.today().strftime("%Y%m%d")
    out_path = ROOT / "tmp" / f"bfg_frontend_cross_domain_{today}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    hits: list[dict] = []
    for fp in ALL_FILES:
        rel = str(fp.relative_to(ROOT)).replace("\\", "/")
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        cat = classify(fp)
        for i, line in enumerate(lines, 1):
            prev = lines[i - 2] if i - 2 >= 0 else ""
            prev2 = lines[i - 3] if i - 3 >= 0 else ""

            if PYTHON_CROSS_PAT.search(line):
                api_url, method = extract_api_ctx(lines, i - 1)
                hit = {
                    "category": cat,
                    "file": rel,
                    "line": i,
                    "rule": "python-style:from-app.services.factors.non-facade",
                    "pattern": PYTHON_CROSS_PAT.pattern,
                    "guarded": "n/a (A forbidden)" if cat == "A" else (
                        "yes" if has_guard(prev, line, prev2) else "NO"),
                    "snippet": line.strip()[:240],
                    "api_url": api_url,
                    "method": method,
                    "exempted": "no",
                }
                if cat == "A":
                    key = (rel.replace("\\", "/"), _norm_url(api_url), method.upper())
                    if key in exempt_set:
                        hit["exempted"] = "yes"
                hits.append(hit)

            for pattern, rule in FACTOR_NATIVE_PATTERNS:
                if pattern not in line:
                    continue
                api_url, method = extract_api_ctx(lines, i - 1)
                guarded = has_guard(prev, line, prev2)
                hit = {
                    "category": "A" if cat == "A" else "B",
                    "file": rel,
                    "line": i,
                    "rule": rule,
                    "pattern": pattern,
                    "guarded": ("n/a (A forbidden)" if cat == "A"
                                else ("yes" if guarded else "NO")),
                    "snippet": line.strip()[:240],
                    "api_url": api_url,
                    "method": method,
                    "exempted": "no",
                }
                if cat == "A":
                    key = (rel.replace("\\", "/"), _norm_url(api_url), method.upper())
                    if key in exempt_set:
                        hit["exempted"] = "yes"
                hits.append(hit)
                break

    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "category", "file", "line", "rule",
            "pattern", "guarded", "snippet",
            "api_url", "method", "exempted",
        ])
        writer.writeheader()
        writer.writerows(hits)

    a_total = sum(1 for h in hits if h["category"] == "A")
    a_exempt = sum(1 for h in hits if h["category"] == "A" and h["exempted"] == "yes")
    a_non_exempt = a_total - a_exempt
    b_total = sum(1 for h in hits if h["category"] == "B")
    b_unguarded = sum(
        1 for h in hits if h["category"] == "B" and h["guarded"] == "NO"
    )

    print(f"[bfg-frontend-audit] exemptions loaded: frontend={len(exempt_raw)}")
    print(f"[bfg-frontend-audit] class A total: {a_total}  (EXEMPTED={a_exempt}, non-exempt A-class frontend FORBIDDEN={a_non_exempt})")
    print(f"[bfg-frontend-audit] class B (factor surface internal) total: {b_total} unguarded: {b_unguarded}")
    print(f"[bfg-frontend-audit] report CSV: {out_path}")

    for h in hits[:60]:
        if h["category"] == "A":
            if h["exempted"] == "yes":
                tag = _c("A-EXEMPTED", _YELLOW)
            else:
                tag = _c("A-FORBIDDEN", _RED)
        else:
            g = (h["guarded"] == "yes" or h["guarded"].startswith("n/a"))
            tag = _c("B-guarded", _GREEN) if g else _c("B-UNGUARDED", _RED)
        ctx = f"url={h['api_url'] or '-'} method={h['method']}"
        print(f"  [{tag}] {h['file']}:{h['line']} rule={h['rule']} {ctx} -> {h['snippet'][:90]}")

    ok = (a_non_exempt == 0) and (b_unguarded == 0)
    verdict = _c("PASS", _GREEN) if ok else _c("FAIL", _RED)
    print(f"[bfg-frontend-audit] Verdict: {verdict} "
          f"(non-exempt A-class frontend = 0 required, B_unguarded = 0 required)")
    sys.stderr.write(
        f"[summary] A_total={a_total} A_exempt={a_exempt} A_non_exempt={a_non_exempt} "
        f"B_total={b_total} B_unguarded={b_unguarded}\n"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())