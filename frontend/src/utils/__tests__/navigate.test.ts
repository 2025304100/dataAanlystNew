import { beforeEach, describe, expect, it, vi } from "vitest";
import { openSettingsSection, requestPortfolioSubtab } from "../navigate";

beforeEach(() => {
  window.localStorage.clear();
});

describe("openSettingsSection", () => {
  it("写入目标 section 并派发 settings:navigate", () => {
    const listener = vi.fn();
    window.addEventListener("settings:navigate", listener);

    openSettingsSection("tasks");

    expect(window.localStorage.getItem("settings_active_section")).toBe("tasks");
    expect(listener).toHaveBeenCalledTimes(1);
    expect((listener.mock.calls[0][0] as CustomEvent<string>).detail).toBe("tasks");
    window.removeEventListener("settings:navigate", listener);
  });

  it("数据中心 section 同样可达", () => {
    openSettingsSection("data-center");
    expect(window.localStorage.getItem("settings_active_section")).toBe("data-center");
  });
});

describe("requestPortfolioSubtab", () => {
  it("派发组合子页切换事件", () => {
    const listener = vi.fn();
    window.addEventListener("portfolio:subtab-navigate", listener);

    requestPortfolioSubtab("strategy");

    expect(listener).toHaveBeenCalledTimes(1);
    expect((listener.mock.calls[0][0] as CustomEvent<{ tab: string }>).detail.tab).toBe(
      "strategy",
    );
    window.removeEventListener("portfolio:subtab-navigate", listener);
  });

  it("支持治理内部深链，并忽略未登记的页", () => {
    const listener = vi.fn();
    window.addEventListener("portfolio:subtab-navigate", listener);
    requestPortfolioSubtab("governance:state");
    requestPortfolioSubtab("governance:unknown");
    requestPortfolioSubtab("state");
    expect(listener).toHaveBeenCalledTimes(1);
    expect((listener.mock.calls[0][0] as CustomEvent<{ tab: string }>).detail.tab).toBe("governance:state");
    window.removeEventListener("portfolio:subtab-navigate", listener);
  });
});
