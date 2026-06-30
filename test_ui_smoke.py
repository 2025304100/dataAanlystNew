from playwright.sync_api import sync_playwright
import time

TABS = [
    ("今日决策", "decision"),
    ("投资中心", "investment"),
    ("目前观察池", "portfolio"),
    ("机会挖掘", "discovery"),
    ("宏观数据", "macro"),
    ("行情消息", "news"),
    ("设置", "settings"),
]

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.on("dialog", lambda dialog: dialog.accept())

    console_errors = []
    page_errors = []

    def on_console(msg):
        text = msg.text
        if msg.type == "error":
            console_errors.append((TABS[current_tab_index][1] if current_tab_index >= 0 else "init", text))
        # Print all non-trivial messages for debugging
        if msg.type in ("error", "warning"):
            print(f"[CONSOLE {msg.type}] [{TABS[current_tab_index][1] if current_tab_index >= 0 else 'init'}] {text[:200]}")

    def on_page_error(err):
        page_errors.append((TABS[current_tab_index][1] if current_tab_index >= 0 else "init", str(err)))
        print(f"[PAGE ERROR] [{TABS[current_tab_index][1] if current_tab_index >= 0 else 'init'}] {err}")

    page.on("console", on_console)
    page.on("pageerror", on_page_error)

    current_tab_index = -1

    print("Navigating to frontend...")
    page.goto("http://localhost:5173")
    page.wait_for_load_state("networkidle")
    time.sleep(2)
    page.screenshot(path="d:/ai_project/dataAanlystNew/test_ui_init.png", full_page=True)

    def close_modal():
        close = page.locator(".ant-modal-close, button[aria-label='Close']").first
        if close.is_visible():
            print("  Closing open modal...")
            close.click()
            time.sleep(1)

    for idx, (label, key) in enumerate(TABS):
        current_tab_index = idx
        print(f"\n--- Testing tab: {label} ({key}) ---")
        close_modal()
        tab_btn = page.locator(f"button.view-tab:has-text('{label}')").first
        if not tab_btn.is_visible():
            print(f"  Tab {label} not visible")
            continue
        tab_btn.click()
        time.sleep(2)
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        page.screenshot(path=f"d:/ai_project/dataAanlystNew/test_ui_{key}.png", full_page=True)
        print(f"  Screenshot saved: test_ui_{key}.png")

        # Portfolio sub-tabs
        if key == "portfolio":
            for sub_label, sub_key in [("工作台", "workbench"), ("模拟交易", "trading")]:
                print(f"  Testing sub-tab: {sub_label}")
                close_modal()
                sub_btn = page.locator(f"button.sub-tab:has-text('{sub_label}')").first
                if sub_btn.is_visible():
                    sub_btn.click()
                    time.sleep(2)
                    page.screenshot(path=f"d:/ai_project/dataAanlystNew/test_ui_portfolio_{sub_key}.png", full_page=True)
                else:
                    print(f"    Sub-tab {sub_label} not found")

    browser.close()

    print("\n" + "=" * 60)
    print("UI Smoke Test Results")
    print("=" * 60)
    print(f"Console errors: {len(console_errors)}")
    for tab, err in console_errors:
        print(f"  [{tab}] {err}")
    print(f"Page errors: {len(page_errors)}")
    for tab, err in page_errors:
        print(f"  [{tab}] {err}")
    if not console_errors and not page_errors:
        print("No JS errors detected.")
