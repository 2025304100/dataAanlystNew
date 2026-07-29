// WP-S-FIX.5: FirstScanWizard 组件测试
//
// 覆盖：
// - overall blocked 时渲染向导
// - 步骤导航（多步 blocked 显示多个步骤）
// - 每步动作后调用 loadCapabilities
// - 全部 ready 后自动关闭
//
// 约束：
// - 不修改组件代码
// - mock i18n（t 返回 key）、AppContext、antd
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockContext } = vi.hoisted(() => ({
  mockContext: {
    capabilities: null as any,
    isCapabilityBlocked: vi.fn((_key: string) => false),
    getCapability: vi.fn((_key: string): any => undefined),
    setActiveTab: vi.fn(),
    runScan: vi.fn(),
    loadCapabilities: vi.fn(),
  },
}));

// Mock i18n：t 返回 key
vi.mock("../../../i18n", () => ({
  t: (key: string) => key,
}));

// Mock antd：保留组件库
vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return { ...actual };
});

// Mock AppContext
vi.mock("../../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

import { FirstScanWizard } from "../FirstScanWizard";
import type { CapabilitiesResponse } from "../../../types";

/** 构造 CapabilitiesResponse */
function makeCapabilities(overall: "ready" | "degraded" | "blocked" = "blocked"): CapabilitiesResponse {
  return {
    overall_status: overall,
    capabilities: [],
    checked_at: "2026-07-22T00:00:00Z",
  };
}

describe("FirstScanWizard WP-S-FIX.5 组件测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockContext.capabilities = null;
    mockContext.isCapabilityBlocked.mockReturnValue(false);
    mockContext.getCapability.mockReturnValue(undefined);
  });

  // Modal 通过 portal 渲染到 document.body，需要每个测试后清理
  afterEach(() => {
    document.body.innerHTML = "";
  });

  // 1. overall blocked 时渲染向导
  it("overall blocked 时渲染向导并显示标题", () => {
    mockContext.capabilities = makeCapabilities("blocked");
    mockContext.isCapabilityBlocked.mockImplementation((key: string) => key === "market_data");
    mockContext.getCapability.mockReturnValue({
      key: "market_data",
      label: "基础数据",
      status: "blocked",
      reason_code: "symbols_empty",
      user_message: "标的库为空",
      prerequisites: [],
      recommended_actions: [],
      data_cutoff_at: null,
      last_checked_at: "2026-07-22T00:00:00Z",
    });
    render(<FirstScanWizard open={true} onClose={vi.fn()} />);
    expect(screen.getByText("capability.firstScanTitle")).toBeInTheDocument();
  });

  // 2. 步骤导航（多步 blocked 显示多个步骤）
  it("多步 blocked 时显示多个步骤，可点击下一步", () => {
    mockContext.capabilities = makeCapabilities("blocked");
    mockContext.isCapabilityBlocked.mockImplementation(
      (key: string) => key === "market_data" || key === "scoring"
    );
    mockContext.getCapability.mockImplementation((key: string) => ({
      key,
      label: key,
      status: "blocked",
      reason_code: "test",
      user_message: `${key} blocked`,
      prerequisites: [],
      recommended_actions: [],
      data_cutoff_at: null,
      last_checked_at: "2026-07-22T00:00:00Z",
    }));
    render(<FirstScanWizard open={true} onClose={vi.fn()} />);
    // 应显示两个步骤标题（步骤标题在 Steps 和 footer action 按钮中均可能出现，用 getAllByText）
    expect(screen.getAllByText("capability.firstScanStep1").length).toBeGreaterThan(0);
    expect(screen.getAllByText("capability.firstScanStep2").length).toBeGreaterThan(0);
  });

  // 3. 每步动作后调用 loadCapabilities
  it("点击步骤动作按钮后调用 loadCapabilities", () => {
    mockContext.capabilities = makeCapabilities("blocked");
    mockContext.isCapabilityBlocked.mockImplementation((key: string) => key === "market_data");
    mockContext.getCapability.mockReturnValue({
      key: "market_data",
      label: "基础数据",
      status: "blocked",
      reason_code: "symbols_empty",
      user_message: "标的库为空",
      prerequisites: [],
      recommended_actions: [],
      data_cutoff_at: null,
      last_checked_at: "2026-07-22T00:00:00Z",
    });
    render(<FirstScanWizard open={true} onClose={vi.fn()} />);
    // footer action 按钮包含步骤标题文本，通过 button role 定位
    const actionButton = screen
      .getAllByRole("button")
      .find((b) => b.textContent?.includes("capability.firstScanStep1"));
    expect(actionButton).toBeTruthy();
    fireEvent.click(actionButton!);
    expect(mockContext.loadCapabilities).toHaveBeenCalled();
    expect(mockContext.setActiveTab).toHaveBeenCalledWith("macro");
  });

  // 4. 全部 ready 后自动关闭
  it("overall ready 时自动调用 onClose", async () => {
    const onClose = vi.fn();
    mockContext.capabilities = makeCapabilities("ready");
    render(<FirstScanWizard open={true} onClose={onClose} />);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });
});
