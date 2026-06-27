from playwright.sync_api import sync_playwright
import time

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.on("dialog", lambda dialog: dialog.accept())

    errors = []
    page_errors = []
    def on_console(msg):
        text = msg.text
        if msg.type == "error":
            errors.append(text)
        print(f"[CONSOLE {msg.type}] {text}")
    def on_page_error(err):
        page_errors.append(str(err))
        print(f"[PAGE ERROR] {err}")

    page.on("console", on_console)
    page.on("pageerror", on_page_error)

    print("Navigating to frontend...")
    page.goto("http://localhost:5173")
    page.wait_for_load_state("networkidle")
    time.sleep(2)
    page.screenshot(path="d:/ai_project/dataAanlystNew/test_sim_buy_initial.png", full_page=True)

    # Click 目前观察池 main tab
    portfolio_tab = page.locator("button.view-tab:has-text('目前观察池')").first
    if portfolio_tab.is_visible():
        print("Clicking 目前观察池 tab...")
        portfolio_tab.click()
        time.sleep(1)
    else:
        print("目前观察池 tab not found")

    # Click 模拟交易 sub-tab
    trading_tab = page.locator("button.sub-tab:has-text('模拟交易')").first
    if trading_tab.is_visible():
        print("Clicking 模拟交易 sub-tab...")
        trading_tab.click()
        time.sleep(2)
        page.screenshot(path="d:/ai_project/dataAanlystNew/test_sim_buy_trading.png", full_page=True)
    else:
        print("模拟交易 sub-tab not found")

    # Select a symbol via search
    search_input = page.locator("input[placeholder*='600519']").first
    if search_input.is_visible():
        print("Searching symbol 000001...")
        search_input.fill("000001")
        search_input.press("Enter")
        time.sleep(3)
        page.screenshot(path="d:/ai_project/dataAanlystNew/test_sim_buy_search.png", full_page=True)
    else:
        print("Search input not found")

    # Check if detail title updated
    detail_title = page.locator("#tradingDetailTitle").first
    if detail_title.is_visible():
        print(f"Detail title: {detail_title.inner_text().strip()}")

    # Fill quantity and price if not already filled
    qty_input = page.locator("#simQuantityInput").first
    price_input = page.locator("#simPriceInput").first
    if qty_input.is_visible() and price_input.is_visible():
        qty = qty_input.input_value()
        price = price_input.input_value()
        print(f"Quantity: {qty}, Price: {price}")
        if not qty:
            qty_input.fill("100")
        if not price:
            price_input.fill("10.00")
        time.sleep(1)

    # Close detail modal if open (it intercepts clicks)
    modal_close = page.locator(".ant-modal-close, .detail-modal-close, button[aria-label='Close']").first
    if modal_close.is_visible():
        print("Closing detail modal...")
        modal_close.click()
        time.sleep(1)

    # Click sim buy button
    buy_btn = page.locator("#simBuyButton").first
    if buy_btn.is_visible():
        print("Clicking sim buy button...")
        buy_btn.click()
        time.sleep(3)
        page.screenshot(path="d:/ai_project/dataAanlystNew/test_sim_buy_after.png", full_page=True)
    else:
        print("Sim buy button not found")

    browser.close()

    print("\n=== RESULTS ===")
    print(f"Console errors: {len(errors)}")
    for e in errors[:10]:
        print(f"  - {e}")
    print(f"Page errors: {len(page_errors)}")
    for e in page_errors[:10]:
        print(f"  - {e}")
    print("Done")
