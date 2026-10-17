import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";

/**
 * 消息中心壳层：「告警中心」+「消息管理」合并后的入口、Tab 与旧深链兼容。
 *
 * 守卫的三件事（都是本次合并最容易做漏的点）：
 * 1. 侧边导航只剩一个入口，旧的两个标签不再出现；
 * 2. 旧调用方仍可达：`settings:navigate` 传 "alerts" 或 {section:"notifications",
 *    subtab:"deliveries"}（铃铛「查看全部」）必须落到新壳层的对应 Tab；
 * 3. 三个 Tab 各自挂载正确的下游组件（告警=AlertCenter、送达配置=四个子页）。
 */

const ruleContext = vi.hoisted(() => ({ rule: null as any, save: vi.fn(async () => undefined) }));
vi.mock("../../../context/AppContext", () => ({
  useApp: () => ({
    signalRule: ruleContext.rule,
    signalRulePresets: [],
    signalRulePreview: null,
    activeSymbolId: null,
    loadSignalRulePreview: vi.fn(async () => undefined),
    saveSignalRule: ruleContext.save,
  }),
}));

vi.mock("../../AlertCenter", () => ({ __esModule: true, default: () => <div data-marker="alert-center" /> }));
vi.mock("../MessageInbox", () => ({ __esModule: true, default: () => <div data-marker="message-inbox" /> }));
vi.mock("../../notifications/ChannelConfig", () => ({
  ChannelConfig: () => <div data-marker="channel-config" />,
}));
vi.mock("../../notifications/PolicyEditor", () => ({ PolicyEditor: () => <div data-marker="policy-editor" /> }));
vi.mock("../../notifications/TemplateEditor", () => ({ TemplateEditor: () => <div data-marker="template-editor" /> }));
vi.mock("../../notifications/DeliveryLog", () => ({ DeliveryLog: () => <div data-marker="delivery-log" /> }));

import Settings from "../../Settings";

const navLabels = () =>
  Array.from(document.querySelectorAll("button.settings-nav-item")).map(
    (b) => b.textContent ?? "",
  );

function navigate(detail: unknown) {
  act(() => {
    window.dispatchEvent(new CustomEvent("settings:navigate", { detail }));
  });
}

function clickNav(label: string) {
  const target = Array.from(document.querySelectorAll("button.settings-nav-item")).find(
    (b) => (b.textContent ?? "").includes(label),
  );
  fireEvent.click(target as HTMLElement);
}

beforeEach(() => {
  ruleContext.rule = null;
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe("消息中心壳层", () => {
  it("侧边导航只剩「消息中心」，旧的两个入口不再出现", () => {
    render(<Settings />);
    const labels = navLabels();
    expect(labels.some((x) => x.includes("消息中心"))).toBe(true);
    expect(labels.some((x) => x.includes("告警中心"))).toBe(false);
    expect(labels.some((x) => x.includes("消息管理"))).toBe(false);
  });

  it("点「消息中心」默认落在告警 Tab，三个 Tab 都在", () => {
    render(<Settings />);
    clickNav("消息中心");
    const content = document.querySelector('[data-settings-content="settings-message-center"]');
    expect(content).toBeTruthy();
    expect(content?.querySelector('[data-marker="alert-center"]')).toBeTruthy();
    const tabs = Array.from(content?.querySelectorAll("button.sub-tab") ?? []).map(
      (b) => b.textContent ?? "",
    );
    expect(tabs.slice(0, 3)).toEqual(["告警", "收件箱", "送达配置"]);
  });

  it("切到「送达配置」再切到「发送记录」子页", () => {
    render(<Settings />);
    clickNav("消息中心");
    const content = document.querySelector('[data-settings-content="settings-message-center"]');
    fireEvent.click(
      Array.from(content?.querySelectorAll("button.sub-tab") ?? []).find(
        (b) => b.textContent === "送达配置",
      ) as HTMLElement,
    );
    expect(content?.querySelector('[data-marker="channel-config"]')).toBeTruthy();
    fireEvent.click(
      Array.from(content?.querySelectorAll("button.sub-tab") ?? []).find(
        (b) => b.textContent === "发送记录",
      ) as HTMLElement,
    );
    expect(content?.querySelector('[data-marker="delivery-log"]')).toBeTruthy();
  });

  it("旧调用方 detail=\"alerts\" 仍能直达告警 Tab", () => {
    render(<Settings />);
    navigate("alerts");
    const content = document.querySelector('[data-settings-content="settings-message-center"]');
    expect(content).toBeTruthy();
    expect(content?.querySelector('[data-marker="alert-center"]')).toBeTruthy();
    expect(window.localStorage.getItem("settings_message_center_tab")).toBe("alerts");
  });

  it("铃铛「查看全部」的 notifications+deliveries 深链落到发送记录", async () => {
    render(<Settings />);
    navigate({ section: "notifications", subtab: "deliveries" });
    const content = document.querySelector('[data-settings-content="settings-message-center"]');
    expect(content).toBeTruthy();
    await waitFor(() =>
      expect(content?.querySelector('[data-marker="delivery-log"]')).toBeTruthy(),
    );
    expect(window.localStorage.getItem("settings_notification_subtab")).toBe("deliveries");
  });

  it("收件箱 Tab 挂载 MessageInbox，且选择会被持久化", () => {
    render(<Settings />);
    clickNav("消息中心");
    const content = document.querySelector('[data-settings-content="settings-message-center"]');
    fireEvent.click(
      Array.from(content?.querySelectorAll("button.sub-tab") ?? []).find(
        (b) => b.textContent === "收件箱",
      ) as HTMLElement,
    );
    expect(content?.querySelector('[data-marker="message-inbox"]')).toBeTruthy();
    expect(window.localStorage.getItem("settings_message_center_tab")).toBe("inbox");
  });

  it("刷新后仍停在上次使用的 Tab（localStorage 恢复）", () => {
    window.localStorage.setItem("settings_active_section", "notifications");
    window.localStorage.setItem("settings_message_center_tab", "delivery");
    render(<Settings />);
    const content = document.querySelector('[data-settings-content="settings-message-center"]');
    expect(content).toBeTruthy();
    expect(content?.querySelector('[data-marker="channel-config"]')).toBeTruthy();
  });
});
