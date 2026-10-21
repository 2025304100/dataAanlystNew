import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";

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
    getAsyncTask: vi.fn(),
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
    localStorage.clear();
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
    mockApi.getAsyncTask.mockResolvedValue({ id: "task-123", status: "running", percent: 30, message: "syncing" });
  });

  it("日常入口只需更新并筛选，首次使用不要求先跳去设置同步", async () => {
    render(<DataPrepActions scope="cn-etf" simpleMode />);
    const start = await screen.findByRole("button", { name: "opportunity.updateAndScreen" });
    expect(screen.queryByRole("button", { name: "opportunity.runIncrementalSync" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "opportunity.autoScanWhenReady" })).not.toBeInTheDocument();
    expect(screen.getByText("opportunity.firstUseHint")).toBeVisible();
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    await waitFor(() => expect(mockApi.startDataPrep).toHaveBeenCalledWith({
      scope: "cn-etf", trigger_fast_scan_after_ready: true,
    }));
    expect(await screen.findByRole("button", { name: "opportunity.updatingAndScreening" })).toBeDisabled();
  });

  it("后台行情更新时只点击一次，更新完成后自动筛选当前范围", async () => {
    mockContext.syncTask = { id: "background-sync", status: "running", percent: 50 };
    const view = render(<DataPrepActions scope="cn-etf" simpleMode />);
    fireEvent.click(await screen.findByRole("button", { name: "opportunity.updateAndScreen" }));
    expect(await screen.findByText("opportunity.backgroundSyncQueued")).toBeVisible();
    expect(mockApi.startDataPrep).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "opportunity.updatingAndScreening" })).toBeDisabled();
    mockContext.syncTask = { id: "background-sync", status: "done", percent: 100 };
    view.rerender(<DataPrepActions scope="cn-etf" simpleMode />);
    await waitFor(() => expect(mockApi.startDataPrep).toHaveBeenCalledWith({
      scope: "cn-etf", trigger_fast_scan_after_ready: true,
    }));
    expect(mockApi.startDataPrep).toHaveBeenCalledTimes(1);
  });

  it("后台更新中断时说明重试动作，不擅自启动筛选", async () => {
    mockContext.syncTask = { id: "background-sync", status: "running" };
    const view = render(<DataPrepActions simpleMode />);
    fireEvent.click(await screen.findByRole("button", { name: "opportunity.updateAndScreen" }));
    await screen.findByText("opportunity.backgroundSyncQueued");
    mockContext.syncTask = { id: "background-sync", status: "failed" };
    view.rerender(<DataPrepActions simpleMode />);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("opportunity.syncStopped"));
    expect(mockApi.startDataPrep).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "opportunity.updateAndScreen" })).toBeEnabled();
  });

  it("等待后台更新时切范围，不把原范围的等待操作带到新范围", async () => {
    mockContext.syncTask = { id: "background-sync", status: "running" };
    const view = render(<DataPrepActions scope="cn-stock" simpleMode />);
    fireEvent.click(await screen.findByRole("button", { name: "opportunity.updateAndScreen" }));
    await screen.findByText("opportunity.backgroundSyncQueued");
    view.rerender(<DataPrepActions scope="cn-etf" simpleMode />);
    await screen.findByRole("button", { name: "opportunity.updateAndScreen" });
    mockContext.syncTask = { id: "background-sync", status: "done" };
    view.rerender(<DataPrepActions scope="cn-etf" simpleMode />);
    await waitFor(() => expect(screen.queryByText("opportunity.backgroundSyncQueued")).not.toBeInTheDocument());
    expect(mockApi.startDataPrep).not.toHaveBeenCalled();
  });

  it("日常入口读取进度失败后原处重试，不暴露错误明细或重复提交任务", async () => {
    mockApi.getAsyncTask.mockRejectedValueOnce(new Error("database query failed"));
    const onPrepared = vi.fn();
    render(<DataPrepActions scope="cn-etf" simpleMode onPrepared={onPrepared} />);
    fireEvent.click(await screen.findByRole("button", { name: "opportunity.updateAndScreen" }));
    const retry = await screen.findByRole("button", { name: "opportunity.retry" });
    expect(screen.getByRole("alert")).toHaveTextContent("opportunity.prepTaskReadFailed");
    expect(screen.getByText("database query failed")).not.toBeVisible();
    mockApi.getAsyncTask.mockResolvedValueOnce({ id: "task-123", status: "done", result: { fast_scan: { status: "ok" } } });
    fireEvent.click(retry);
    expect(await screen.findByText("opportunity.prepCompleted")).toBeVisible();
    expect(onPrepared).toHaveBeenCalledWith("cn-etf", "task-123");
    expect(mockApi.startDataPrep).toHaveBeenCalledTimes(1);
  });

  it("渲染三个联动入口按钮", async () => {
    render(<DataPrepActions scope="cn-stock" />);
    await waitFor(() => {
      expect(screen.getByText("opportunity.goToMarketData")).toBeInTheDocument();
      expect(screen.getByText("opportunity.runIncrementalSync")).toBeInTheDocument();
      expect(screen.getByText("opportunity.autoScanWhenReady")).toBeInTheDocument();
    });
  });

  it("状态接口失败显示读取错误，重试成功后才展示实际快照状态", async () => {
    mockApi.getSnapshotStatus.mockRejectedValueOnce(new Error("snapshot offline"));
    render(<DataPrepActions scope="cn-stock" />);
    expect(await screen.findByText("opportunity.snapshotReadFailed")).toBeInTheDocument();
    expect(screen.getByText("snapshot offline")).toBeInTheDocument();
    expect(screen.queryByText("还没有评分快照，建议先初始化数据")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "opportunity.retryStatusRead" }));
    await waitFor(() => expect(screen.queryByText("opportunity.snapshotReadFailed")).not.toBeInTheDocument());
    expect(await screen.findByText("opportunity.dataPrepIdle")).toBeInTheDocument();
  });

  it.each([
    ["2026-09-23T00:00:00", "2026-09-23"],
    [null, "2026-09-24"],
  ])("快照展示实际数据截止日期，不用新构建时间冒充新行情 (%s)", async (cutoff, expected) => {
    mockApi.getSnapshotStatus.mockResolvedValue({
      scope: "cn-stock", has_ready_snapshot: true, has_building_snapshot: false,
      ready_snapshot_generated_at: "2026-10-20T03:00:00",
      ready_snapshot_trade_date: "2026-09-24T00:00:00",
      ready_snapshot_data_cutoff_at: cutoff,
    });
    render(<DataPrepActions />);
    expect(await screen.findByText(`opportunity.dataPrepReady · opportunity.snapshotDataAsOf: ${expected}`)).toBeInTheDocument();
    expect(screen.queryByText(/opportunity.dataPrepReady.*2026-10-20/)).not.toBeInTheDocument();
  });

  it("历史快照缺少数据日期时显示未知，不拿生成日期兜底", async () => {
    mockApi.getSnapshotStatus.mockResolvedValue({
      scope: "cn-stock", has_ready_snapshot: true, has_building_snapshot: false,
      ready_snapshot_generated_at: "2026-10-20T03:00:00",
    });
    render(<DataPrepActions />);
    expect(await screen.findByText("opportunity.dataPrepReady · opportunity.snapshotDataDateUnknown")).toBeInTheDocument();
    expect(screen.queryByText(/opportunity.dataPrepReady.*2026-10-20/)).not.toBeInTheDocument();
  });

  it("点击前往基础数据定位设置中的行情数据页，包含尚未挂载的设置页", async () => {
    render(<DataPrepActions scope="cn-stock" />);
    await waitFor(() => expect(mockApi.getSnapshotStatus).toHaveBeenCalled());
    fireEvent.click(screen.getByText("opportunity.goToMarketData"));
    expect(mockContext.setActiveTab).toHaveBeenCalledWith("settings");
    expect(localStorage.getItem("settings_active_section")).toBe("data-center");
    expect(localStorage.getItem("settings_data_center_tab")).toBe("market");
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

  it("跟踪本次准备任务，扫描完成前保持禁用，完成后通知候选刷新", async () => {
    let finish!: (value: any) => void;
    mockApi.getAsyncTask.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const onPrepared = vi.fn();
    render(<DataPrepActions scope="cn-stock" onPrepared={onPrepared} />);
    fireEvent.click(screen.getByText("opportunity.autoScanWhenReady"));
    await waitFor(() => expect(mockApi.getAsyncTask).toHaveBeenCalledWith("task-123"));
    expect(screen.getByRole("button", { name: /opportunity.autoScanWhenReady/ })).toBeDisabled();
    expect(onPrepared).not.toHaveBeenCalled();
    finish({ id: "task-123", status: "done", percent: 100, message: "complete", result: { snapshot_id: 7, fast_scan: { status: "ok", scan_run_id: 9 } } });
    await waitFor(() => expect(onPrepared).toHaveBeenCalledWith("cn-stock", "task-123"));
    expect(onPrepared).toHaveBeenCalledTimes(1);
    expect(screen.getByText("opportunity.prepCompleted")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /opportunity.autoScanWhenReady/ })).not.toBeDisabled();
  });

  it("任务状态读取失败仅重试原任务，避免重复启动，partial完成也刷新有效扫描结果", async () => {
    mockApi.getAsyncTask.mockRejectedValueOnce(new Error("task connection lost"));
    const onPrepared = vi.fn();
    render(<DataPrepActions onPrepared={onPrepared} />);
    fireEvent.click(screen.getByText("opportunity.autoScanWhenReady"));
    expect(await screen.findByText("opportunity.prepTaskReadFailed")).toBeInTheDocument();
    expect(screen.getByText("task connection lost")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /opportunity.autoScanWhenReady/ })).toBeDisabled();
    mockApi.getAsyncTask.mockResolvedValueOnce({ id: "task-123", status: "partial", message: "optional input failed", result: { fast_scan: { status: "ok" } } });
    fireEvent.click(screen.getByRole("button", { name: "opportunity.retryTaskRead" }));
    await waitFor(() => expect(onPrepared).toHaveBeenCalledWith("cn-stock", "task-123"));
    expect(mockApi.startDataPrep).toHaveBeenCalledTimes(1);
    expect(mockApi.getAsyncTask).toHaveBeenNthCalledWith(2, "task-123");
    expect(screen.getByText("opportunity.prepPartial")).toBeInTheDocument();
  });

  it("父任务仍在运行时继续轮询到扫描终态，只通知一次刷新", async () => {
    mockApi.getAsyncTask.mockResolvedValueOnce({ id: "task-123", status: "running", percent: 92,
      message: "Task created", current_step_description: "scanning" })
      .mockResolvedValueOnce({ id: "task-123", status: "done", percent: 100, result: { fast_scan: { status: "ok" } } });
    const onPrepared = vi.fn();
    render(<DataPrepActions onPrepared={onPrepared} />);
    fireEvent.click(screen.getByText("opportunity.autoScanWhenReady"));
    expect(await screen.findByText("scanning")).toBeInTheDocument();
    expect(screen.getByText("92%")).toBeInTheDocument();
    expect(onPrepared).not.toHaveBeenCalled();
    await waitFor(() => expect(onPrepared).toHaveBeenCalledTimes(1), { timeout: 4500 });
    expect(mockApi.getAsyncTask).toHaveBeenCalledTimes(2);
  });

  it.each(["failed", "cancelled"])("本次任务%s时旧ready快照不冒充本次成功，不刷新候选", async (status) => {
    mockApi.getSnapshotStatus.mockResolvedValue({ scope: "cn-stock", has_ready_snapshot: true, ready_snapshot_generated_at: "2026-10-19", has_building_snapshot: false });
    mockApi.getAsyncTask.mockResolvedValueOnce({ id: "task-123", status, message: "scan did not complete" });
    const onPrepared = vi.fn();
    render(<DataPrepActions onPrepared={onPrepared} />);
    await screen.findByText(/opportunity.dataPrepReady/);
    fireEvent.click(screen.getByText("opportunity.autoScanWhenReady"));
    expect(await screen.findByText("scan did not complete")).toBeInTheDocument();
    expect(screen.queryByText(/opportunity.dataPrepReady/)).not.toBeInTheDocument();
    expect(onPrepared).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /opportunity.autoScanWhenReady/ })).not.toBeDisabled();
  });

  it("重新进入页面接续已运行的父任务，不重新提交", async () => {
    mockApi.getSnapshotStatus.mockResolvedValueOnce({ scope: "cn-stock", has_ready_snapshot: false, has_building_snapshot: false, last_data_prep_task_id: "recovered", last_data_prep_status: "running" });
    mockApi.getAsyncTask.mockResolvedValueOnce({ id: "recovered", status: "done", percent: 100, result: { fast_scan: { status: "ok" } } });
    const onPrepared = vi.fn();
    render(<DataPrepActions onPrepared={onPrepared} />);
    await waitFor(() => expect(onPrepared).toHaveBeenCalledWith("cn-stock", "recovered"));
    expect(mockApi.startDataPrep).not.toHaveBeenCalled();
  });

  it("重新进入页面也恢复上次partial任务的警告和可用扫描结果", async () => {
    mockApi.getSnapshotStatus.mockResolvedValue({ scope: "cn-stock", has_ready_snapshot: true, ready_snapshot_generated_at: "2026-10-19", has_building_snapshot: false, last_data_prep_task_id: "partial-old", last_data_prep_status: "partial" });
    mockApi.getAsyncTask.mockResolvedValueOnce({ id: "partial-old", status: "partial", message: "external data incomplete", result: { fast_scan: { status: "ok" } } });
    const onPrepared = vi.fn();
    render(<DataPrepActions onPrepared={onPrepared} />);
    expect(await screen.findByText("opportunity.prepPartial")).toBeInTheDocument();
    expect(screen.getByText("external data incomplete")).toBeInTheDocument();
    expect(mockApi.startDataPrep).not.toHaveBeenCalled();
  });

  it.each([true, false])("盘中数据提示未收盘，不误报数据准备失败（扫描完成=%s）", async (scanned) => {
    mockApi.getSnapshotStatus.mockResolvedValue({
      scope: "cn-stock", has_ready_snapshot: true, has_building_snapshot: false,
      ready_snapshot_trade_date: "2026-10-20", last_data_prep_task_id: "intraday-task",
      last_data_prep_status: "partial",
    });
    mockApi.getAsyncTask.mockResolvedValue({
      id: "intraday-task", status: "partial", message: "包含盘中数据（非收盘评分）", errors: [],
      result: { source_data_status: "intraday", fast_scan: scanned ? { status: "ok" } : null },
    });
    render(<DataPrepActions />);
    expect(await screen.findByText(scanned ? "opportunity.prepIntraday" : "opportunity.prepIntradayPending")).toBeInTheDocument();
    expect(screen.queryByText("opportunity.prepPartial")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunity.prepIncomplete")).not.toBeInTheDocument();
  });

  it("恢复仅准备的任务不声称扫描成功，也不通知候选刷新", async () => {
    mockApi.getSnapshotStatus.mockResolvedValue({ scope: "cn-stock", has_ready_snapshot: true, ready_snapshot_generated_at: "2026-10-19", last_data_prep_task_id: "prepare-only", last_data_prep_status: "done" });
    mockApi.getAsyncTask.mockResolvedValueOnce({ id: "prepare-only", status: "done", result: { fast_scan: null } });
    const onPrepared = vi.fn();
    render(<DataPrepActions onPrepared={onPrepared} />);
    expect(await screen.findByText("opportunity.prepScanPending")).toBeInTheDocument();
    expect(screen.queryByText("opportunity.prepCompleted")).not.toBeInTheDocument();
    expect(onPrepared).not.toHaveBeenCalled();
  });

  it("切换范围时忽略旧范围迟到的快照和准备任务结果", async () => {
    let oldStatus!: (value: any) => void;
    let oldTask!: (value: any) => void;
    mockApi.getSnapshotStatus.mockImplementationOnce(() => new Promise(resolve => { oldStatus = resolve; }));
    mockApi.getAsyncTask.mockImplementationOnce(() => new Promise(resolve => { oldTask = resolve; }));
    const onPrepared = vi.fn();
    const view = render(<DataPrepActions scope="cn-stock" onPrepared={onPrepared} />);
    fireEvent.click(screen.getByText("opportunity.autoScanWhenReady"));
    await waitFor(() => expect(mockApi.getAsyncTask).toHaveBeenCalled());
    view.rerender(<DataPrepActions scope="cn-etf" onPrepared={onPrepared} />);
    await waitFor(() => expect(mockApi.getSnapshotStatus).toHaveBeenCalledWith("cn-etf"));
    await act(async () => {
      oldStatus({ has_ready_snapshot: true, ready_snapshot_generated_at: "1999-01-01" });
      oldTask({ id: "task-123", status: "done", message: "old scope complete", result: { fast_scan: { status: "ok" } } });
    });
    expect(screen.queryByText(/1999-01-01/)).not.toBeInTheDocument();
    expect(screen.queryByText("old scope complete")).not.toBeInTheDocument();
    expect(onPrepared).not.toHaveBeenCalled();
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
