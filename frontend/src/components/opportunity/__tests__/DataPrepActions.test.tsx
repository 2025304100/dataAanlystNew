import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const { mockContext, mockApi } = vi.hoisted(() => ({
  mockContext: {
    setActiveTab: vi.fn(),
    runSync: vi.fn(),
    showToast: vi.fn(),
  },
  mockApi: {
    startDataPrep: vi.fn(),
    getSnapshotStatus: vi.fn(),
  },
}));

vi.mock("../../../i18n", () => ({ t: (key: string) => key }));
vi.mock("../../../context/AppContext", () => ({ useApp: () => mockContext }));
vi.mock("../../../api/client", () => ({ api: mockApi }));

import { DataPrepActions } from "../DataPrepActions";

describe("DataPrepActions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
});
