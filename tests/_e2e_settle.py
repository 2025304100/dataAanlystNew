"""e2e 页面"稳定等待"的统一姿势。

为什么要有它（实测故障，不是洁癖）：
本应用存在持续轮询（任务心跳、行情、探测进度）。`wait_for_load_state("networkidle")`
在没有请求空隙时**永远不会满足**，Playwright 默认 30s 超时后抛
`TimeoutError`，表现为整个 e2e 文件成批 FAILED / ERROR at setup ——
本地在后端刚重启时必现（实测 3 条用例同时栽在 `_open_view` 的 networkidle 上），
CI 上前端恰好空闲所以一直没暴露。

规则：
- 固定 settle 保留（用例后续多用 `.count()` / `is_visible()` 这类**不等待**的检查，
  完全去掉 settle 会让它们抢跑）；
- networkidle 降级为"尽力而为"，等不到**不算故障**；
- 需要"真的渲染出来了"这种硬前置时，请等**具体元素**
  （参考 conftest 里 `wait_for_selector("button.view-tab")` 的写法），
  不要用 networkidle 当渲染信号。
"""
from __future__ import annotations

from playwright.sync_api import Error as PlaywrightError


def settle(page, ms: int = 1500, idle_ms: int = 5000) -> bool:
    """先固定等 ms，再尽力等 networkidle（最多 idle_ms）。

    返回是否真的等到了 networkidle —— 调用方不需要关心，但留着便于诊断。
    """
    page.wait_for_timeout(ms)
    try:
        page.wait_for_load_state("networkidle", timeout=idle_ms)
        return True
    except PlaywrightError:
        # 持续轮询是产品行为，不是页面故障：等不到就继续，不抛。
        return False
