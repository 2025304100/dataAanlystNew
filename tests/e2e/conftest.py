"""E2E 测试 fixtures（P2-1）。

提供：
- live_backend：检查后端是否在线（http://localhost:8000）
- live_frontend：检查前端是否在线（http://localhost:5173）
- browser：Playwright 浏览器实例（module scope）
- page：Playwright 页面（function scope）

E2E 测试需要前后端同时运行：
    cd d:/ai_project/dataAanlystNew && python -m uvicorn app.main:app --port 8000
    cd d:/ai_project/dataAanlystNew/frontend && npm run dev

运行 E2E 测试：
    pytest tests/e2e/ -v -m e2e
"""
from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.e2e

# Keep localhost as the default used by the existing smoke tests, while
# allowing the G3 acceptance runner to point at an isolated API/Vite pair.
BACKEND_URL = os.getenv("E2E_BACKEND_URL", "http://localhost:8000").rstrip("/")
FRONTEND_URL = os.getenv("E2E_FRONTEND_URL", "http://localhost:5173").rstrip("/")


@pytest.fixture(scope="session")
def live_backend() -> bool:
    """检查后端是否在线，不在线则 skip 所有 E2E 测试。

    CI（REQUIRE_LIVE_BACKEND=1）下改为失败：否则“服务没起来→全体 skip→job 绿”
    会让 e2e/blackbox 变成零覆盖的假通过。
    """
    from tests._live_backend_guard import skip_or_fail_no_live_backend

    try:
        r = httpx.get(f"{BACKEND_URL}/health", timeout=3.0, trust_env=False)
        if r.status_code == 200:
            return True
        skip_or_fail_no_live_backend(
            f"GET {BACKEND_URL}/health 返回 {r.status_code}", what="后端服务"
        )
    except Exception as exc:  # 连不上、超时，或上面抛出 Skipped/Failed 本身
        if isinstance(exc, (pytest.skip.Exception, pytest.fail.Exception)):
            raise
        skip_or_fail_no_live_backend(exc, what=f"后端服务（{BACKEND_URL}）")


@pytest.fixture(scope="session")
def live_frontend() -> bool:
    """检查前端是否在线，不在线则 skip 所有 E2E 测试。

    与 live_backend 同一口径：REQUIRE_LIVE_BACKEND=1（CI）下服务不在就失败，
    不拿“全体 skip”当“e2e 通过”。
    """
    from tests._live_backend_guard import skip_or_fail_no_live_backend

    try:
        r = httpx.get(FRONTEND_URL, timeout=3.0, trust_env=False)
        if r.status_code == 200:
            return True
        skip_or_fail_no_live_backend(
            f"GET {FRONTEND_URL} 返回 {r.status_code}", what="前端服务"
        )
    except Exception as exc:
        if isinstance(exc, (pytest.skip.Exception, pytest.fail.Exception)):
            raise
        skip_or_fail_no_live_backend(exc, what=f"前端服务（{FRONTEND_URL}）")


@pytest.fixture(scope="module")
def browser(live_backend, live_frontend):
    """Playwright 浏览器实例（module scope，复用浏览器）。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - depends on the验收机环境
        pytest.skip(
            "Python Playwright 不可用；请用 PYTHONPATH=.pip_packages 暴露锁定依赖 "
            f"（{type(exc).__name__}: {exc}）"
        )

    with sync_playwright() as p:
        # 默认用 Playwright 配套 build；如果本机没那个 build（常见：playwright 升版后
        # ms-playwright 下仍是旧版本号），可以用已装浏览器跑：
        #   E2E_BROWSER_CHANNEL=chrome|msedge|chrome-beta …
        #   E2E_BROWSER_EXECUTABLE_PATH=D:/path/to/chrome.exe
        launch_kwargs: dict = {"headless": True}
        channel = os.getenv("E2E_BROWSER_CHANNEL", "").strip()
        exe = os.getenv("E2E_BROWSER_EXECUTABLE_PATH", "").strip()
        if channel:
            launch_kwargs["channel"] = channel
        if exe:
            launch_kwargs["executable_path"] = exe

        try:
            br = p.chromium.launch(**launch_kwargs)
        except Exception as exc:  # pragma: no cover - Chromium sandbox/权限相关
            # A restricted CI/container commonly reports spawn EPERM.  This is
            # an environment limitation, not a failed UI assertion; make it a
            # visible skip so the acceptance report can distinguish the two.
            # 但设了 REQUIRE_LIVE_BACKEND=1（CI）时不能 skip：否则“浏览器起不来 →
            # e2e 全体 skip”又是一种零覆盖假绿（§十五.6 同族）。
            from tests._live_backend_guard import skip_or_fail_no_live_backend

            skip_or_fail_no_live_backend(
                str(exc).splitlines()[0] if str(exc) else type(exc).__name__,
                what="Playwright 浏览器",
            )
        yield br
        br.close()


@pytest.fixture
def page(browser):
    """Playwright 页面（function scope，每个测试独立页面）。

    自动处理 dialog（alert/confirm）和收集 console/page 错误。
    """
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.on("dialog", lambda dialog: dialog.accept())

    console_errors: list[tuple[str, str]] = []
    page_errors: list[str] = []

    def on_console(msg):
        if msg.type == "error":
            console_errors.append(("console", msg.text))

    def on_page_error(err):
        page_errors.append(str(err))

    pg.on("console", on_console)
    pg.on("pageerror", on_page_error)

    pg.goto(FRONTEND_URL)
    pg.wait_for_load_state("networkidle")

    yield pg

    # 测试结束后断言无 JS 错误（可选，通过 pytest hook 控制）
    pg.close()

    # 将错误附加到测试上下文（通过 request.node 也可）
    if console_errors:
        # 不直接 fail，仅记录；具体测试可自行断言
        pass
    if page_errors:
        pass
