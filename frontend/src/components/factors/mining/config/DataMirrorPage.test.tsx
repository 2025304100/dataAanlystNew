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
import { ApiError } from "../../../../api/client";
import DataMirrorPage, { humanBytes, humanSeconds } from "./DataMirrorPage";

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

  it("创建成功 → 展示估算块（行数/耗时/占用）且显式标注「估算」", async () => {
    vi.mocked(dataMirrorApi.createTask).mockResolvedValueOnce({
      task_id: "t-new", status: "queued", preset: "5y",
      estimated: {
        estimated: true,
        estimated_rows: 1_234_567,
        estimated_symbols: 5200,
        estimated_seconds: 411.5,
        estimated_bytes: 4_800_000_000,
        note_zh: "以上均为**估算值**（规则：源表 COUNT ÷ 吞吐假设），实际以任务完成后展示为准。",
      },
    });
    render(<DataMirrorPage />);
    await waitFor(() => {
      expect(document.querySelector("[data-mirror-create]")).toBeTruthy();
    });
    fireEvent.click(document.querySelector("[data-mirror-create]") as HTMLElement);

    await waitFor(() => {
      expect(document.querySelector("[data-mirror-create-info]")).toBeTruthy();
    });
    const info = document.querySelector("[data-mirror-create-info]") as HTMLElement;
    // 估算标记必须可见（设计 §10.1：估算值须标注）
    expect(info.querySelector("[data-mirror-estimate-tag]")!.textContent).toBe("估算");
    expect(info.querySelector("[data-mirror-est-rows]")!.textContent).toContain("1,234,567");
    expect(info.querySelector("[data-mirror-est-seconds]")!.textContent).toContain("6.9 分钟");
    expect(info.querySelector("[data-mirror-est-bytes]")!.textContent).toContain("4.5 GB");
    // note_zh 的 markdown 强调符必须剥掉，不能把 ** 甩上屏
    expect(info.textContent).not.toContain("**");
    expect(info.textContent).toContain("估算值");
  });

  it("创建失败 → 不再静默：展示后端中文原因（磁盘余量不足）", async () => {
    vi.mocked(dataMirrorApi.createTask).mockRejectedValueOnce(
      new ApiError("boom", {
        status_code: 400,
        detail: {
          error_code: "VALIDATION_ERROR",
          detail_zh: "磁盘剩余 2.0 GB，预估需要 5.0 GB（含 1.5x 安全余量），不足以完成本次镜像。",
        },
      }),
    );
    render(<DataMirrorPage />);
    await waitFor(() => {
      expect(document.querySelector("[data-mirror-create]")).toBeTruthy();
    });
    fireEvent.click(document.querySelector("[data-mirror-create]") as HTMLElement);

    await waitFor(() => {
      expect(document.querySelector("[data-mirror-create-error]")).toBeTruthy();
    });
    const box = document.querySelector("[data-mirror-create-error]") as HTMLElement;
    expect(box.textContent).toContain("磁盘剩余 2.0 GB");
    expect(box.textContent).toContain("不足以完成本次镜像");
    // 失败时不得残留上一次的成功估算块
    expect(document.querySelector("[data-mirror-create-info]")).toBeNull();
  });

  it("humanBytes / humanSeconds：边界与非法值", () => {
    expect(humanBytes(512)).toBe("512 B");
    expect(humanBytes(2048)).toBe("2.0 KB");
    expect(humanBytes(4_800_000_000)).toBe("4.5 GB");
    expect(humanBytes(null)).toBe("-");
    expect(humanBytes(Number.NaN)).toBe("-");

    expect(humanSeconds(41.5)).toBe("41.5 秒");
    expect(humanSeconds(411.5)).toBe("6.9 分钟");
    expect(humanSeconds(7200)).toBe("2.0 小时");
    expect(humanSeconds(null)).toBe("-");
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