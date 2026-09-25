import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, waitFor } from "@testing-library/react";

/** antd Modal 确认按钮（okText 匹配 .ant-btn-primary） */
const modalConfirm = () =>
  document.querySelector<HTMLElement>(".ant-modal-footer .ant-btn-primary");
/** antd Modal 取消按钮 */
const modalCancel = () =>
  document.querySelector<HTMLElement>(".ant-modal-footer .ant-btn:not(.ant-btn-primary)");

/** DEF-4：生成按钮在预览定型前置灰 —— 点击前必须等 300ms 防抖预览落地。 */
async function settlePreview() {
  await waitFor(() => {
    expect(document.querySelector("[data-pool-preview-stats]")).toBeTruthy();
  });
}

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
  createPool,
  createSnapshot,
  deleteLatestSnapshot,
  removeMembers,
  fetchMembers,
} = vi.hoisted(() => ({
  previewPool: vi.fn(),
  createPoolFromFilter: vi.fn(),
  createPool: vi.fn(),
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
  createPool,
  createSnapshot,
  deleteLatestSnapshot,
  removeMembers,
  fetchFilterPresets: vi.fn(async () => ({
    as_of_date: null,
    window_days: 20,
    groups: [
      { group: "market_cap", label_zh: "市值" },
      { group: "valuation", label_zh: "估值" },
    ],
    presets: [
      { preset_code: "cap_all", group: "market_cap", group_label_zh: "市值", label_zh: "全市场", field: "total_market_cap", operator: "all", min_value: null, max_value: null, applyable: true },
      { preset_code: "cap_large", group: "market_cap", group_label_zh: "市值", label_zh: "大盘", field: "total_market_cap", operator: "between", min_value: 500, max_value: null, applyable: true },
    ],
    warnings: [],
  })),
  fetchFilterFields: vi.fn(async () => ({
    fields: [
      { field: "total_market_cap", label_zh: "总市值", category: "valuation", category_label_zh: "估值与市值", availability: "available" },
      { field: "exclude_st", label_zh: "排除ST", category: "risk", category_label_zh: "风险标记", availability: "available" },
    ],
    categories: [
      { category: "market_status", label_zh: "市场与交易状态" },
      { category: "valuation", label_zh: "估值与市值" },
      { category: "risk", label_zh: "风险标记" },
    ],
    min_pool_size: 50,
  })),
  fetchMembers,
  fetchLatestSnapshot: vi.fn(async () => null),
  downloadImportTemplate: vi.fn(),
  previewImport: vi.fn(async () => ({
    total_rows: 0,
    admitted_count: 0,
    error_count: 0,
    has_errors: false,
    counts: {},
    counts_label_zh: {},
    rows: [],
  })),
  exportImportErrors: vi.fn(),
  importPoolMembers: vi.fn(async () => ({ rows: [] })),
}));

import MiningPoolStep from "./MiningPoolStep";

const PREVIEW_OK = {
  universe_size: 5000,
  hits: 285,
  excluded_total: 4715,
  min_pool_size: 50,
  can_generate: true,
  by_rule: [{ rule: "market", excluded: 2000 }],
  warnings: [] as string[],
  blocking_issues: [] as { error_code: string; detail_zh: string }[],
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
    // 弹出确认 Modal → 取消则仍在筛选入口
    expect(modalCancel()).toBeTruthy();
    fireEvent.click(modalCancel() as HTMLElement);
    expect(document.querySelector("[data-pool-filter-panel]")).toBeTruthy();
    expect(document.querySelector("[data-pool-import-panel]")).toBeNull();
  });

  it("确认清空后切到导入入口", () => {
    render(<MiningPoolStep />);
    fireEvent.click(document.querySelector('[data-pool-tab="import"]') as HTMLElement);
    fireEvent.click(modalConfirm() as HTMLElement);
    expect(document.querySelector("[data-pool-import-panel]")).toBeTruthy();
    expect(document.querySelector("[data-pool-filter-panel]")).toBeNull();
  });
});

describe("MiningPoolStep 防抖预览", () => {
  /** 展开 antd Collapse 分类（按 header 文本；收起分类内容不在 DOM，交互前需展开） */
  const openCategory = (label: string) => {
    const headers = Array.from(
      document.querySelectorAll<HTMLElement>(".ant-collapse-header"),
    );
    const target = headers.find((el) => el.textContent?.includes(label));
    if (target) fireEvent.click(target);
  };

  /** 等待筛选面板异步加载（fetchFilterPresets/fetchFilterFields resolve 后
   *  才有 data-pool-filter-input；fake timers 下用 flush 微任务替代 waitFor）。 */
  const flushPanel = async () => {
    await act(async () => {
      for (let i = 0; i < 12 && !document.querySelector("[data-filter-left]"); i += 1) {
        await Promise.resolve();
      }
    });
    // total_market_cap 输入在「估值与市值」分类（收起）里，先展开
    await act(async () => {
      openCategory("估值与市值");
    });
    return document.querySelector("[data-pool-filter-input] input") as HTMLInputElement;
  };

  it("条件变化 300ms 内不重复请求，防抖后才调用预览", async () => {
    vi.useFakeTimers();
    render(<MiningPoolStep />);
    const input = await flushPanel();
    expect(input).toBeTruthy();
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
    const input = await flushPanel();
    act(() => {
      fireEvent.change(input, { target: { value: "100" } });
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
    await settlePreview();
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(createSnapshot).toHaveBeenCalled();
    expect(document.querySelector("[data-pool-lock-banner]")).toBeTruthy();
  });

  it("锁定态下筛选/导入/批量删除全部置灰", async () => {
    render(<MiningPoolStep />);
    await settlePreview();
    await act(async () => {
      fireEvent.click(document.querySelector("[data-pool-generate]") as HTMLElement);
    });
    // market_status 分类默认展开：市场多选 Checkbox 锁定后禁用（antd InputNumber/Checkbox）
    const marketCheck = document.querySelector(
      '[data-filter-multi="markets"] input',
    ) as HTMLInputElement;
    const importTab = document.querySelector('[data-pool-tab="import"]') as HTMLElement;
    const bulkDelete = document.querySelector("[data-pool-bulk-delete]") as HTMLElement;
    expect(marketCheck.disabled).toBe(true);
    expect(importTab.hasAttribute("disabled") || importTab.getAttribute("aria-disabled") === "true").toBe(true);
    expect(bulkDelete.hasAttribute("disabled")).toBe(true);
  });

  it("按钮状态机：分析完成后变为『查看候选池看板』", async () => {
    render(<MiningPoolStep />);
    await settlePreview();
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
    await settlePreview();
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
    await settlePreview();
    await act(async () => {
      fireEvent.click(document.querySelector("[data-pool-generate]") as HTMLElement);
    });
    await act(async () => {
      fireEvent.click(document.querySelector("[data-pool-generate]") as HTMLElement);
    });
    await act(async () => {
      fireEvent.click(document.querySelector("[data-pool-reselect]") as HTMLElement);
    });
    // 二次确认 Modal
    expect(modalConfirm()).toBeTruthy();
    await act(async () => {
      fireEvent.click(modalConfirm() as HTMLElement);
    });
    expect(deleteLatestSnapshot).toHaveBeenCalled();
    expect(document.querySelector("[data-pool-lock-banner]")).toBeNull();
  });
});

describe("MiningPoolStep 分层筛选布局（P2-4）", () => {
  const openCategory = (label: string) => {
    const headers = Array.from(
      document.querySelectorAll<HTMLElement>(".ant-collapse-header"),
    );
    const target = headers.find((el) => el.textContent?.includes(label));
    if (target) fireEvent.click(target);
  };

  it("渲染左栏预设 + 右栏分类 Accordion 双栏", async () => {
    render(<MiningPoolStep />);
    await waitFor(
      () => {
        expect(document.querySelector("[data-filter-left]")).toBeTruthy();
      },
      { timeout: 3000 },
    );
    expect(document.querySelector("[data-filter-right]")).toBeTruthy();
    // 左栏预设按钮（antd Tabs 渲染，market_cap 默认激活）
    expect(document.querySelector('[data-filter-preset="cap_all"]')).toBeTruthy();
    // 右栏分类（market_status 默认展开；展开 valuation 后含范围输入）
    expect(document.querySelector('[data-filter-category="market_status"]')).toBeTruthy();
    await act(async () => {
      openCategory("估值与市值");
    });
    expect(document.querySelector('[data-filter-category="valuation"]')).toBeTruthy();
    expect(document.querySelector("[data-pool-filter-input] input")).toBeTruthy();
    // 结构性控件：市场多选（market_status 默认展开）
    expect(document.querySelector('[data-filter-multi="markets"]')).toBeTruthy();
  });

  it("点击预设把服务端分位边界写入 filter_config（valuation.total_market_cap）", async () => {
    const { default: PoolFilterPanel } = await import("./PoolFilterPanel");
    const onChange = vi.fn();
    render(<PoolFilterPanel locked={false} value={{}} onChange={onChange} />);
    await waitFor(
      () => {
        expect(document.querySelector('[data-filter-preset="cap_large"]')).toBeTruthy();
      },
      { timeout: 3000 },
    );
    await act(async () => {
      fireEvent.click(document.querySelector('[data-filter-preset="cap_large"]') as HTMLElement);
    });
    // PoolFilterPanel onChange 收到 valuation.total_market_cap {min:500}
    const calls = onChange.mock.calls.map((c) => c[0] as Record<string, unknown>);
    let last: Record<string, unknown> | undefined;
    for (const cfg of calls) if (cfg.valuation != null) last = cfg;
    expect(last).toBeTruthy();
    const val = (last?.valuation as Record<string, unknown>).total_market_cap as Record<string, unknown>;
    expect(val?.min).toBe(500);
  });

  it("B2：预设应用后标记「已应用」，手工修改后标记「自定义」（§3.2）", async () => {
    const { default: PoolFilterPanel } = await import("./PoolFilterPanel");
    const onChange = vi.fn();
    render(<PoolFilterPanel locked={false} value={{}} onChange={onChange} />);
    await waitFor(
      () => {
        expect(document.querySelector('[data-filter-preset="cap_large"]')).toBeTruthy();
      },
      { timeout: 3000 },
    );
    await act(async () => {
      fireEvent.click(document.querySelector('[data-filter-preset="cap_large"]') as HTMLElement);
    });
    // 应用后：已应用 Tag 出现，且按钮非自定义态
    expect(
      document.querySelector('[data-filter-preset="cap_large"] [data-filter-preset-tag="applied"]'),
    ).toBeTruthy();
    expect(
      document.querySelector('[data-filter-preset="cap_large"] [data-filter-preset-tag="custom"]'),
    ).toBeNull();

    // 手工修改该字段（展开「估值与市值」分类，改 min）
    const headers = Array.from(
      document.querySelectorAll<HTMLElement>(".ant-collapse-header"),
    );
    const target = headers.find((el) => el.textContent?.includes("估值与市值"));
    if (target) fireEvent.click(target);
    await waitFor(
      () => {
        expect(document.querySelector("[data-pool-filter-input] input")).toBeTruthy();
      },
      { timeout: 3000 },
    );
    await act(async () => {
      fireEvent.change(
        document.querySelector("[data-pool-filter-input] input") as HTMLInputElement,
        { target: { value: "800" } },
      );
    });
    // 手工修改后：预设按钮显示「自定义」Tag，且不再显示「已应用」
    expect(
      document.querySelector('[data-filter-preset="cap_large"] [data-filter-preset-tag="custom"]'),
    ).toBeTruthy();
    expect(
      document.querySelector('[data-filter-preset="cap_large"] [data-filter-preset-tag="applied"]'),
    ).toBeNull();
  });
});

describe("MiningPoolStep 硬边界", () => {
  it("命中 <50 阻断：生成物料不可用且提示不足 50", async () => {
    previewPool.mockResolvedValue({
      ...PREVIEW_OK,
      hits: 12,
      can_generate: false,
      blocking_issues: [{ error_code: "MINING_POOL_TOO_SMALL", detail_zh: "有效标的不足 50" }],
    });
    // 本用例走**真实定时器**：waitFor 依赖真实时间轮询，fake timers 会让它
    // 直接超时（实测 Test timed out in 5000ms）。防抖 300ms 用 waitFor 等即可。
    render(<MiningPoolStep />);
    // 等筛选面板异步加载，并展开「估值与市值」分类使输入可见
    await waitFor(
      () => {
        expect(document.querySelector("[data-filter-category]")).toBeTruthy();
      },
      { timeout: 3000 },
    );
    const headers = Array.from(
      document.querySelectorAll<HTMLElement>(".ant-collapse-header"),
    );
    const target = headers.find((el) => el.textContent?.includes("估值与市值"));
    if (target) fireEvent.click(target);
    await waitFor(
      () => {
        expect(document.querySelector("[data-pool-filter-input] input")).toBeTruthy();
      },
      { timeout: 3000 },
    );
    await act(async () => {
      fireEvent.change(
        document.querySelector("[data-pool-filter-input] input") as HTMLInputElement,
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
    const confirm = modalConfirm();
    expect(confirm).toBeTruthy();
    await act(async () => {
      fireEvent.click(confirm as HTMLElement);
    });
    expect(removeMembers).toHaveBeenCalled();
  });
});

describe("MiningPoolStep 成员表列按真实契约呈现（2026-09-21）", () => {
  /**
   * 后端 `list_members` 实测只返回 symbol/name/asset_type/market/board/industry/
   * is_st/is_active/included_at —— 没有 risk_flag / integrity / source / reason
   * 与任何行情估值指标。因此：
   *   1. 风险标记必须读真实的 `is_st`（此前读 risk_flag 永远不显示）；
   *   2. 无数据的列**整列隐藏**并给出缺口说明，而不是每行渲染 "-"。
   */
  it("is_st=1 渲染 ST 风险标记；缺失的列不渲染并给出缺口说明", async () => {
    fetchMembers.mockResolvedValue({
      items: [
        { symbol: "600000", name: "浦发银行", market: "sh", board: "main", is_st: 0 },
        { symbol: "000004", name: "国华网安", market: "sz", board: "main", is_st: 1 },
      ],
      total: 2, page: 1, page_size: 20,
    });
    render(<MiningPoolStep poolId="pool-1" />);
    await act(async () => {
      await Promise.resolve();
    });

    // 风险标记来自真实字段 is_st
    const risks = document.querySelectorAll("[data-pool-member-risk]");
    expect(risks.length).toBe(1);
    expect(risks[0].textContent).toBe("ST");

    // 交易所/板块列存在（真实字段 market/board）
    const headers = Array.from(
      document.querySelectorAll("[data-pool-member-table] thead th"),
    ).map((th) => th.textContent ?? "");
    expect(headers.join("|")).toContain("交易所/板块");
    // 后端未提供的列不渲染（不出现一列 "-"）
    expect(headers.join("|")).not.toContain("数据完整性");
    expect(headers.join("|")).not.toContain("纳入来源");
    expect(document.querySelector("[data-pool-member-gap-note]")).toBeTruthy();

    // 勾选框数量仍是成员数（测试钩子不变）
    expect(
      document.querySelectorAll(
        "[data-pool-member-table] tbody input[type=checkbox]",
      ).length,
    ).toBe(2);
  });
});


describe("DEF-4：预览三态与生成门控（VIS-2）", () => {
  it("预览未定型 → 生成按钮置灰；定型后恢复可用", async () => {
    let resolvePreview: (v: unknown) => void = () => undefined;
    previewPool.mockImplementation(
      () => new Promise((res) => { resolvePreview = res; }),
    );
    render(<MiningPoolStep />);
    await waitFor(() => {
      expect(document.querySelector("[data-pool-preview-loading]")).toBeTruthy();
    });
    expect(
      (document.querySelector("[data-pool-generate]") as HTMLButtonElement)
        .hasAttribute("disabled"),
    ).toBe(true);

    await act(async () => { resolvePreview(PREVIEW_OK); });
    await waitFor(() => {
      expect(
        (document.querySelector("[data-pool-generate]") as HTMLButtonElement)
          .hasAttribute("disabled"),
      ).toBe(false);
    });
  });

  it("预览失败 → 错误提示+重试；重试成功后按钮恢复", async () => {
    previewPool.mockRejectedValueOnce(new Error("warehouse busy"));
    render(<MiningPoolStep />);
    await waitFor(() => {
      expect(document.querySelector("[data-pool-preview-error]")).toBeTruthy();
    });
    expect(
      (document.querySelector("[data-pool-generate]") as HTMLButtonElement)
        .hasAttribute("disabled"),
    ).toBe(true);

    previewPool.mockResolvedValue(PREVIEW_OK);
    fireEvent.click(document.querySelector("[data-pool-preview-retry]") as HTMLElement);
    await waitFor(() => {
      expect(document.querySelector("[data-pool-preview-stats]")).toBeTruthy();
    });
    expect(
      (document.querySelector("[data-pool-generate]") as HTMLButtonElement)
        .hasAttribute("disabled"),
    ).toBe(false);
  });
});


describe("DEF-7：筛选面板渲染告警（React key）", () => {
  it("展开含布尔条件的分类 → 不产生「unique key」警告", async () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(<MiningPoolStep />);
    // 等字段目录落地（risk 分类含 exclude_st 布尔条件）
    await waitFor(() => {
      expect(document.querySelectorAll(".ant-collapse-header").length).toBeGreaterThan(0);
    });
    const header = Array.from(
      document.querySelectorAll(".ant-collapse-header"),
    ).find((h) => (h.textContent ?? "").includes("风险标记"));
    expect(header).toBeTruthy();
    fireEvent.click(header as HTMLElement);

    await waitFor(() => {
      expect(document.querySelector("[data-filter-bool='exclude_st']")).toBeTruthy();
    });
    const keyWarnings = spy.mock.calls.filter((args) =>
      String(args[0] ?? "").includes("unique key"),
    );
    spy.mockRestore();
    expect(keyWarnings.length).toBe(0);
  });
});
