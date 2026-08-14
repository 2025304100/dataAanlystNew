import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const { mockContext, mockApi, mockTemplate } = vi.hoisted(() => ({
  mockContext: {
    setActiveTab: vi.fn(),
    runSync: vi.fn(),
    showToast: vi.fn(),
    syncTask: null as any,
    syncPolling: false,
  },
  mockApi: {
    startDataPrep: vi.fn(),
    getSnapshotStatus: vi.fn(),
  },
  mockTemplate: vi.fn((key: string, params: Record<string, string | number> = {}) => {
    return key.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? ""));
  }),
}));

vi.mock("../../../i18n", () => ({
  t: (key: string) => key,
  template: mockTemplate,
}));
vi.mock("../../../context/AppContext", () => ({ useApp: () => mockContext }));
vi.mock("../../../api/client", () => ({ api: mockApi }));

import { DataPrepActions } from "../DataPrepActions";

describe("DataPrepActions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockContext.syncTask = null;
    mockContext.syncPolling = false;
    mockTemplate.mockImplementation((key: string, params: Record<string, string | number> = {}) => {
      return key.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? ""));
    });
    mockApi.getSnapshotStatus.mockResolvedValue({
      scope: "cn-stock", has_ready_snapshot: false, has_building_snapshot: false,
      last_data_prep_status: null, recommended_action: null,
    });
    mockApi.startDataPrep.mockResolvedValue({ id: "task-123", status: "queued" });
  });

  it("渲染三个联动入口按钮", async () => {
    render(<DataPrepActions scope="cn-stock" />);
    await waitFor(() => {
      expect(screen.getByText("opportunity.goToMarketData")).toBeInTheDocument();
      expect(screen.getByText("opportunity.runIncrementalSync")).toBeInTheDocument();
      expect(screen.getByText("opportunity.autoScanWhenReady")).toBeInTheDocument();
    });
  });

  it("点击前往基础数据跳转到 macro tab", async () => {
    render(<DataPrepActions scope="cn-stock" />);
    await waitFor(() => expect(mockApi.getSnapshotStatus).toHaveBeenCalled());
    fireEvent.click(screen.getByText("opportunity.goToMarketData"));
    expect(mockContext.setActiveTab).toHaveBeenCalledWith("macro");
  });

  it("点击运行增量同步调用 ctx.runSync", async () => {
    render(<DataPrepActions scope="cn-stock" />);
    await waitFor(() => expect(mockApi.getSnapshotStatus).toHaveBeenCalled());
    fireEvent.click(screen.getByText("opportunity.runIncrementalSync"));
    expect(mockContext.runSync).toHaveBeenCalled();
  });

  it("点击数据就绪后自动扫描调用 startDataPrep API", async () => {
    render(<DataPrepActions scope="cn-stock" />);
    await waitFor(() => expect(mockApi.getSnapshotStatus).toHaveBeenCalled());
    fireEvent.click(screen.getByText("opportunity.autoScanWhenReady"));
    await waitFor(() => {
      expect(mockApi.startDataPrep).toHaveBeenCalledWith({
        scope: "cn-stock",
        trigger_fast_scan_after_ready: true,
      });
      expect(mockContext.showToast).toHaveBeenCalledWith("info", expect.any(String));
    });
  });

  it("同步进行中时，同步按钮显示 loading 且禁用", async () => {
    mockContext.syncTask = {
      id: "task-1",
      status: "running",
      stage: "sync",
      percent: 45,
      message: "正在同步行情",
      total: 100,
      processed: 45,
    };
    render(<DataPrepActions scope="cn-stock" />);
    await waitFor(() => expect(mockApi.getSnapshotStatus).toHaveBeenCalled());

    // Find the sync button (not the progress alert text)
    const buttons = screen.getAllByRole("button");
    const syncBtn = buttons.find(btn => 
      btn.textContent?.includes("opportunity.syncInProgress")
    );
    expect(syncBtn).toBeTruthy();
    expect(syncBtn).toBeDisabled();
  });

  it("同步进行中时，自动扫描按钮被禁用", async () => {
    mockContext.syncTask = {
      id: "task-1",
      status: "running",
      stage: "sync",
      percent: 45,
      message: "正在同步行情",
      total: 100,
      processed: 45,
    };
    render(<DataPrepActions scope="cn-stock" />);
    await waitFor(() => expect(mockApi.getSnapshotStatus).toHaveBeenCalled());

    const buttons = screen.getAllByRole("button");
    const scanBtn = buttons.find(btn => 
      btn.textContent?.includes("opportunity.autoScanWhenReady")
    );
    expect(scanBtn).toBeTruthy();
    expect(scanBtn).toBeDisabled();
  });

  it("同步进行中时显示进度条和阶段信息", async () => {
    mockContext.syncTask = {
      id: "task-1",
      status: "running",
      stage: "sync",
      percent: 45,
      message: "正在同步行情",
      total: 100,
      processed: 45,
    };
    render(<DataPrepActions scope="cn-stock" />);
    await waitFor(() => expect(mockApi.getSnapshotStatus).toHaveBeenCalled());

    // Should have sync in progress text (in alert message + button)
    const syncInProgressElements = screen.getAllByText("opportunity.syncInProgress");
    expect(syncInProgressElements.length).toBeGreaterThanOrEqual(1);
    
    // Progress bar should show percentage
    expect(screen.getByText("45%")).toBeInTheDocument();
  });

  it("同步进行中时点击同步按钮不会重复触发", async () => {
    mockContext.syncTask = {
      id: "task-1",
      status: "queued",
      stage: "prepare",
      percent: 2,
      message: "解析标的列表",
      total: 100,
      processed: 2,
    };
    render(<DataPrepActions scope="cn-stock" />);
    await waitFor(() => expect(mockApi.getSnapshotStatus).toHaveBeenCalled());

    // The button is disabled when syncing, so clicking won't trigger handler
    const buttons = screen.getAllByRole("button");
    const syncBtn = buttons.find(btn => 
      btn.textContent?.includes("opportunity.syncInProgress")
    );
    expect(syncBtn).toBeDisabled();
    // runSync should not have been called (since it was already syncing)
    expect(mockContext.runSync).not.toHaveBeenCalled();
  });

  it("同步完成时显示成功摘要", async () => {
    mockContext.syncTask = {
      id: "task-1",
      status: "done",
      stage: "done",
      percent: 100,
      total: 100,
      result: { ok_count: 98, symbols_total: 100, failed_count: 2 },
    };
    render(<DataPrepActions scope="cn-stock" />);
    await waitFor(() => expect(mockApi.getSnapshotStatus).toHaveBeenCalled());

    expect(screen.getByText("opportunity.syncCompleted")).toBeInTheDocument();
    // template function should be called with correct sync stats
    expect(mockTemplate).toHaveBeenCalledWith("opportunity.syncSummaryMsg", {
      ok: 98,
      total: 100,
      failed: 2,
    });
  });

  it("同步失败时显示错误信息", async () => {
    mockContext.syncTask = {
      id: "task-1",
      status: "failed",
      stage: "sync",
      percent: 30,
      message: "网络错误",
      total: 100,
      processed: 30,
    };
    render(<DataPrepActions scope="cn-stock" />);
    await waitFor(() => expect(mockApi.getSnapshotStatus).toHaveBeenCalled());

    expect(screen.getByText("opportunity.syncFailed")).toBeInTheDocument();
  });

  it("同步进行中时自动扫描按钮处于禁用状态", async () => {
    mockContext.syncTask = {
      id: "task-1",
      status: "running",
      stage: "sync",
      percent: 50,
      message: "正在同步行情",
      total: 100,
      processed: 50,
    };
    render(<DataPrepActions scope="cn-stock" />);
    await waitFor(() => expect(mockApi.getSnapshotStatus).toHaveBeenCalled());

    const buttons = screen.getAllByRole("button");
    const scanBtn = buttons.find(btn => 
      btn.textContent?.includes("opportunity.autoScanWhenReady")
    );
    expect(scanBtn).toBeTruthy();
    // Button should be disabled during sync
    expect(scanBtn).toBeDisabled();
  });

  it("前往基础数据按钮点击时显示 toast 反馈", async () => {
    render(<DataPrepActions scope="cn-stock" />);
    await waitFor(() => expect(mockApi.getSnapshotStatus).toHaveBeenCalled());
    fireEvent.click(screen.getByText("opportunity.goToMarketData"));
    expect(mockContext.showToast).toHaveBeenCalledWith("info", "opportunity.navigatingToMarketData");
  });
});