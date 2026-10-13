import { describe, expect, it } from "vitest";
import type { ActionItem, ActionPrimaryAction } from "../../types";
import {
  isRawEnum,
  resolveAction,
  resolveItemAction,
  resolvePortfolioNavigation,
  safeImpact,
  safeTitle,
} from "../actionItemRegistry";

function item(over: Partial<ActionItem> = {}): ActionItem {
  return {
    id: "readiness:X",
    source: "readiness",
    code: "X",
    severity: "blocker",
    title: "标题",
    impact: "影响",
    detail: null,
    family: null,
    primary_action: null,
    sources: ["readiness"],
    ...over,
  };
}

function act(a: ActionPrimaryAction | null): ActionPrimaryAction | null {
  return a;
}

describe("resolveAction 动作白名单", () => {
  it("识别原地开关与对账引导", () => {
    expect(resolveAction(act({ action_type: "toggle" }))).toEqual({ kind: "toggle" });
    expect(resolveAction(act({ action_type: "guided", target: "governance:reconciliation" }))).toEqual({
      kind: "guided",
      flow: "reconciliation",
    });
  });

  it("仅接受已存在的组合子页", () => {
    expect(resolveAction(act({ action_type: "subtab", target: "strategy" }))).toEqual({
      kind: "subtab",
      tab: "strategy",
    });
    for (const bad of ["nope", "", undefined]) {
      expect(resolveAction(act({ action_type: "subtab", target: bad as string }))).toEqual({ kind: "none" });
    }
  });

  it("治理子页采用有命名空间的稳定目标", () => {
    for (const tab of ["overview", "state", "reconciliation", "audit"]) {
      expect(resolveAction({ action_type: "subtab", target: `governance:${tab}` })).toEqual({ kind: "subtab", tab: `governance:${tab}` });
      expect(resolvePortfolioNavigation(`governance:${tab}`)).toEqual({ tab: "governance", governanceTab: tab });
    }
    expect(resolvePortfolioNavigation("overview")).toEqual({ tab: "overview" });
    expect(resolvePortfolioNavigation("reconciliation")).toBeNull();
    expect(resolveAction({ action_type: "subtab", target: "governance:unknown" })).toEqual({ kind: "none" });
  });

  it("仅接受已验证的跨页目标", () => {
    expect(resolveAction(act({ action_type: "redirect", target: "settings:tasks" }))).toEqual({
      kind: "redirect",
      target: "settings:tasks",
    });
    expect(resolveAction(act({ action_type: "redirect", target: "settings:nope" }))).toEqual({
      kind: "none",
    });
  });

  it("仅接受已知弹窗", () => {
    expect(resolveAction(act({ action_type: "open-modal", target: "add-candidate" }))).toEqual({
      kind: "open-modal",
      modal: "add-candidate",
    });
    expect(resolveAction(act({ action_type: "open-modal", target: "evil" }))).toEqual({ kind: "none" });
    expect(resolveAction({ action_type: "open-modal", target: "model-binding-review" })).toEqual({ kind: "open-modal", modal: "model-binding-review" });
  });

  it("未知引导目标不会误执行对账", () => {
    expect(resolveAction({ action_type: "guided", target: "unknown" })).toEqual({ kind: "none" });
  });

  it("none 与空动作安全降级", () => {
    expect(resolveAction(act({ action_type: "none" }))).toEqual({ kind: "none" });
    expect(resolveAction(null)).toEqual({ kind: "none" });
  });
});

describe("文案不裸显枚举", () => {
  it("识别裸枚举", () => {
    expect(isRawEnum("MARKET_DATA_STALE")).toBe(true);
    expect(isRawEnum("ABC")).toBe(false);
    expect(isRawEnum("行情过期")).toBe(false);
  });

  it("safeTitle 缺失或裸枚举时兜底", () => {
    expect(safeTitle(item({ title: "行情数据过期" }))).toBe("行情数据过期");
    expect(safeTitle(item({ title: "MARKET_DATA_STALE" }))).toBe("检测到一项待处理条件");
    expect(safeTitle(item({ title: "" }))).toBe("检测到一项待处理条件");
  });

  it("safeImpact 裸枚举时回退到标题", () => {
    expect(safeImpact(item({ impact: "今天不会下单" }))).toBe("今天不会下单");
    expect(safeImpact(item({ impact: "NO_TRADEABLE_RANGE", title: "没有可交易对象" }))).toBe(
      "没有可交易对象",
    );
  });
});

it("resolveItemAction 端到端读取 primary_action", () => {
  expect(
    resolveItemAction(item({ primary_action: { action_type: "subtab", target: "members" } })),
  ).toEqual({ kind: "subtab", tab: "members" });
});
