import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, waitFor } from "@testing-library/react";

/**
 * T28 Step1 候选池 UI 测试（向导 §3 / 需求 §3.5~§3.7 / 设计 §9.2）。
 *
 * 覆盖 artifacts 与 pitfalls：
 *   1. 入口 Tab（条件筛选 / 导入）**互斥**，切换前必须确认清空未保存配置
 *   2. 条件变化后 **300ms 防抖**才调用预览（防抖预览不得作为挖掘快照）
 *   3. 锁定态：筛选 / 导入 / 批量删除**全部置灰**（§3.7.3）
 *   4. 「生成挖掘物料」按钮状态机（初始/分析中/已完成）
 *   5. 看板弹窗**只读**，[重新选择] 二次确认后解锁
 *   6. 有效标的 <50 → 阻断，不能进入下一步
 *   7. 批量删除**二次确认**
 *
 * 只 mock 本模块的 `./poolApi` 与 i18n 之外的外部依赖，组件真实渲染。
 */

const {
  previewPool,
  createPoolFromFilter,
  createSnapshot,
  deleteLatestSnapshot,
  removeMembers,
  fetchMembers,
} = vi.hoisted(() => ({
  previewPool: vi.fn(),
  createPoolFromFilter: vi.fn(),
  createSnapshot: vi.fn(),
  deleteLatestSnapshot: vi.fn(),
  removeMembers: vi.fn(),
  fetchMembers: vi.fn(),
}));

vi.mock("./poolApi", () => ({
  // 常量也必须导出：组件 import 了 MIN_POOL_SIZE，mock 缺它会在渲染时
  // 抛 "No MIN_POOL_SIZE export is defined"，表现为元素为 null（易误判为
  // 组件 bug，实为 mock 不完整）。
  MIN_POOL_SIZE: 50,
  previewPool,
  createPoolFromFilter,
  createSnapshot,
  deleteLatestSnapshot,
  removeMembers,
  fetchFilterPresets: vi.fn(async () => []),
  fetchFilterFields: vi.fn(async () => []),
  fetchMembers,
  fetchLatestSnapshot: vi.fn(async () => null),
  downloadImportTemplate: vi.fn(),
  previewImport: vi.fn(async () => ({ rows: [] })),
  exportImportErrors: vi.fn(),
  importMembers: vi.fn(async () => ({})),
}));

import MiningPoolStep from "./MiningPoolStep";

const PREVIEW_OK = {
  matched: 285,
  total: 5000,
  excluded: 4715,
  by_rule: [{ rule: "market", excluded: 2000 }],
  warnings: [] as string[],
  blocked: null as null | { error_code: string; detail_zh: string },
};

beforeEach(() => {
  vi.clearAllMocks();
  previewPool.mockResolvedValue(PREVIEW_OK);
  createPoolFromFilter.mockResolvedValue({ id: "pool-1", name: "p", source_type: "filter" });
  createSnapshot.mockResolvedValue({ id: "snap-1", is_locked: true, analysis_json: {} });
  deleteLatestSnapshot.mockResolvedValue({ ok: true });
  removeMembers.mockResolvedValue({ removed: 1 });
  window.localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  window.localStorage.clear();
});

describe("MiningPoolStep 入口与互斥", () => {
  it("默认展示条件筛选入口，两个入口互斥不同时渲染", () => {
    render(<MiningPoolStep />);
    expect(document.querySelector("[data-pool-step]")).toBeTruthy();
    const filterBtn = document.querySelector('[data-pool-tab="filter"]');
    const importBtn = document.querySelector('[data-pool-tab="import"]');
    expect(filterBtn).toBeTruthy();
    expect(importBtn).toBeTruthy();
    // 互斥：只渲染当前入口的面板
    expect(document.querySelector("[data-pool-filter-panel]")).toBeTruthy();
    expect(document.querySelector("[data-pool-import-panel]")).toBeNull();
  });

  it("切换入口需先确认清空（确认后才切换）", () => {
    render(<MiningPoolStep />);
    fireEvent.click(document.querySelector('[data-pool-tab="import"]') as HTMLElement);
    // 弹出确认 → 取消则仍在筛选入口
    const cancel = document.querySelector("[data-pool-switch-cancel]");
    expect(cancel).toBeTruthy();
    fireEvent.click(cancel as HTMLElement);
    expect(document.querySelector("[data-pool-filter-panel]")).toBeTruthy();
    expect(document.querySelector("[data-pool-import-panel]")).toBeNull();
  });

  it("确认清空后切到导入入口", () => {
    render(<MiningPoolStep />);
    fireEvent.click(document.querySelector('[data-pool-tab="import"]') as HTMLElement);
    fireEvent.click(document.querySelector("[data-pool-switch-confirm]") as HTMLElement);
    expect(document.querySelector("[data-pool-import-panel]")).toBeTruthy();
    expect(document.querySelector("[data-pool-filter-panel]")).toBeNull();
  });
});

describe("MiningPoolStep 防抖预览", () => {
  it("条件变化 300ms 内不重复请求，防抖后才调用预览", () => {
    vi.useFakeTimers();
    render(<MiningPoolStep />);
    const input = document.querySelector("[data-pool-filter-input]") as HTMLInputElement;
    act(() => {
      fireEvent.change(input, { target: { value: "100" } });
    });
    act(() => {
      vi.advanceTimersByTime(100);
    });
    expect(previewPool).not.toHaveBeenCalled();
    act(() => {
      vi.advanceTimersByTime(250);
    });
    expect(previewPool).toHaveBeenCalledTimes(1);
  });

  it("防抖预览结果只用于展示，不写快照", async () => {
    vi.useFakeTimers();
    render(<MiningPoolStep />);
    act(() => {
      fireEvent.change(
        document.querySelector("[data-pool-filter-input]") as HTMLInputElement,
        { target: { value: "100" } },
      );
    });
    act(() => {
      vi.advanceTimersByTime(350);
    });
    // 防抖回调里 `previewPool(...).then(setPreview)` 是 microtask，
    // fake timers 下必须再 flush 一次，否则断言早于 setState 落地。
    await act(async () => {
      await Promise.resolve();
    });
    expect(createPoolFromFilter).not.toHaveBeenCalled();
    expect(document.querySelector("[data-pool-preview-stats]")).toBeTruthy();
  });
});

describe("MiningPoolStep 生成物料与锁定", () => {
  it("点击生成挖掘物料 → 走 snapshot 接口并进入锁定态", async () => {
    render(<MiningPoolStep />);
    const btn = document.querySelector("[data-pool-generate]") as HTMLElement;
    expect(btn.textContent).toContain("生成挖掘物料");
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(createSnapshot).toHaveBeenCalled();
    expect(document.querySelector("[data-pool-lock-banner]")).toBeTruthy();
  });

  it("锁定态下筛选/导入/批量删除全部置灰", async () => {
    render(<MiningPoolStep />);
    await act(async () => {
      fireEvent.click(document.querySelector("[data-pool-generate]") as HTMLElement);
    });
    const filterInput = document.querySelector(
      "[data-pool-filter-input]",
    ) as HTMLInputElement;
    const importTab = document.querySelector('[data-pool-tab="import"]') as HTMLElement;
    const bulkDelete = document.querySelector("[data-pool-bulk-delete]") as HTMLElement;
    expect(filterInput.disabled).toBe(true);
    expect(importTab.hasAttribute("disabled") || importTab.getAttribute("aria-disabled") === "true").toBe(true);
    expect(bulkDelete.hasAttribute("disabled")).toBe(true);
  });

  it("按钮状态机：分析完成后变为『查看候选池看板』", async () => {
    render(<MiningPoolStep />);
    await act(async () => {
      fireEvent.click(document.querySelector("[data-pool-generate]") as HTMLElement);
    });
    const btn = document.querySelector("[data-pool-generate]") as HTMLElement;
    expect(btn.textContent).toContain("查看候选池看板");
  });
});

describe("MiningPoolStep 看板弹窗与重新选择", () => {
  it("看板弹窗只读且含重新选择/下一步", async () => {
    render(<MiningPoolStep />);
    await act(async () => {
      fireEvent.click(document.querySelector("[data-pool-generate]") as HTMLElement);
    });
    await act(async () => {
      fireEvent.click(document.querySelector("[data-pool-generate]") as HTMLElement);
    });
    const modal = document.querySelector("[data-pool-analysis-modal]");
    expect(modal).toBeTruthy();
    expect(modal?.querySelectorAll("input, select, textarea").length).toBe(0); // 只读
    expect(document.querySelector("[data-pool-reselect]")).toBeTruthy();
    expect(document.querySelector("[data-pool-next]")).toBeTruthy();
  });

  it("重新选择需二次确认，确认后解锁并清掉锁定条", async () => {
    render(<MiningPoolStep />);
    await act(async () => {
      fireEvent.click(document.querySelector("[data-pool-generate]") as HTMLElement);
    });
    await act(async () => {
      fireEvent.click(document.querySelector("[data-pool-generate]") as HTMLElement);
    });
    await act(async () => {
      fireEvent.click(document.querySelector("[data-pool-reselect]") as HTMLElement);
    });
    // 二次确认弹窗
    expect(document.querySelector("[data-pool-reselect-confirm]")).toBeTruthy();
    await act(async () => {
      fireEvent.click(
        document.querySelector("[data-pool-reselect-confirm]") as HTMLElement,
      );
    });
    expect(deleteLatestSnapshot).toHaveBeenCalled();
    expect(document.querySelector("[data-pool-lock-banner]")).toBeNull();
  });
});

describe("MiningPoolStep 硬边界", () => {
  it("命中 <50 阻断：生成物料不可用且提示不足 50", async () => {
    previewPool.mockResolvedValue({
      ...PREVIEW_OK,
      matched: 12,
      blocked: { error_code: "MINING_POOL_TOO_SMALL", detail_zh: "有效标的不足 50" },
    });
    // 本用例走**真实定时器**：waitFor 依赖真实时间轮询，fake timers 会让它
    // 直接超时（实测 Test timed out in 5000ms）。防抖 300ms 用 waitFor 等即可。
    render(<MiningPoolStep />);
    await act(async () => {
      fireEvent.change(
        document.querySelector("[data-pool-filter-input]") as HTMLInputElement,
        { target: { value: "1" } },
      );
    });
    await waitFor(
      () => {
        expect(document.querySelector("[data-pool-blocked]")).toBeTruthy();
      },
      { timeout: 3000 },
    );
    expect(
      (document.querySelector("[data-pool-generate]") as HTMLElement).hasAttribute("disabled"),
    ).toBe(true);
  });

  it("批量删除需二次确认，确认后调用成员删除", async () => {
    fetchMembers.mockResolvedValue({
      items: [{ symbol: "600000", name: "A" }, { symbol: "000001", name: "B" }],
      total: 2, page: 1, page_size: 20,
    });
    render(<MiningPoolStep poolId="pool-1" />);
    await act(async () => {
      await Promise.resolve();
    });
    // 未选中任何成员时按钮禁用（防误删）
    expect(
      (document.querySelector("[data-pool-bulk-delete]") as HTMLButtonElement).disabled,
    ).toBe(true);
    const boxes = document.querySelectorAll(
      "[data-pool-member-table] tbody input[type=checkbox]",
    );
    expect(boxes.length).toBe(2);
    await act(async () => {
      fireEvent.click(boxes[0] as HTMLElement);
    });
    const del = document.querySelector("[data-pool-bulk-delete]") as HTMLElement;
    await act(async () => {
      fireEvent.click(del);
    });
    const confirm = document.querySelector("[data-pool-delete-confirm]");
    expect(confirm).toBeTruthy();
    await act(async () => {
      fireEvent.click(confirm as HTMLElement);
    });
    expect(removeMembers).toHaveBeenCalled();
  });
});
