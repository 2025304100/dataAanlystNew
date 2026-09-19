/**
 * 应用内跳转的可替换 helper（用于 P2 流水线 UI 从 Settings 跳到因子中心）。
 *
 * 当前实现：优先派发现有 settings:navigate 自定义事件（工作台设置 Tab 会
 * 监听它切到 settings/factor-center）；如果调用方需要完整 URL 导航，也会
 * 尝试同步设置 window.location.hash（单页 hash 路由的回退）。
 *
 * 之所以不直接硬编码 dispatchEvent，是因为 vitest 需要精确断言
 * `navigate('/factors?next=pipeline')` 这一次调用——这是 P2 验收的
 * TR-8.3 rule 要求。把跳转封装成一个独立函数，vitest 就能 mock。
 */
export function navigate(url: string): void {
  if (typeof url !== "string") return;

  // 语义 URL：/factors?next=pipeline  →  对应 settings 下的因子中心 Tab
  if (url.startsWith("/factors")) {
    try {
      if (typeof window !== "undefined" && typeof CustomEvent === "function") {
        window.dispatchEvent(
          new CustomEvent("settings:navigate", { detail: "factor-center" }),
        );
        window.dispatchEvent(
          new CustomEvent("factor-center:navigate", {
            detail: { target: "models", next: "pipeline" },
          }),
        );
      }
    } catch {
      /* 环境里 CustomEvent 不支持，忽略 */
    }
    try {
      if (typeof window !== "undefined" && window.location) {
        const hash = `#/settings/factor-center${url.includes("?") ? url.slice(url.indexOf("?")) : ""}`;
        if (window.location.hash !== hash) {
          window.location.hash = hash;
        }
      }
    } catch {
      /* hash 修改失败不抛出 */
    }
    return;
  }

  // Settings is a tab-based view, not a real browser route. Keep pipeline
  // navigation inside the SPA instead of assigning window.location.href.
  if (url === "/settings/pipeline") {
    try {
      if (typeof window !== "undefined" && typeof CustomEvent === "function") {
        window.dispatchEvent(new CustomEvent("settings:navigate", { detail: "factor-model" }));
      }
    } catch {
      /* ignore unsupported event environments */
    }
    return;
  }

  // 默认：直接跳转（保留对未来 path 路由的兼容）
  try {
    if (typeof window !== "undefined" && window.location) {
      window.location.href = url;
    }
  } catch {
    /* 忽略 */
  }
}
