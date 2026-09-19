import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render } from "@testing-library/react";

/**
 * T30 Step3 字段与校验 UI 测试（向导 §5 / §5.1，设计 §9.2）。
 *
 * 覆盖 artifacts「字段勾选 + 校验进度 + 阻断详情」与两条 not_do：
 *   1. 字段按分组（行情/估值/财报/资金流/事件）勾选，选中集上抛；
 *   2. 发起校验 → **5s 轮询**进度（not_do：不引入 WebSocket）；
 *   3. 进度按分片展示「已完成 / 总数」；
 *   4. 阻断详情：逐字段展示问题类型/当前值/要求值；
 *   5. **阻断只给两个出口**：重新选择字段 / 去修复数据（pitfalls）；
 *   6. **不提供逐字段自动修复**（not_do）—— 阻断项内不得出现修复按钮。
 */

const {
  createValidation,
  getValidation,
  getValidFields,
} = vi.hoisted(() => ({
  createValidation: vi.fn(),
  getValidation: vi.fn(),
  getValidFields: vi.fn(),
}));

vi.mock("./fieldApi", () => ({
  createValidation,
  getValidation,
  getValidFields,
  resumeValidation: vi.fn(),
  pauseValidation: vi.fn(),
}));

import MiningFieldStep from "./MiningFieldStep";
import type { MiningField } from "./fieldTypes";

const FIELDS: MiningField[] = [
  { code: "close", name_zh: "收盘价", group: "quote", source_table: "raw_daily_bars",
    data_mode: "continuous", coverage: 0.999, latest_date: "2026-09-05",
    available_from: "2021-01-04", available_to: "2026-09-05" },
  { code: "pe_ttm", name_zh: "市盈率TTM", group: "valuation", source_table: "daily_valuation",
    data_mode: "PIT", coverage: 0.921, latest_date: "2026-09-04",
    available_from: "2021-01-04", available_to: "2026-09-04" },
  { code: "roe_ttm", name_zh: "ROE_TTM", group: "financial", source_table: "financial_reports",
    data_mode: "PIT", coverage: 0.612, latest_date: "2026-06-30",
    available_from: "2021-03-31", available_to: "2026-06-30", blocked_reason: "覆盖率不足" },
];

const RUNNING = {
  task_id: "val-1", status: "running", progress: { done: 12, total: 60 },
  blocked: [] as Array<Record<string, unknown>>, warnings: [] as string[],
  passed: false, message: "校验中",
};

beforeEach(() => {
  vi.clearAllMocks();
  createValidation.mockResolvedValue({ task_id: "val-1", status: "running" });
  getValidation.mockResolvedValue(RUNNING);
  getValidFields.mockResolvedValue({ fields: ["close"] });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("MiningFieldStep 字段勾选", () => {
  it("按分组渲染字段并可勾选，选中集上抛", () => {
    const onChange = vi.fn();
    render(<MiningFieldStep fields={FIELDS} onChange={onChange} />);
    expect(document.querySelector('[data-field-group="quote"]')).toBeTruthy();
    expect(document.querySelector('[data-field-group="valuation"]')).toBeTruthy();
    expect(document.querySelector('[data-field-group="financial"]')).toBeTruthy();
    fireEvent.click(document.querySelector('[data-field-checkbox="close"]') as HTMLElement);
    expect(onChange).toHaveBeenCalledWith(["close"]);
  });

  it("显示已选数量", () => {
    render(<MiningFieldStep fields={FIELDS} selected={["close", "pe_ttm"]} />);
    expect(
      document.querySelector("[data-field-selected-count]")?.textContent,
    ).toContain("2");
  });

  it("blocked 字段不可勾选", () => {
    render(<MiningFieldStep fields={FIELDS} />);
    const box = document.querySelector(
      '[data-field-checkbox="roe_ttm"]',
    ) as HTMLInputElement;
    expect(box.disabled).toBe(true);
  });
});

describe("MiningFieldStep 校验进度（5s 轮询，非 WebSocket）", () => {
  it("发起校验后展示分片进度", async () => {
    render(<MiningFieldStep fields={FIELDS} selected={["close"]} />);
    await act(async () => {
      fireEvent.click(document.querySelector("[data-field-start]") as HTMLElement);
    });
    expect(createValidation).toHaveBeenCalled();
    const panel = document.querySelector("[data-field-progress]");
    expect(panel).toBeTruthy();
    expect(panel?.querySelector("[data-field-progress-done]")?.textContent).toContain("12");
    expect(panel?.querySelector("[data-field-progress-total]")?.textContent).toContain("60");
  });

  it("5s 轮询刷新进度（不引入 WebSocket）", async () => {
    vi.useFakeTimers();
    const wsSpy = vi.spyOn(window as unknown as { WebSocket: unknown },
      "WebSocket" as never).mockImplementation(() => undefined as never);
    render(<MiningFieldStep fields={FIELDS} selected={["close"]} />);
    await act(async () => {
      fireEvent.click(document.querySelector("[data-field-start]") as HTMLElement);
    });
    const first = getValidation.mock.calls.length;
    await act(async () => {
      vi.advanceTimersByTime(5000);
      await Promise.resolve();
    });
    expect(getValidation.mock.calls.length).toBeGreaterThan(first);
    expect(wsSpy).not.toHaveBeenCalled();
    wsSpy.mockRestore();
  });

  it("校验通过后展示通过标记", async () => {
    getValidation.mockResolvedValue({
      ...RUNNING, status: "passed", passed: true, progress: { done: 60, total: 60 },
    });
    render(<MiningFieldStep fields={FIELDS} selected={["close"]} />);
    await act(async () => {
      fireEvent.click(document.querySelector("[data-field-start]") as HTMLElement);
    });
    expect(document.querySelector("[data-field-passed]")).toBeTruthy();
  });
});

describe("MiningFieldStep 阻断详情与出口", () => {
  it("阻断时逐字段展示问题类型与当前/要求值", async () => {
    getValidation.mockResolvedValue({
      ...RUNNING,
      status: "blocked",
      blocked: [
        { field: "roe_ttm", name_zh: "ROE_TTM", issue: "覆盖率不足",
          current: 0.612, required: 0.8 },
      ],
    });
    render(<MiningFieldStep fields={FIELDS} selected={["roe_ttm"]} />);
    await act(async () => {
      fireEvent.click(document.querySelector("[data-field-start]") as HTMLElement);
    });
    const items = document.querySelectorAll("[data-field-blocked-item]");
    expect(items.length).toBe(1);
    const text = items[0].textContent ?? "";
    expect(text).toContain("roe_ttm");
    expect(text).toContain("0.612");
    expect(text).toContain("0.8");
  });

  it("阻断只给两个出口：重新选择字段 / 去修复数据", async () => {
    getValidation.mockResolvedValue({
      ...RUNNING, status: "blocked",
      blocked: [{ field: "roe_ttm", issue: "覆盖率不足" }],
    });
    const onReselect = vi.fn();
    const onRepair = vi.fn();
    render(
      <MiningFieldStep fields={FIELDS} selected={["roe_ttm"]}
        onReselectFields={onReselect} onRepairData={onRepair} />,
    );
    await act(async () => {
      fireEvent.click(document.querySelector("[data-field-start]") as HTMLElement);
    });
    const reselect = document.querySelector("[data-field-exit-reselect]");
    const repair = document.querySelector("[data-field-exit-repair]");
    expect(reselect).toBeTruthy();
    expect(repair).toBeTruthy();
    await act(async () => { fireEvent.click(reselect as HTMLElement); });
    await act(async () => { fireEvent.click(repair as HTMLElement); });
    expect(onReselect).toHaveBeenCalled();
    expect(onRepair).toHaveBeenCalled();
  });

  it("不提供逐字段自动修复（not_do）", async () => {
    getValidation.mockResolvedValue({
      ...RUNNING, status: "blocked",
      blocked: [{ field: "roe_ttm", issue: "覆盖率不足" }],
    });
    render(<MiningFieldStep fields={FIELDS} selected={["roe_ttm"]} />);
    await act(async () => {
      fireEvent.click(document.querySelector("[data-field-start]") as HTMLElement);
    });
    const item = document.querySelector("[data-field-blocked-item]");
    expect(item?.querySelector("[data-field-autofix]")).toBeNull();
    // 阻断项内除出口按钮外不应有其它操作按钮
    expect(item?.querySelectorAll("button").length ?? 0).toBe(0);
  });
});
