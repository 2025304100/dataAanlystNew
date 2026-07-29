import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

const baseOverview = {
  region: "all",
  snapshot: {
    region: "all",
    market_score: 62.5,
    stance: "cautious",
    summary: "ok",
    growth_score: 3,
    inflation_score: 1,
    liquidity_score: 2,
    credit_score: 0,
    risk_score: -1,
    indicators_total: 3,
    failed_total: 0,
    created_at: "2026-07-11T10:00:00",
  },
  indicators: [
    {
      region: "us",
      indicator_key: "us_cpi_yoy",
      name: "US CPI YoY",
      category: "inflation",
      period: "2026-06",
      value: 2.1,
      previous_value: 2.0,
      delta: 0.1,
      unit: "%",
      frequency: "monthly",
      source: "macro-api",
      score: 0.5,
      status: "neutral",
    },
  ],
  brief: ["Macro is stable"],
  failed: [],
};

const { mockContext, mockApi } = vi.hoisted(() => ({
  mockContext: {
    locale: "en-US" as const,
    showToast: vi.fn((_type: string, _msg: string) => {}),
  },
  mockApi: {
    getMacroOverview: vi.fn(async () => baseOverview),
    getLatestMacroUpdateTask: vi.fn(async () => null as any),
    getMacroUpdateTask: vi.fn(async () => null as any),
    startMacroUpdateTask: vi.fn(async () => null as any),
    cancelMacroUpdateTask: vi.fn(async () => null as any),
    getMacroIndicatorHistory: vi.fn(async () => [] as any[]),
  },
}));

vi.mock("../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

vi.mock("../../api/client", () => ({
  api: mockApi,
}));

vi.mock("echarts-for-react", () => ({
  default: () => <div data-testid="mock-chart" />,
}));

import MacroData from "../MacroData";
import { setLocale } from "../../i18n";

describe("MacroData", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockContext.locale = "en-US";
    // 测试断言使用英文文案（/partial failures/i、View failure details），
    // 需同步真实 i18n 模块的 locale（组件 t() 读取 i18n 模块 currentLocale）
    setLocale("en-US");
    mockApi.getMacroOverview.mockImplementation(async () => ({ ...baseOverview }));
    mockApi.getLatestMacroUpdateTask.mockImplementation(async () => null);
    mockApi.getMacroIndicatorHistory.mockImplementation(async () => []);
  });

  afterEach(() => {
    setLocale("zh-CN");
  });

  // 使用真实定时器：组件轮询 interval 为 2500ms，真实定时器下约 3s 完成。
  // fake timers 下 waitFor 的内部 setTimeout 轮询不会触发，导致测试卡死超时。
  it("keeps partial failure details after background task finishes", async () => {
    mockApi.startMacroUpdateTask.mockImplementation(async () => ({
      id: "task-1",
      task_type: "macro_update",
      status: "queued",
      stage: "fetch",
      percent: 0,
      message: "queued",
      total: 1,
      processed: 0,
      ok_count: 0,
      failed_count: 0,
      current_item: null,
      result: null,
      errors: [],
    }));
    mockApi.getMacroUpdateTask.mockImplementation(async () => ({
      id: "task-1",
      task_type: "macro_update",
      status: "done",
      stage: "done",
      percent: 100,
      message: "Macro update completed",
      total: 1,
      processed: 1,
      ok_count: 0,
      failed_count: 1,
      current_item: null,
      result: {
        failed: [{ indicator_key: "us_cpi_yoy", name: "US CPI YoY", error: "source timeout" }],
      },
      errors: [],
    }));

    render(<MacroData />);

    await waitFor(() => {
      expect(mockApi.getMacroOverview).toHaveBeenCalled();
    });

    // 等待 overview 数据渲染完成：snapshot 非空时 empty state 消失，
    // 页面仅剩工具栏一个 "Update Macro" 按钮。
    // 否则 getByRole 会因匹配到 2 个按钮（工具栏 + 空状态）而报错。
    await screen.findByText("Market Score");

    // 工具栏按钮含 ReloadOutlined 图标（aria-label="reload"），
    // 按钮的 accessible name 为 "reload Update Macro" 而非纯 "Update Macro"，
    // 需用正则匹配。
    fireEvent.click(screen.getByRole("button", { name: /Update Macro/ }));

    // 真实定时器下，refresh() 调用 startMacroUpdateTask 返回 queued 任务，
    // useEffect 注册 2500ms 轮询 interval，约 2.5s 后触发 getMacroUpdateTask。
    // waitFor 轮询检测 getMacroUpdateTask 被调用（timeout 6000ms 覆盖 2500ms interval）。
    await waitFor(() => {
      expect(mockApi.getMacroUpdateTask).toHaveBeenCalledWith("task-1");
    }, { timeout: 6000 });

    expect(screen.getByText(/partial failures/i)).toBeInTheDocument();
    expect(mockContext.showToast).toHaveBeenCalledWith("info", expect.stringMatching(/partial failures/i));

    fireEvent.click(screen.getAllByRole("button", { name: "View failure details" })[0]);

    expect(await screen.findByText("source timeout")).toBeInTheDocument();
    // "US CPI YoY" 同时出现在指标表格和失败详情弹窗中，使用 getAllByText
    expect(screen.getAllByText("US CPI YoY").length).toBeGreaterThanOrEqual(1);
  }, 12000);
});
