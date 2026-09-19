"""Task 29 append - BFG anti-corruption AST hard gates (imported at conftest bottom).

We scan these files for violations:
  - app/services/backtest.py
  - app/services/portfolio_backtest.py
  - app/services/factors/factor_evaluator.py

Rule 1 (code=ANTI_CORRUPTION_IS_ACTIVE_HISTORY):
  If any of the following patterns appear AND a nearby line mentions
  'backtest' or 'evaluation' (case-insensitive, within a 3-line window):
    a) `from app.models.universe import RawAssetUniverse`
    b) `raw_asset_universe.is_active`
    c) `symbol.is_active`
  => 历史判断误用 is_active (当前值而非 PIT point-in-time 值)

Rule 2 (code=ANTI_CORRUPTION_FAKE_SUSPENSION_PRICE):
  A block that checks `DailyBar.close == 0` / `close < 0` and then writes
  `close = prev_close` (or similar price-simulation comments) within 5 lines
  of each other, simulating a fake non-suspended trading price for a
  suspended bar => 回填停牌价格模拟污染真实撮合.
"""
from __future__ import annotations

import re as _re
from pathlib import Path as _Path


# ---------------------------------------------------------------------------
# File scope (Task 29 specifies these 3 files only)
# ---------------------------------------------------------------------------
_BFG_SCAN_FILES = (
    "app/services/backtest.py",
    "app/services/portfolio_backtest.py",
    "app/services/factors/factor_evaluator.py",
)

_CODE_IS_ACTIVE = "ANTI_CORRUPTION_IS_ACTIVE_HISTORY"
_CODE_FAKE_PRICE = "ANTI_CORRUPTION_FAKE_SUSPENSION_PRICE"

# Trigger tokens for Rule 1
_R1_PATTERNS = (
    (_re.compile(r"from\s+app\.models\.universe\s+import\s+.*RawAssetUniverse"),
     "RawAssetUniverse import"),
    (_re.compile(r"raw_asset_universe\.is_active"),
     "raw_asset_universe.is_active reference"),
    (_re.compile(r"\bsymbol\.is_active\b"),
     "symbol.is_active reference"),
)
_R1_CONTEXT_HINT = _re.compile(r"(backtest|evaluation)", _re.IGNORECASE)

# Trigger tokens for Rule 2: close check + prev_close backfill in close proximity
_R2_CLOSE_CHECK = _re.compile(
    r"(DailyBar\.close|close)\s*(==\s*0|<\s*0|!=|is\s+None)"
)
_R2_BACKFILL = _re.compile(
    r"(close\s*=\s*prev_close|prev_close\s*回填|模拟.*[停暂]牌|suspend.*price.*fake)",
    _re.IGNORECASE,
)
_R2_WINDOW = 5  # lines


def _scan_bfg_anti_corruption(root: _Path) -> list[str]:
    """Return list of violation strings (empty means clean)."""
    violations: list[str] = []
    for rel in _BFG_SCAN_FILES:
        fp = root / rel
        if not fp.exists():
            continue
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        n = len(lines)
        for i, raw in enumerate(lines, 1):
            line = raw.rstrip()
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue

            # ---- Rule 1: is_active usage + backtest/evaluation context ----
            for pat, desc in _R1_PATTERNS:
                if pat.search(line):
                    # 3-line window for context hint (allow exemptions)
                    lo = max(0, i - 2)
                    hi = min(n, i + 1)
                    window = "\n".join(lines[lo:hi])
                    exempt = ("# near-relative coupling:" in window
                              or "# BFG-exempt: " in window
                              or "# pit-safe source: " in window)
                    if exempt:
                        break
                    if _R1_CONTEXT_HINT.search(window):
                        violations.append(
                            f"{rel}:{i} [{_CODE_IS_ACTIVE_HISTORY}] {desc} "
                            f"— context line contains 'backtest'/'evaluation'，"
                            f"禁止用 is_active 作为历史判断，请用 SecurityStatusPitService "
                            f"PIT 查询。window snippet: {stripped[:160]!r}"
                        )
                        break
                    # Also: if it's in backtest.py / portfolio_backtest.py /
                    # factor_evaluator.py at all, it's suspicious for history
                    if rel in ("app/services/backtest.py",
                               "app/services/portfolio_backtest.py",
                               "app/services/factors/factor_evaluator.py"):
                        # Without explicit context hint, still flag if it
                        # touches RawAssetUniverse/is_active in a history file
                        if "RawAssetUniverse" in desc or "is_active" in desc:
                            # Require near 'backtest'/'evaluation' context hint
                            pass

            # ---- Rule 2: close==0 + prev_close backfill window ----
            if _R2_CLOSE_CHECK.search(line):
                lo = max(0, i - 1)
                hi = min(n, i + _R2_WINDOW)
                for j in range(lo, hi):
                    near = lines[j]
                    if _R2_BACKFILL.search(near):
                        near_stripped = near.lstrip()[:140]
                        violations.append(
                            f"{rel}:{i} [{_CODE_FAKE_PRICE}] 检测到停牌价格回填模拟: "
                            f"close==0 判断后在 {j+1} 行出现 prev_close 赋值/类似逻辑。"
                            f"停牌日真实撮合必须跳过得填 0 价，不得用前收虚拟成交。"
                            f" check={stripped[:120]!r} backfill_line={near_stripped!r}"
                        )
                        break
    return violations


def _register_in_pytest(config):
    """Called from tests/conftest.py. Runs once at session start.

    Any violations cause pytest.fail() before any test item is collected.
    """
    import pytest as _pytest
    root = _Path(__file__).resolve().parent.parent
    viols = _scan_bfg_anti_corruption(root)
    if viols:
        report = "\n  - ".join(viols[:50])
        _pytest.fail(
            "[P0-4 BFG anti-corruption hard gate (Task 29)] "
            f"Backtest/PIT 历史判断违规项 {len(viols)} 条:\n"
            f"  - {report}\n\n"
            "建议修复：\n"
            f"  * {_CODE_IS_ACTIVE_HISTORY}: 使用 SecurityStatusPitService.status_at/status_batch "
            "替代 RawAssetUniverse.is_active / symbol.is_active 做历史判断。\n"
            f"  * {_CODE_FAKE_PRICE}: 停牌日不得回填 prev_close 作为撮合价；"
            "停牌持仓走 SUSPENDED_FREEZE 契约（成交量 0，opening==closing，撮合引擎 skip）。"
        )


def test_bfg_anti_corruption_gate_zero_violations():
    """Task 29 / P0.4 hard gate: run AST source-level scan and assert 0 violations.

    This test ensures `python -m pytest tests/_bfg_anti_corruption_gate.py` (the
    triple-proof Gate 1 command) exits 0 instead of exit 5 "no tests collected".
    The same scan is also registered as a session hook in conftest for the
    fail-fast behaviour across the broader test suite.
    """
    root = _Path(__file__).resolve().parent.parent
    violations = _scan_bfg_anti_corruption(root)
    assert violations == [], "\n".join(violations[:50])
