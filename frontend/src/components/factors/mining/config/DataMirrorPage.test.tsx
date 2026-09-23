import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, waitFor } from "@testing-library/react";

/**
 * C2：数据镜像管理页（列表渲染 + 创建/取消操作）。
 */
vi.mock("../../../../api/dataMirror", () => ({
  dataMirrorApi: {
    getStatus: vi.fn(async () => ({
      available: true,
      total_rows: 123456,
      total_symbols: 5200,
      mirrored_from: "2020-01-01",
      mirrored_to: "2026-09-18",
      low_coverage_years: [],
    })),
    getTasks: vi.fn(async () => ({
      items: [
        { task_id: "t-1", status: "running", preset: "5y", completed_chunks: 3, total_chunks: 10 },
        { task_id: "t-2", status: "done", preset: "full", completed_chunks: 4, total_chunks: 4 },
      ],
    })),
    createTask: vi.fn(async () => ({ task_id: "t-new", status: "queued", preset: "5y" })),
    cancelTask: vi.fn(async () => ({ task_id: "t-1", status: "cancelled" })),
  },
}));

import { dataMirrorApi } from "../../../../api/dataMirror";
import DataMirrorPage from "./DataMirrorPage";

describe("DataMirrorPage（C2）", () => {
  it("渲染状态与任务列表", async () => {
    render(<DataMirrorPage />);
    await waitFor(() => {
      expect(document.querySelector("[data-mirror-status]")).toBeTruthy();
    });
    expect(document.querySelector('[data-mirror-status]')?.getAttribute("data-available")).toBe("true");
    const rows = document.querySelectorAll("[data-mirror-task-table] tbody tr");
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain("t-1");
    expect(rows[0].textContent).toContain("5y");
    // running 任务有取消按钮，done 任务无
    expect(document.querySelector("[data-mirror-cancel='t-1']")).toBeTruthy();
    expect(document.querySelector("[data-mirror-cancel='t-2']")).toBeNull();
  });

  it("点击创建 → 调 createTask 并刷新", async () => {
    render(<DataMirrorPage />);
    await waitFor(() => {
      expect(document.querySelector("[data-mirror-create]")).toBeTruthy();
    });
    fireEvent.click(document.querySelector("[data-mirror-create]") as HTMLElement);
    await waitFor(() => {
      expect(dataMirrorApi.createTask).toHaveBeenCalledWith({ preset: "5y" });
    });
  });

  it("点击取消 → 调 cancelTask", async () => {
    render(<DataMirrorPage />);
    await waitFor(() => {
      expect(document.querySelector("[data-mirror-cancel='t-1']")).toBeTruthy();
    });
    fireEvent.click(document.querySelector("[data-mirror-cancel='t-1']") as HTMLElement);
    await waitFor(() => {
      expect(dataMirrorApi.cancelTask).toHaveBeenCalledWith("t-1");
    });
  });

  it("接口异常 → 错误态（不崩页）", async () => {
    vi.mocked(dataMirrorApi.getStatus).mockRejectedValueOnce(new Error("boom"));
    vi.mocked(dataMirrorApi.getTasks).mockRejectedValueOnce(new Error("boom"));
    render(<DataMirrorPage />);
    await waitFor(() => {
      expect(document.querySelector("[data-mirror-error]")).toBeTruthy();
    });
  });

  it("P1-2：数仓不可用时中文兜底 + 错误折叠 + 禁用创建按钮", async () => {
    vi.mocked(dataMirrorApi.getStatus).mockResolvedValueOnce({
      available: false,
      error: "Connection Error: Can't open a connection to same database file...",
      warehouse_path: "d:/wh",
    });
    render(<DataMirrorPage />);
    await waitFor(() => {
      expect(document.querySelector("[data-mirror-status]")).toBeTruthy();
    });
    // 不可用 → 展示中文兜底文案，而非英文底层错误
    expect(document.querySelector('[data-mirror-status]')?.getAttribute("data-available")).toBe("false");
    expect(document.querySelector("[data-mirror-unavailable-hint]")).toBeTruthy();
    // 错误详情折叠存在（未展开时不直接展示底层错误文本）
    expect(document.querySelector("[data-mirror-error-detail]")).toBeTruthy();
    expect(document.querySelector("[data-mirror-unavailable-hint]")?.textContent).not.toContain("Connection Error");
    // 底层错误文本只出现在折叠的 <details> 内
    expect(document.querySelector("[data-mirror-error-detail] pre")?.textContent).toContain("Connection Error");
    // 创建按钮禁用
    expect((document.querySelector("[data-mirror-create]") as HTMLButtonElement).disabled).toBe(true);
  });
});