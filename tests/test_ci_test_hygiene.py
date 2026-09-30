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


# ══════════════════════════════════════════════════
# 5. 前端孤儿组件棘轮（体检报告 §二十五）
# ══════════════════════════════════════════════════

_FRONTEND_SRC = Path(__file__).resolve().parents[1] / "frontend" / "src"
_FRONTEND_ROOT = Path(__file__).resolve().parents[1] / "frontend"
_IMPORT_RE = re.compile(r"""(?:from|import)\s*\(?\s*['"]([^'"]+)['"]""")
_ENTRY_SUFFIXES = ("", ".tsx", ".ts", ".jsx", ".js", "/index.tsx", "/index.ts")

# 已知孤儿基线（相对 frontend/src 的 posix 路径）：只允许变短，不允许变长。
# 2026-09-29 已清空：报告 §二十六/§二十七 里那 7 个从应用入口不可达的旧组件
# （Trading / PortfolioWorkbench / PortfolioMembersPanel / AutoTradePanel /
#  PortfolioBacktestPanel / PortfolioPerformancePanel / MiningExperiencePage）
# 连同各自测试一并删除，复盘能力改由 PortfolioReviewDrawer 承接。
# 基线为空意味着：今后任何新增的"没人渲染"组件都会直接被判红。
KNOWN_ORPHAN_COMPONENTS: set[str] = set()


def _frontend_entries() -> list[Path]:
    """应用入口：main.tsx / standalone 入口 + 各 html 的 <script src>。"""
    found: list[Path] = []
    for name in ("main.tsx", "universe-standalone.tsx"):
        candidate = _FRONTEND_SRC / name
        if candidate.is_file():
            found.append(candidate)
    if _FRONTEND_ROOT.exists():
        for html in _FRONTEND_ROOT.glob("*.html"):
            text = html.read_text(encoding="utf-8", errors="ignore")
            for match in re.finditer(r'<script[^>]+src="([^"]+)"', text):
                raw = match.group(1).lstrip("/")
                for candidate in (
                    _FRONTEND_ROOT / raw,
                    _FRONTEND_SRC / raw.removeprefix("src/"),
                ):
                    if candidate.is_file():
                        found.append(candidate)
    return sorted(set(found))


def _resolve_module(spec: str, from_file: Path) -> Path | None:
    if not spec.startswith("."):
        return None  # 裸包名（react / antd / …）不是本地模块
    base = (from_file.parent / spec).resolve()
    for suffix in _ENTRY_SUFFIXES:
        candidate = Path(str(base) + suffix)
        if candidate.is_file():
            return candidate
    return None


def _reachable_from_entries() -> set[Path]:
    seen: set[Path] = set()
    stack = _frontend_entries()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        try:
            text = current.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in _IMPORT_RE.finditer(text):
            target = _resolve_module(match.group(1), current)
            if target is not None:
                stack.append(target)
    return seen


def _orphan_components() -> set[str]:
    components_dir = _FRONTEND_SRC / "components"
    if not components_dir.is_dir():  # pragma: no cover - 仓库结构坏了由其它守护兜底
        pytest.fail(f"前端组件目录不存在：{components_dir}")
    entries = _frontend_entries()
    assert entries, "找不到任何前端入口（main.tsx / html），可达性分析无法进行"
    reachable = _reachable_from_entries()
    orphans: set[str] = set()
    for path in components_dir.rglob("*.tsx"):
        rel = path.relative_to(_FRONTEND_SRC).as_posix()
        if "__tests__" in rel or rel.endswith((".test.tsx", ".spec.tsx")):
            continue
        if path.resolve() not in reachable:
            orphans.add(rel)
    return orphans


def test_frontend_has_no_new_orphan_components():
    """禁止再新增「用户看不到、只有自己的测试在跑」的前端组件。

    为什么值得守：孤儿组件的测试会一直绿，却保护着永远不渲染的界面。本项目里
    真实发生过的伤害是——我按旧孤儿组件里的 id 判定「手动交易入口缺失」，
    误立 PT-DEF-22（现役其实把它换成了持仓成员行内动作）。
    """
    orphans = _orphan_components()
    introduced = sorted(orphans - KNOWN_ORPHAN_COMPONENTS)
    assert not introduced, (
        f"新增了无人渲染的前端组件：{introduced}。它自己的测试会一直绿但用户永远看不到。"
        "请二选一：① 从入口/路由真正挂载；② 连同它自己的测试一起删除。"
        "确属必要的中间产物，才加进 KNOWN_ORPHAN_COMPONENTS 并在报告里写明理由。"
    )
    # 债务必须单调递减（修好了就从基线里删）
    assert len(orphans) <= len(KNOWN_ORPHAN_COMPONENTS), (
        f"孤儿组件数量增长：{sorted(orphans)}"
    )
    fixed = sorted(KNOWN_ORPHAN_COMPONENTS - orphans)
    if fixed:
        print(f"[孤儿棘轮] 这些历史孤儿已不再孤立，请从基线移除：{fixed}")


# ══════════════════════════════════════════════
# 12. .gitignore 必须保持"规则化"，且不得吞掉任何已跟踪文件
# ══════════════════════════════════════════════

_GITIGNORE_MAX_ENTRIES = 140


def test_gitignore_is_rule_based_and_hides_nothing_tracked():
    """.gitignore 只允许目录/后缀/形状规则，禁止逐文件黑名单。

    真实伤害（§十一.7）：仓库重建时这里灌进 10,237 行逐文件条目（其中 10,077 行
    是 .venv/ 下的一个个文件），6 行误伤 alembic/env.py 与 0045..0049 五个迁移；
    本机一切正常，新克隆与 CI 上 `alembic upgrade head` 直接断链。
    本轮（§二十九）又发现同类新问题：未锚定的 `scan_*.py`/`check_*.py` 会连带
    吞掉 scripts/ 下的正式工具 —— 所以这里不仅查条目数，还直接问 git。
    """
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8", errors="ignore")
    entries = [
        ln.strip() for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]

    assert len(entries) <= _GITIGNORE_MAX_ENTRIES, (
        f".gitignore 有效条目 {len(entries)} 条，超过上限 {_GITIGNORE_MAX_ENTRIES}："
        "十有八九是又有人把未跟踪清单逐行贴进来。请改成目录/后缀规则。"
    )
    # 注意：".venv/" 本身是正当的整目录规则，只有它下面的具体文件路径才是黑名单
    per_file_venv = [e for e in entries if e.startswith(".venv/") and e.rstrip("/") != ".venv"]
    assert not per_file_venv, (
        f".gitignore 里出现了 .venv 下的逐文件条目（{len(per_file_venv)} 条）。"
        "虚拟环境必须整目录忽略；逐文件列举既会长到上万行，也挡不住新装包。"
    )
    assert any(e.rstrip("/") in {".venv", ".venv*"} for e in entries), "必须整目录忽略 .venv/"

    swallowed = _git("ls-files", "-i", "-c", "--exclude-standard").splitlines()
    assert not swallowed, (
        "以下**已跟踪**文件正被 .gitignore 匹配（tracked ∩ ignored 必须为空）：\n"
        + "\n".join(s.strip() for s in swallowed[:30])
        + "\n要么这些文件本就该出库（git rm），要么规则写得太宽（用 / 锚定到仓库根）。"
    )


# ══════════════════════════════════════════════
# 13. 仓库根不得再堆积一次性脚本与结果转储
# ══════════════════════════════════════════════

# 只统计**已入库**的根文件：这样在 CI 的新克隆上也成立，不依赖某台机器的残留产物。
_ROOT_TRACKED_FILE_BASELINE = 47

# 这些形状属于"诊断期用完即弃"，不该出现在仓库里（正式测试进 tests/，工具进 scripts/）
_ROOT_FORBIDDEN_PREFIXES = (
    "_bb", "_diag_", "_dbg_", "_tmp_", "_debug_", "_dd_", "_patch_", "_verify_",
    "_http_verify_", "_tr", "check_", "clean_", "find_", "fix_", "scan_",
    "qa_blockers_", "perf_report", "openapi_schema",
)
_ROOT_FORBIDDEN_SUFFIXES = ("-result.txt", "result.txt", ".tmp.ts")


def test_repo_root_has_no_new_junk_committed():
    """仓库根只该放工程入口，不该放过程性产物。"""
    root_files = sorted(p.strip() for p in _git("ls-files").splitlines() if "/" not in p.strip())

    junk = [
        f for f in root_files
        if f.lower().startswith(_ROOT_FORBIDDEN_PREFIXES)
        or f.endswith(_ROOT_FORBIDDEN_SUFFIXES)
    ]
    assert not junk, (
        f"仓库根混入了过程性产物：{junk[:20]}。"
        "一次性脚本放进 tmp/（已忽略）或直接删；正式用例进 tests/ 并带 marker。"
    )
    assert len(root_files) <= _ROOT_TRACKED_FILE_BASELINE, (
        f"根目录已跟踪文件从 {len(root_files)} 增长趋势超出基线 "
        f"{_ROOT_TRACKED_FILE_BASELINE}：{root_files}。"
        "新文件请放进对应目录（scripts/、docs/），不要堆在根上。"
    )


# ══════════════════════════════════════════════
# 14. 测试与应用源码必须能被编译（语法坏了收集阶段就撞墙）
# ══════════════════════════════════════════════

def test_python_sources_have_no_syntax_errors():
    """tests/ 与 app/ 下不得有语法错误的 .py。

    真实发生过的类型：一份黑盒测试文件首行模块 docstring 被误加了两个空格
    （编辑器手滑），`IndentationError: unexpected indent (line 1)` 让整档无法
    收集——不是某个用例红，而是整个黑盒闸门的这一档直接挂。那种问题
    靠肉眼下轮才找得到，所以交给机器。

    实现细节：用内置 `compile()` 而不是 `py_compile.compile(cfile=os.devnull)` ——
    后者在 Windows 上对**每一个**文件都报“nul is a non-regular file...”，
    守护会变成永远红的噪声（实测 799/799 全报“语法错”），反而把真问题埋掉。
    """
    broken: list[str] = []
    for base in ("tests", "app"):
        for path in sorted((REPO_ROOT / base).rglob("*.py")):
            src = path.read_text(encoding="utf-8-sig", errors="replace")
            try:
                compile(src, str(path), "exec")
            except SyntaxError as exc:
                broken.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}:{exc.lineno} "
                    f"{type(exc).__name__}: {exc.msg}"
                )
    assert not broken, (
        "以下源码有语法错误，pytest 到收集阶段就会直接挂：\n" + "\n".join(broken)
    )


# ══════════════════════════════════════════════
# 15. 测试不得把临时数据库建在仓库根
# ══════════════════════════════════════════════

def test_tests_do_not_create_temp_databases_at_repo_root():
    """用例的临时库必须放系统临时目录 / tmp_path，不能建在仓库根。

    实测伤害（§三十）：integration 的 conftest 曾经 `mkstemp(dir=str(ROOT))`，
    而收尾的 unlink 在 Windows 上会撞 `PermissionError(13)`（已留下 58 条失败记录），
    删不掉就直接在项目根堆出 **344 个 integration_*.sqlite3（257MB）**。
    只要建在根上，一次崩溃/一次索引锁定就会弄脏工作树，所以这条当硬规则定住。
    """
    offenders: list[str] = []
    import ast

    for f in (REPO_ROOT / "tests").rglob("*.py"):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        # 用 AST 只抓真实代码里的 mkstemp(...) 调用：第一版用正则扫文本，
        # 结果被本用例自己的注释（里面写了 mkstemp(dir=str(ROOT))）误报。
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", "") != "mkstemp" and getattr(
                node.func, "attr", ""
            ) != "mkstemp":
                continue
            for kw in node.keywords:
                if kw.arg == "dir" and "ROOT" in ast.unparse(kw.value):
                    offenders.append(
                        f"{f.relative_to(REPO_ROOT).as_posix()}:{node.lineno} "
                        f"mkstemp(dir={ast.unparse(kw.value)})"
                    )
    assert not offenders, (
        "以下用例代码把临时文件建在了仓库根（应该用 tempfile.gettempdir() 子目录"
        "或 pytest 的 tmp_path）：\n" + "\n".join(offenders)
    )


# ══════════════════════════════════════════
# 16. dev 慢测试名单不得含已不存在的用例名
# ══════════════════════════════════════════

_DEV_XFAIL_NAMES_RE = re.compile(r"^def (test_[A-Za-z0-9_]+)\(", re.M)


def test_dev_hardware_xfail_names_still_exist():
    """`_SLOW_FUNCTION_NAMES` 里每个名字都必须对应一个真实存在的用例函数。

    为什么值得守：dev 硬件判定会给这批用例自动挂 xfail(strict=False)，名字不在名单里
    就会默默失效——名单看起来还在“管东西”，实际已经管不到任何事。
    实测发生过：探测改成任务化后，名单里 6 个用例名（test_probe_returns_within_30s /
    test_batch_probe_completes_within_180s / test_consecutive_probes_* 等）全部成为死名字。
    死名字不是无害的：它会让下一个人以为这些守护还在。
    """
    import tests.conftest as root_conftest

    names = set(getattr(root_conftest, "_SLOW_FUNCTION_NAMES", set()))
    assert names, "_SLOW_FUNCTION_NAMES 不应被清空（清空等于取消全部 dev 保护，需重新评估）"

    existing: set[str] = set()
    for f in (REPO_ROOT / "tests").rglob("test_*.py"):
        existing.update(_DEV_XFAIL_NAMES_RE.findall(f.read_text(encoding="utf-8", errors="ignore")))

    dead = sorted(names - existing)
    assert not dead, (
        f"dev 慢测试名单里有 {len(dead)} 个不存在的用例名：{dead}。"
        "用例已重构/删除就要同步清掉，否则名单会给人「这些守护还在」的错觉。"
    )


# 只允许这些“性能/硬件慢”用例被 dev 判定放宽。新增必须连理由一起加进来评审。
PERF_RELAXATION_ALLOWED = {
    "test_dashboard_overview",     # T4 FR-4.1：dashboard 15s 慢查询保护
    "test_dashboard_workbench",    # 同上，实测 14.21s 贴着超时线
}

_DEV_MARKER_RE = re.compile(
    r"@pytest\.mark\.xfail_dev_hardware\s*\n\s*def (test_[A-Za-z0-9_]+)\(", re.M
)


def test_dev_hardware_relaxation_is_performance_only():
    """dev 放宽只能给慢查询用，不得用它关掉正确性契约门禁。

    为什么要定这条：CI 上没有 2GB DuckDB、universe 也是空的（查询失败直接算 dev），
    而且 CI 从不传 --hardware-capability=prod —— **进了 dev 放宽名单就等于在 CI 上
    永久不计入结果**，该契约再坏也不会红。
    实测发生过：两个观察池契约用例（0.05s / 0.02s，跟硬件无关）被挂在名单里，
    直到全量黑盒跑出 3 个 xpassed 才暴露（报告 §“3 个 xpassed”）。
    """
    import tests.conftest as root_conftest

    names = set(getattr(root_conftest, "_SLOW_FUNCTION_NAMES", set()))
    off_policy = sorted(names - PERF_RELAXATION_ALLOWED)
    assert not off_policy, (
        f"dev 放宽名单里出现了非性能类用例：{off_policy}。"
        "把它们挂上来就等于在 CI 上默默取消这几条门禁；正确性契约不该被放宽。"
    )

    # 另一条通道：直接挂装饰器的也要管住（装饰器与名单是两套挂口）
    decorated: set[str] = set()
    for f in (REPO_ROOT / "tests").rglob("test_*.py"):
        decorated.update(
            _DEV_MARKER_RE.findall(f.read_text(encoding="utf-8", errors="ignore"))
        )
    bad_decorated = sorted(decorated - PERF_RELAXATION_ALLOWED)
    assert not bad_decorated, (
        f"以下用例挂了 @pytest.mark.xfail_dev_hardware 但不是性能类用例：{bad_decorated}"
    )


# ══════════════════════════════════════════
# 18/19. 内部 code 的中文标签不得漏配（后端↔前端跨语校验）
# ══════════════════════════════════════════

_TASK_TYPE_ASSIGN_RE = re.compile(r'task_type\s*=\s*"([a-z][a-z0-9_]*)"')
_CREATE_TASK_RE = re.compile(r'create_async_task\(\s*"([a-z][a-z0-9_]*)"')
# 实测口径教训：只扫 `task_type="..."` 仅能挑到 4 个值，而后端真实有 28 种 ——
# 因为主要写法是常量赋值（`XXX_TASK_TYPE = "factor_mining"`）和默认值。
# 扫窄了会交出一根“看着绿、几乎不覆盖”的守护，比没守护更危险。
_TASK_TYPE_CONST_RE = re.compile(
    r'^\s*_?[A-Z0-9_]*TASK_TYPE[A-Z0-9_]*\s*=\s*"([a-z][a-z0-9_]*)"', re.M
)
_TASK_TYPE_DEFAULT_RE = re.compile(r'task_type"?\s*:\s*"([a-z][a-z0-9_]*)"')
_TASK_TYPE_PAYLOAD_RE = re.compile(r'"task_type":\s*"([a-z][a-z0-9_]*)"')
_LABEL_MAP_ENTRY_RE = re.compile(r'^\s{2}([a-z][a-z0-9_]*):\s*"(taskType[A-Za-z0-9]*)"', re.M)


def _backend_task_types() -> set[str]:
    found: set[str] = set()
    for path in (REPO_ROOT / "app").rglob("*.py"):
        src = path.read_text(encoding="utf-8", errors="ignore")
        for rx in (
            _TASK_TYPE_ASSIGN_RE,
            _CREATE_TASK_RE,
            _TASK_TYPE_CONST_RE,
            _TASK_TYPE_DEFAULT_RE,
            _TASK_TYPE_PAYLOAD_RE,
        ):
            found.update(rx.findall(src))
    # external_sync_<dataset> 是运行时拼前缀，不属于固定取值
    return {t for t in found if not t.startswith("external_sync_")}


def _frontend_task_type_map() -> dict[str, str]:
    src = (REPO_ROOT / "frontend" / "src" / "utils" / "taskTypeLabel.ts").read_text(
        encoding="utf-8", errors="ignore"
    )
    return {code: key for code, key in _LABEL_MAP_ENTRY_RE.findall(src)}


def test_backend_task_types_have_frontend_labels():
    """后端每一种 task_type 都必须在前端标签表里有对应中文。

    拟真走查实测：任务中心的标签表只映了 6 种，而后端实际会产生 20+ 种，
    没命中的直接 `return taskType` —— 于是用户看到 `external_api_probe` 这种内部 code。
    这类缺口跳语言、跳仓库，单侧测试发现不了：新增 task_type 时只改后端，前端依旧静默露原值。
    """
    backend = _backend_task_types()
    assert len(backend) >= 20, (
        f"只从 app/ 扫到 {len(backend)} 个 task_type，低于历史基准 20；"
        "说明扫描口径退化（后端改了新写法），本守护已接近空转，必须先修口径。"
    )

    labels = _frontend_task_type_map()
    assert labels, "前端 taskTypeLabel.ts 的映射表为空或格式变了"

    missing = sorted(backend - set(labels))
    assert not missing, (
        f"以下 task_type 在前端没有中文标签，会直接露内部 code：{missing}。"
        "修法：在 frontend/src/utils/taskTypeLabel.ts 加映射，并在 i18n zh/en 补文案。"
    )


def test_task_type_label_keys_exist_in_both_dictionaries():
    """标签表引用的 i18n 键，必须在 zh/en 两边字典里都有真文案。

    否则界面会把键名（taskTypeApiProbe）当文案显示 —— 同样是泄露，而且只 mock t()
    的单测永远看不出来。
    """
    labels = _frontend_task_type_map()
    wanted = set(labels.values()) | {"taskTypeOther", "taskTypeExternalSync"}

    for locale_file in ("zh-CN.ts", "en-US.ts"):
        src = (REPO_ROOT / "frontend" / "src" / "i18n" / locale_file).read_text(
            encoding="utf-8", errors="ignore"
        )
        absent = sorted(k for k in wanted if not re.search(rf'^\s*{k}:', src, re.M))
        assert not absent, f"{locale_file} 缺少这些任务类型文案键：{absent}"


# ══════════════════════════════════════════
# 20/21/22. 可读标签不再漂：两侧映射、治理枚举、告警标题
# ══════════════════════════════════════════

_BACKEND_LABEL_DICT_RE = re.compile(
    r'^\s{4}"([a-z][a-z0-9_]*)":\s*"([^"]+)"', re.M
)
_BLOCKING_UNION_RE = re.compile(
    r'export type DecisionBlockingStatus =([^;]*?);', re.S
)
_UNION_MEMBER_RE = re.compile(r'"([A-Z0-9_]+)"')
_GOV_LABEL_ENTRY_RE = re.compile(r'^\s{2}([A-Z0-9_]+):\s*"(govStatus[A-Za-z0-9]*)"', re.M)


def test_task_type_label_maps_do_not_drift():
    """后端中文标签表与前端英文标签表必须覆盖同一批 task_type。

    两处是同一含义的跨语言重复实现（没法共码）：只改一边就会出现
    “告警标题已中文化、任务中心还在露 code”这种半新半旧的状态。
    """
    backend_src = (REPO_ROOT / "app" / "services" / "task_type_labels.py").read_text(
        encoding="utf-8", errors="ignore"
    )
    head, _, tail = backend_src.partition("TASK_TYPE_LABELS_ZH")
    assert tail, "没找到 TASK_TYPE_LABELS_ZH 定义，后端标签表改名了"
    backend_keys = {k for k, _v in _BACKEND_LABEL_DICT_RE.findall(tail.partition("}")[0])}
    assert backend_keys, "后端 task_type 中文标签表为空"

    frontend_keys = set(_frontend_task_type_map())
    only_backend = sorted(backend_keys - frontend_keys)
    only_frontend = sorted(frontend_keys - backend_keys)
    assert not only_backend and not only_frontend, (
        f"两侧 task_type 标签表已漂移：仅后端有 {only_backend}，仅前端有 {only_frontend}。"
        "新增/删除 task_type 时两边要一起改。"
    )


def test_decision_blocking_status_has_labels():
    """client.ts 里声明的每一个治理状态枚举，前端都要有可读文案。"""
    src = (REPO_ROOT / "frontend" / "src" / "api" / "client.ts").read_text(
        encoding="utf-8", errors="ignore"
    )
    match = _BLOCKING_UNION_RE.search(src)
    assert match, "找不到 export type DecisionBlockingStatus，声明形式变了"
    declared = set(_UNION_MEMBER_RE.findall(match.group(1)))
    assert declared, "DecisionBlockingStatus 为空 union，实扫口径已失效"

    label_src = (REPO_ROOT / "frontend" / "src" / "utils" / "govStatusLabel.ts").read_text(
        encoding="utf-8", errors="ignore"
    )
    labeled = {k for k, _v in _GOV_LABEL_ENTRY_RE.findall(label_src)}
    missing = sorted(declared - labeled)
    assert not missing, (
        f"治理状态 {missing} 没有可读文案，界面会直接印枚举值（如 RECONCILIATION_BLOCKED）。"
    )


def test_alert_titles_do_not_interpolate_raw_task_type():
    """告警标题不得把 task_type 原值直拼进去（应走 task_type_label_zh）。

    历史写法：title=f"{task.task_type} 任务失败” —— 用户在告警中心看到
    `factor_pipeline 任务失败`。技术详情里仍可以带原值。
    """
    offenders: list[str] = []
    for path in (REPO_ROOT / "app").rglob("*.py"):
        src = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(src.splitlines(), 1):
            if "任务失败" not in line:
                continue
            # 只查真正给标题赋值/传参的行：标签模块的文档字符串里会引用那句
            # 历史写法作为成因说明，不能把它当成违规（自指式误报）。
            if "title" not in line:
                continue
            # 已走 label 函数包装的行是正确写法（f-string 里仍会出现 .task_type）
            if "task_type_label_zh(" in line:
                continue
            if ".task_type}" in line or "task_type)}" in line:
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{lineno} {line.strip()}")
    assert not offenders, (
        "以下告警标题直接把 task_type 原值拼进了用户可读文案：\n"
        + "\n".join(offenders)
        + "\n修法：用 app/services/task_type_labels.task_type_label_zh(...) 包装。"
    )


# 只盯两种真反模式：① 把治理状态变量直接插进「」给用户；② 文案里写死动作枚举原值。
# 故意**不包含** RECONCILIATION_BLOCKED: 形式 —— 它在多个组件里作为字典键（颜色/文案映射），
# 把那种写法也报红就成了一句真错没有、天天误报的噪声守护。
_RAW_ENUM_IN_UI_TEXT_RE = re.compile(
    r"「\{\s*currentState\s*\}」|(?:NEW_BUY|RISK_EXIT)\s*[:：]"
)


def test_portfolio_ui_copy_does_not_print_raw_enums():
    """组合交易组件的界面文案不得直接把治理/动作枚举原值写给用户。

    拟真走查实测：持仓成员横幅里是硬编码的
    `NEW_BUY: 禁止 | RISK_EXIT: 禁止` 和 `「{currentState}」`，用户因此读到
    RECONCILIATION_BLOCKED 这种词。这些位置必须走 utils/govStatusLabel（原值只进 tooltip）。
    """
    offenders: list[str] = []
    comp_dir = REPO_ROOT / "frontend" / "src" / "components"
    for f in sorted(comp_dir.rglob("*.tsx")):
        if "__tests__" in f.parts or ".test." in f.name:
            continue
        src = f.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(src.splitlines(), 1):
            if _RAW_ENUM_IN_UI_TEXT_RE.search(line):
                offenders.append(f"{f.relative_to(REPO_ROOT).as_posix()}:{lineno} {line.strip()[:110]}")
    assert not offenders, (
        "以下界面文案直接拼了枚举原值（应改走 govStatusLabel / 中文权限名）：\n"
        + "\n".join(offenders)
    )

