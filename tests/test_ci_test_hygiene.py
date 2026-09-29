"""CI 卫生守护（2026-09-27 体检报告 §十一.4 / §十一.7 的回归防线）。

三件事都真实发生过，且都是"本机看起来正常、仓库/CI 其实是坏的"那一类：

1. **测试盲区**：CI 的 backend-whitebox job 跑的是 `pytest -m whitebox`。任何没写
   `pytestmark` / `pytest.mark.*` 的测试文件会被**静默 deselect**——曾经有 141 个
   文件（2105 个用例）长期处于这种状态，里面的契约红因"不在任何闸门里"藏了两个月。
   本用例把无主文件数钉死为 0，新增文件必须带 marker。

2. **迁移断链**：`.gitignore` 在仓库重建时灌进了上万条逐文件规则，其中 6 行误伤了
   `alembic/env.py` 与 0045..0049 五个迁移脚本；而已跟踪的 0050 依赖被忽略的
   `wps_0023_047_...` → 新克隆/CI 上 `alembic upgrade head` 必断链。本用例校验链路
   连续性与单一 head。

3. **源码被忽略**：真实源码（app/alembic/frontend/src/tests/scripts 下的
   .py/.ts/.tsx）不该出现在"已忽略且未跟踪"列表里；出现就意味着它只存在于某台
   机器的工作区。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.whitebox

REPO_ROOT = Path(__file__).resolve().parents[1]

# 与 pytest.ini 中已注册的 marker 保持一致
KNOWN_MARKERS = ("whitebox", "blackbox", "e2e", "slow", "reap_workers")
_FILE_MARKER_RE = re.compile(
    r"^pytestmark\s*=|pytest\.mark\.(?:" + "|".join(KNOWN_MARKERS) + r")\b", re.M
)

_REV_RE = re.compile(r"^\s*revision(?:\s*:\s*[^=]+)?\s*=\s*['\"]([^'\"]+)", re.M)
_DOWN_RE = re.compile(r"^\s*down_revision(?:\s*:\s*[^=]+)?\s*=\s*['\"]([^'\"]+)", re.M)

# "已忽略且未跟踪"里允许出现的产物/临时目录（这些确实不该入库）
_ALLOWED_IGNORED_PARTS = (
    "__pycache__",
    ".venv",
    "node_modules",
    ".pnpm-store",
    "dist",
    ".pytest_cache",
    ".tmp",
    "tmp_",
    "test_output",
    ".workbuddy",
    ".pycache_tmp",
    ".pip_packages",
    ".edge",
    ".chrome",
)


def _git(*args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
    except FileNotFoundError:  # 极窄环境：无 git 可执行文件
        pytest.skip("git 不可用，跳过仓库卫生校验")
    if proc.returncode != 0:
        pytest.skip(f"git 调用失败，跳过（{proc.stderr.strip()[:120]}）")
    return proc.stdout


# ══════════════════════════════════════════════════════════
# 1. 测试盲区棘轮
# ══════════════════════════════════════════════════════════

def test_every_test_file_declares_a_ci_marker():
    """tests/ 下每个 test_*.py 都必须声明 marker，否则 CI 的 -m whitebox 会漏掉它。

    基线是 0：2026-09-27 已把当时全部 141 个无主文件补齐（139 个补 whitebox +
    2 个此前已单独处理）。新增测试文件如果没写 marker，这里会直接红。
    """
    unmarked: list[str] = []
    for path in sorted(REPO_ROOT.glob("tests/**/test_*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if not _FILE_MARKER_RE.search(text):
            unmarked.append(path.relative_to(REPO_ROOT).as_posix())
    assert not unmarked, (
        f"{len(unmarked)} 个测试文件没有任何 CI marker，`pytest -m whitebox` 会静默"
        f"跳过它们（CI 盲区，体检报告 §十一.4）。请在文件顶部加 "
        f"`pytestmark = pytest.mark.whitebox`（打活服务的用 blackbox，浏览器端到端"
        f"用 e2e）：\n" + "\n".join(unmarked[:50])
    )


# ══════════════════════════════════════════════════════════
# 2. alembic 迁移链完整性
# ══════════════════════════════════════════════════════════

def test_alembic_chain_is_contiguous_with_single_head():
    """每个 down_revision 都能在同目录找到父迁移，且只有一个 head。

    曾经 0045..0049 五个迁移被 .gitignore 排除，工作区里跑得好、克隆里直接断链，
    所以这个校验必须进 CI 而不是只在某台机器上成立。
    """
    versions_dir = REPO_ROOT / "alembic" / "versions"
    files = sorted(versions_dir.glob("*.py"))
    assert files, "alembic/versions 下没有迁移文件"

    revisions: set[str] = set()
    down_of: dict[str, str] = {}
    for f in files:
        text = f.read_text(encoding="utf-8", errors="ignore")
        m = _REV_RE.search(text)
        if not m:
            pytest.fail(f"{f.name} 未能解析出 revision（格式异常）")
        rev = m.group(1)
        revisions.add(rev)
        d = _DOWN_RE.search(text)
        if d:
            down_of[rev] = d.group(1)

    broken = {c: p for c, p in down_of.items() if p not in revisions}
    assert not broken, f"迁移链断链（缺失父 revision）：{broken}"

    heads = sorted(revisions - set(down_of.values()))
    assert len(heads) == 1, f"应只有一个 head，实际 {heads}"


def test_alembic_env_is_present():
    """`alembic/env.py` 是迁移运行环境（幂等补丁/URL 解析），必须存在且被引用。

    它在 2026-09-27 之前被 .gitignore 排除、从未入库；这里做最低限度的存在性
    校验，配合下面第 3 项的"未被忽略"校验一起兜住。
    """
    env = REPO_ROOT / "alembic" / "env.py"
    assert env.is_file(), "缺少 alembic/env.py：新克隆将无法执行任何迁移"
    assert "disable_existing_loggers=False" in env.read_text(
        encoding="utf-8", errors="ignore"
    ), (
        "env.py 的 fileConfig 必须 disable_existing_loggers=False，否则进程内跑迁移"
        "会把宿主进程的全部 logger 静默，后续 caplog 断言集体假失败（§十一.5）"
    )


# ══════════════════════════════════════════════════════════
# 3. 源码不得处于"被忽略且未跟踪"状态
# ══════════════════════════════════════════════════════════

def test_no_source_files_are_untracked_and_ignored():
    """app/alembic/frontend/src/tests/scripts 下的源码不得只活在某台机器的工作区。"""
    out = _git("ls-files", "--others", "--ignored", "--exclude-standard")
    offenders: list[str] = []
    for line in out.splitlines():
        line = line.strip().replace("\\", "/")
        if not line:
            continue
        if not line.startswith(("app/", "alembic/", "frontend/src/", "tests/", "scripts/")):
            continue
        if not line.endswith((".py", ".ts", ".tsx", ".sql", ".json", ".ini", ".mako")):
            continue
        low = line.lower()
        if any(part in low for part in _ALLOWED_IGNORED_PARTS):
            continue
        offenders.append(line)
    assert not offenders, (
        "以下源码被 .gitignore 排除且未跟踪——新克隆/CI 里它们不存在"
        "（体检报告 §十一.7）：\n" + "\n".join(sorted(offenders)[:50])
    )


# ══════════════════════════════════════════════════
# 4. 闸门不得“零覆盖假绿”（§十五）
# ══════════════════════════════════════════════════

def test_live_backend_guard_fails_instead_of_skipping_when_required(monkeypatch):
    """REQUIRE_LIVE_BACKEND=1 时，“活服务不在”必须是 failure 而不是 skip。

    实测背景：后端挂掉时 `pytest -m blackbox` 跑出 79 skipped / 0 failed，
    CI job 会当成通过——blackbox/e2e 完全没跑但显示“绿”。本用例钉住那个防线本身。
    """
    from tests._live_backend_guard import skip_or_fail_no_live_backend

    monkeypatch.setenv("REQUIRE_LIVE_BACKEND", "1")
    with pytest.raises(pytest.fail.Exception):
        skip_or_fail_no_live_backend(RuntimeError("connection refused"))


def test_live_backend_guard_still_skips_for_local_runs(monkeypatch):
    """没设变量时仍走 skip，本地没起后端的开发不应被误红。"""
    from tests._live_backend_guard import skip_or_fail_no_live_backend

    monkeypatch.delenv("REQUIRE_LIVE_BACKEND", raising=False)
    with pytest.raises(pytest.skip.Exception):
        skip_or_fail_no_live_backend(RuntimeError("connection refused"))


def test_e2e_coverage_guard_rejects_skip_only_runs(monkeypatch):
    """e2e 全跳光（0 条真跑）在 CI 下必须判失败 —— skip 不能凑数。

    背景（报告 §十六.2 待办第 2 条）：`_live_backend_guard` 只回答"服务在不在"；
    浏览器 channel 配错、选择器大面积退化成 skip、甚至 `-m e2e` 选择集被删空，
    都会留下"0 failed 但几乎没跑"的绿灯。本用例钉住那道最低真跑数闸门本身。
    """
    from tests._e2e_coverage_guard import evaluate_coverage

    monkeypatch.delenv("E2E_MIN_EXECUTED", raising=False)
    message = evaluate_coverage(0, 39, required=True)
    assert message is not None, "0 条真跑 + 39 条 skip 必须判失败"
    assert "几乎没跑" in message

    # skip 再多也不能替真跑数达标
    assert evaluate_coverage(5, 200, required=True, minimum=20) is not None
    # 本地未开启时不判定
    assert evaluate_coverage(0, 39, required=False) is None


def test_e2e_coverage_guard_threshold_falls_back_instead_of_crashing(monkeypatch):
    """阈值来自环境变量；坏值必须回退默认而不是把闸门自己弄崩。"""
    from tests import _e2e_coverage_guard as guard

    monkeypatch.delenv("E2E_MIN_EXECUTED", raising=False)
    assert guard.min_executed_threshold() == guard.DEFAULT_MIN_EXECUTED

    monkeypatch.setenv("E2E_MIN_EXECUTED", "not-a-number")
    assert guard.min_executed_threshold() == guard.DEFAULT_MIN_EXECUTED

    monkeypatch.setenv("E2E_MIN_EXECUTED", "10")
    assert guard.min_executed_threshold() == 10
    assert guard.evaluate_coverage(12, 0, required=True) is None


def test_e2e_minimum_gate_is_wired_into_ci():
    """只写代码不接线等于没有：CI 步骤必须真的打开这道闸。"""
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "test.yml").read_text(
        encoding="utf-8"
    )
    assert "E2E_REQUIRE_MINIMUM" in workflow

    # 而且必须挂在跑 e2e 的那一步，不是别的 job
    e2e_step = workflow.split("Run E2E tests", 1)[1].split("- name:", 1)[0]
    assert "E2E_REQUIRE_MINIMUM" in e2e_step, "e2e 步骤没打开最低真跑数闸门"

    # 钩子必须装在根 conftest：选择集被删空时 tests/e2e/conftest.py 不会被加载
    conftest = (root / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "def pytest_sessionfinish" in conftest
    assert "_e2e_coverage_guard" in conftest


def test_factor_warehouse_is_isolated_from_developer_machine_state():
    """PT-DEF-21：测试会话必须用**专属**因子仓库，不能与本机后端共用同一个文件。

    实测代价（两次独立复现）：
    1. 后端在跑时全量出现 7 例红，真因是共享 `tmp/factor_warehouse.duckdb` 被另一个
       进程持有（"另一个程序正在使用此文件"），但错误表现成 `assert 0 == 2` /
       `assert False is True`，完全看不出根因；
    2. 共享仓库里的残留数据让用例随执行顺序飘移（体检报告 §十九.2：单跑绿、全量红）。
    本用例钉住 conftest 里那个 env 默认值仍然生效（它必须在 import app 之前设置）。
    """
    from app.core.config import Settings

    path = Settings().factor_warehouse_path
    assert path.name.startswith("qa_factor_warehouse_"), (
        f"测试会话正在用与开发机共享的仓库 {path}；"
        "tests/conftest.py 里 FACTOR_WAREHOUSE_PATH 的默认值失效了，"
        "本机只要跑着后端，全量就会出现无法归因的红灯"
    )
