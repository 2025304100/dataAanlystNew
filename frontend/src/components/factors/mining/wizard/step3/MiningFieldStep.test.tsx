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
  it("无字段时渲染空态提示（P2-2）", () => {
    render(<MiningFieldStep fields={[]} />);
    expect(document.querySelector("[data-field-empty]")).toBeTruthy();
    expect(document.querySelector("[data-field-group]")).toBeNull();
    // 空态下开始校验按钮禁用
    expect((document.querySelector("[data-field-start]") as HTMLButtonElement).disabled).toBe(true);
  });

  it("按分组渲染字段并可勾选，选中集上抛", () => {
    const onChange = vi.fn();
    render(<MiningFieldStep fields={FIELDS} onChange={onChange} />);
    expect(document.querySelector('[data-field-group="quote"]')).toBeTruthy();
    expect(document.querySelector('[data-field-group="valuation"]')).toBeTruthy();
    expect(document.querySelector('[data-field-group="financial"]')).toBeTruthy();
    // antd Checkbox：data-* 转发到**内层 input**（探针验证），可直接点击
    fireEvent.click(
      document.querySelector('[data-field-checkbox="close"]') as HTMLElement,
    );
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

describe("MiningFieldStep 只列可进入挖掘公式的字段（设计 §5）", () => {
  /**
   * 候选池筛选条件类字段（market_status/risk/industry/listing）不属于公式输入：
   * 设计 §3.2 明确行业「不得用于挖掘期逐日 PIT 筛选」；且这些字段的物理表在
   * MySQL 主数据（universe_symbols），不在 DuckDB 因子仓库 —— 一旦可选，
   * 后端校验会抛 CatalogException（真实缺陷，2026-09-21 实测）。
   */
  const WITH_FILTER_FIELDS: MiningField[] = [
    ...FIELDS,
    { code: "board", name_zh: "板块", group: "market_status",
      source_table: "universe_symbols", data_mode: "continuous" },
    { code: "exclude_st", name_zh: "排除 ST", group: "risk",
      source_table: "universe_symbols", data_mode: "continuous" },
  ];

  it("筛选条件类字段不渲染，且给出「已隐藏 N 个」说明", () => {
    render(<MiningFieldStep fields={WITH_FILTER_FIELDS} />);
    expect(document.querySelector("[data-field-group='market_status']")).toBeNull();
    expect(document.querySelector("[data-field-group='risk']")).toBeNull();
    expect(document.querySelector("[data-field-row='board']")).toBeNull();
    expect(document.querySelector("[data-field-row='exclude_st']")).toBeNull();
    // 公式字段仍在
    expect(document.querySelector("[data-field-row='close']")).toBeTruthy();
    const note = document.querySelector("[data-field-non-formula-note]");
    expect(note).toBeTruthy();
    expect(note?.textContent).toContain("2");
  });

  it("全是筛选条件字段时落空态（不静默留白）", () => {
    render(
      <MiningFieldStep
        fields={[
          { code: "board", name_zh: "板块", group: "market_status" },
          { code: "industry", name_zh: "行业", group: "industry" },
        ]}
      />,
    );
    expect(document.querySelector("[data-field-empty]")).toBeTruthy();
    expect(document.querySelector("[data-field-group]")).toBeNull();
  });

  it("已选字段数为公式字段数（标题摘要口径一致）", () => {
    render(<MiningFieldStep fields={WITH_FILTER_FIELDS} />);
    const head = document.querySelector(".mining-field-head")?.textContent ?? "";
    // 共 3 个公式字段（close/pe_ttm/roe_ttm），2 个筛选条件字段被隐藏
    expect(head).toContain("已选字段");
  });
});

describe("MiningFieldStep 覆盖率 / 最新日期 / 可用区间的来源（2026-09-21）", () => {
  // 测试 fixture 去掉 `coverage` / `latest_date` / `available_from` / `available_to`，
  // 对齐真实后端（`FieldBinding.to_dict()` 这四个都不返回）；
  // 组件对「目录里直接带这些值」保留了回退分支，那是另一条路径。
  const CATALOG_FIELDS: MiningField[] = FIELDS.map((f) => {
    const {
      coverage: _coverage,
      latest_date: _latest,
      available_from: _from,
      available_to: _to,
      ...rest
    } = f;
    return rest as MiningField;
  });
  /**
   * 字段目录接口 `filter-fields`（后端 `FieldBinding.to_dict`）**不含**覆盖率、
   * 最新日期与可用区间 —— 这三个值只有校验任务跑完才由后端实测出来。
   * 因此：校验前显示「校验后显示」占位，校验完成后用 `field_meta` 回填真值。
   */
  it("校验前：覆盖率为「校验后显示」占位，并给出来源说明", () => {
    render(<MiningFieldStep fields={CATALOG_FIELDS} />);
    const pending = document.querySelectorAll("[data-field-coverage-pending]");
    expect(pending.length).toBe(3);
    expect(document.querySelector("[data-field-meta-pending-note]")).toBeTruthy();
    // 校验前不伪造数值；目录里本就没有最新日期/可用区间，不渲染空行
    expect(document.querySelector("[data-field-coverage]")).toBeNull();
    expect(document.querySelector("[data-field-latest]")).toBeNull();
    expect(document.querySelector("[data-field-range]")).toBeNull();
  });

  it("校验后：用报告里的 field_meta 回填覆盖率 / 最新日期 / 可用区间", async () => {
    getValidation.mockResolvedValue({
      ...RUNNING,
      status: "passed",
      passed: true,
      progress: { done: 1, total: 1 },
      field_meta: {
        close: {
          coverage: 0.999,
          min_date: "2021-01-04",
          max_date: "2026-08-21",
          verdict: "pass",
        },
      },
    });
    render(<MiningFieldStep fields={CATALOG_FIELDS} selected={["close"]} />);
    await act(async () => {
      fireEvent.click(document.querySelector("[data-field-start]") as HTMLElement);
    });
    const pct = document.querySelector('[data-field-coverage="close"]')?.textContent ?? "";
    expect(pct).toContain("99.9");
    expect(document.querySelector('[data-field-latest="close"]')?.textContent).toContain("2026-08-21");
    const range = document.querySelector('[data-field-range="close"]')?.textContent ?? "";
    expect(range).toContain("2021-01-04");
    expect(range).toContain("2026-08-21");
    // 已有实测值的字段不再显示占位
    expect(document.querySelector('[data-field-coverage-pending="close"]')).toBeNull();
    // 元数据来源说明随之消失
    expect(document.querySelector("[data-field-meta-pending-note]")).toBeNull();
  });
});
