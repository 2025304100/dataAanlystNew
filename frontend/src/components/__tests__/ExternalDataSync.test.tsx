import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { message } from "antd";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
// globals: true 配置下 vi 作为全局变量在 hoisted 回调中可用
const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    syncFundamental: vi.fn(async () => ({ total: 5, success: 4, skipped: 1, failed: 0, errors: [] })),
    syncFinancialReports: vi.fn(async () => ({ total: 5, success: 5, skipped: 0, failed: 0, records: 100, errors: [] })),
    syncLhbInstitution: vi.fn(async () => ({ total: 8, success: 8, skipped: 0, failed: 0, records: 8, errors: [] })),
    syncHotRank: vi.fn(async () => ({ total: 100, success: 100, skipped: 0, failed: 0, records: 100, errors: [] })),
    syncTailProxy: vi.fn(async () => ({ total: 20, success: 18, skipped: 2, failed: 0, records: 18, errors: [] })),
    syncCapitalFlow: vi.fn(async () => ({ total: 5, success: 4, skipped: 1, failed: 0, errors: [] })),
    syncEtfIndicators: vi.fn(async () => ({ total: 3, success: 3, skipped: 0, failed: 0, errors: [] })),
  },
}));

// Mock i18n: t(key) 返回 key，template 返回拼接后的字符串
vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, params: Record<string, string | number> = {}) =>
    key.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? "")),
  DOT: " | ",
}));

// Mock antd message
vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return {
    ...actual,
    message: {
      ...actual.message,
      success: vi.fn(),
      error: vi.fn(),
      warning: vi.fn(),
      info: vi.fn(),
    },
  };
});

// Mock api/client
vi.mock("../../api/client", () => ({
  api: mockApi,
}));

import ExternalDataSync from "../ExternalDataSync";

describe("ExternalDataSync 组件渲染测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("should render section title and description", () => {
    render(<ExternalDataSync />);
    expect(screen.getByText("extSectionTitle")).toBeInTheDocument();
    expect(screen.getByText("extSectionDesc")).toBeInTheDocument();
  });

  it("should render source label and default select value", () => {
    render(<ExternalDataSync />);
    // 组件中文本为 "extSourceLabel:" (含冒号)，用 partial match
    expect(screen.getByText(/extSourceLabel/)).toBeInTheDocument();
    // antd Select 的默认值通过 combobox 渲染
    const select = screen.getByRole("combobox");
    expect(select).toBeInTheDocument();
  });

  it("should render includeNorthbound checkbox checked by default", () => {
    render(<ExternalDataSync />);
    const checkbox = screen.getByRole("checkbox", { name: "extIncludeNorthbound" });
    expect(checkbox).toBeChecked();
  });

  it("should render seven sync sub-cards with titles", () => {
    render(<ExternalDataSync />);
    // 三个子 Card 标题（每个标题在 Card title 和 Button 中各出现一次）
    expect(screen.getAllByText("extSyncFundamental").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("extSyncFinancial").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("extSyncLhb").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("extSyncHotRank").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("extSyncTailProxy").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("extSyncCapitalFlow").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("extSyncEtf").length).toBeGreaterThanOrEqual(1);
  });

  it("should render descriptions for each sync card", () => {
    render(<ExternalDataSync />);
    expect(screen.getByText("extSyncFundamentalDesc")).toBeInTheDocument();
    expect(screen.getByText("extSyncFinancialDesc")).toBeInTheDocument();
    expect(screen.getByText("extSyncLhbDesc")).toBeInTheDocument();
    expect(screen.getByText("extSyncHotRankDesc")).toBeInTheDocument();
    expect(screen.getByText("extSyncTailProxyDesc")).toBeInTheDocument();
    expect(screen.getByText("extSyncCapitalFlowDesc")).toBeInTheDocument();
    expect(screen.getByText("extSyncEtfDesc")).toBeInTheDocument();
  });

  it("should call syncFundamental when sync button clicked", async () => {
    render(<ExternalDataSync />);
    // Button 内含 SyncOutlined 图标，accessible name 为 "sync extSyncFundamental"
    const syncBtn = screen.getByRole("button", { name: /extSyncFundamental/ });
    fireEvent.click(syncBtn);
    await waitFor(() => {
      expect(mockApi.syncFundamental).toHaveBeenCalledWith("watchlist");
    });
  });

  it("should call syncCapitalFlow with includeNorthbound when clicked", async () => {
    render(<ExternalDataSync />);
    const syncBtn = screen.getByRole("button", { name: /extSyncCapitalFlow/ });
    fireEvent.click(syncBtn);
    await waitFor(() => {
      expect(mockApi.syncCapitalFlow).toHaveBeenCalledWith("watchlist", true);
    });
  });

  it("should call syncFinancialReports when sync button clicked", async () => {
    render(<ExternalDataSync />);
    const syncBtn = screen.getByRole("button", { name: /extSyncFinancial/ });
    fireEvent.click(syncBtn);
    await waitFor(() => {
      expect(mockApi.syncFinancialReports).toHaveBeenCalledWith("watchlist");
    });
  });

  it("should call syncLhbInstitution for the last 30 days", async () => {
    render(<ExternalDataSync />);
    const syncBtn = screen.getByRole("button", { name: /extSyncLhb/ });
    fireEvent.click(syncBtn);
    await waitFor(() => {
      expect(mockApi.syncLhbInstitution).toHaveBeenCalledWith(30);
    });
  });

  it("should call syncHotRank for the current snapshot", async () => {
    render(<ExternalDataSync />);
    const syncBtn = screen.getByRole("button", { name: /extSyncHotRank/ });
    fireEvent.click(syncBtn);
    await waitFor(() => {
      expect(mockApi.syncHotRank).toHaveBeenCalledWith();
    });
  });

  it("should call syncTailProxy for the top 20 candidates", async () => {
    render(<ExternalDataSync />);
    const syncBtn = screen.getByRole("button", { name: /extSyncTailProxy/ });
    fireEvent.click(syncBtn);
    await waitFor(() => {
      expect(mockApi.syncTailProxy).toHaveBeenCalledWith(20);
    });
  });

  it("should call syncEtfIndicators when sync button clicked", async () => {
    render(<ExternalDataSync />);
    const syncBtn = screen.getByRole("button", { name: /extSyncEtf/ });
    fireEvent.click(syncBtn);
    await waitFor(() => {
      expect(mockApi.syncEtfIndicators).toHaveBeenCalledWith("watchlist");
    });
  });
});

describe("ExternalDataSync 交互测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("should call syncFundamental with selected source after Select changed", async () => {
    const user = userEvent.setup();
    render(<ExternalDataSync />);
    // 打开 source Select 下拉
    const selector = document.querySelector(".ant-select-selector") as HTMLElement;
    fireEvent.mouseDown(selector);
    await waitFor(() => {
      const opts = document.querySelectorAll(".ant-select-item-option");
      expect(opts.length).toBeGreaterThan(0);
    });
    // 选择 "extSourcePositions" (value=positions)
    const opts = Array.from(document.querySelectorAll(".ant-select-item-option"));
    const target = opts.find((o) => o.textContent?.includes("extSourcePositions"));
    expect(target).toBeDefined();
    fireEvent.click(target!);
    // 点击 fundamental sync 按钮
    const syncBtn = screen.getByRole("button", { name: /extSyncFundamental/ });
    await user.click(syncBtn);
    await waitFor(() => {
      expect(mockApi.syncFundamental).toHaveBeenCalledWith("positions");
    });
  });

  it("should call syncCapitalFlow with false after checkbox unchecked", async () => {
    const user = userEvent.setup();
    render(<ExternalDataSync />);
    // 初始 includeNorthbound=true，取消勾选
    const checkbox = screen.getByRole("checkbox", { name: "extIncludeNorthbound" });
    await user.click(checkbox);
    expect(checkbox).not.toBeChecked();
    // 点击 capital flow sync 按钮
    const syncBtn = screen.getByRole("button", { name: /extSyncCapitalFlow/ });
    await user.click(syncBtn);
    await waitFor(() => {
      expect(mockApi.syncCapitalFlow).toHaveBeenCalledWith("watchlist", false);
    });
  });

  it("should show success toast and Alert after sync completes", async () => {
    render(<ExternalDataSync />);
    const syncBtn = screen.getByRole("button", { name: /extSyncFundamental/ });
    fireEvent.click(syncBtn);
    await waitFor(() => {
      expect(mockApi.syncFundamental).toHaveBeenCalled();
    });
    // API 解析完成后 message.success 应被调用
    await waitFor(() => {
      expect(message.success).toHaveBeenCalled();
    });
    // Alert 显示 lastResult（label: template）
    await waitFor(() => {
      expect(screen.getByText(/extSyncFundamental: extSyncResult/)).toBeInTheDocument();
    });
  });

  it("should show loading state on sync button during sync", async () => {
    let resolveSync!: (v: any) => void;
    mockApi.syncFundamental.mockImplementation(async () => {
      return new Promise((resolve) => { resolveSync = resolve; });
    });
    render(<ExternalDataSync />);
    const syncBtn = screen.getByRole("button", { name: /extSyncFundamental/ });
    fireEvent.click(syncBtn);
    // 同步期间按钮应处于 loading 状态（antd loading 不设置 disabled，通过 class 判断）
    await waitFor(() => {
      expect(syncBtn.className).toContain("ant-btn-loading");
    });
    // 完成后恢复
    resolveSync({ total: 5, success: 5, skipped: 0, failed: 0, errors: [] });
    await waitFor(() => {
      expect(syncBtn.className).not.toContain("ant-btn-loading");
    });
  });
});
