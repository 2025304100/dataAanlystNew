import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, waitFor } from "@testing-library/react";

/**
 * 阶段重构回归：列表优先 + 抽屉向导 + 任务生命周期 + 复制为草稿。
 *
 * 覆盖用户诉求链路：
 *   1. 默认落在「批次列表」，带常驻「新建挖掘」入口；草稿作为一行可继续编辑/删除；
 *   2. 点「新建挖掘」→ 右侧抽屉以**编辑态**打开（无「已锁定」徽标）；
 *   3. 点历史任务「查看」→ 抽屉以**只读态**打开（出现「已锁定·只读」徽标）；
 *   4. 点「复制为草稿」→ 拉取该任务配置、以**编辑态**打开（无锁定徽标）。
 *
 * 说明：抽屉用 forceRender，向导内容始终在 DOM；开合与模式由状态驱动，
 * 用 `.ant-drawer-open` 与 `[data-mining-drawer-locked]` 断言。
 */

const { listRuns, listDrafts, getRun } = vi.hoisted(() => ({
  listRuns: vi.fn(),
  listDrafts: vi.fn(),
  getRun: vi.fn(),
}));

vi.mock("../../../api/factorMining", () => ({
  factorMiningApi: {
    listRuns,
    listMiningFields: vi.fn(async () => ({ fields: [] })),
    getRun,
    listGenerations: vi.fn(async () => []),
    createRun: vi.fn(async () => ({ run_id: "new-run", eta_seconds: null })),
    resumeRun: vi.fn(async () => ({})),
    pauseRun: vi.fn(async () => ({})),
    stopRun: vi.fn(async () => ({})),
    discardRun: vi.fn(async () => ({})),
    cancelRun: vi.fn(async () => ({})),
    listCandidates: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    getLockStatus: vi.fn(async () => ({
      miningDomain: { busy: false, taskId: null, runId: null, acquiredAt: null, heartbeatAt: null },
      duckdbWrite: { busy: false, taskId: null, queue: [] },
    })),
    computeSplitBudget: vi.fn(async () => ({ available: false })),
    saveDraft: vi.fn(async () => ({ draft_id: "d-new" })),
    listDrafts,
    deleteDraft: vi.fn(async () => ({ deleted: true })),
  },
}));

vi.mock("../../../api/dataMirror", () => ({
  dataMirrorApi: { getStatus: vi.fn(async () => ({ available: false })) },
}));

vi.mock("../../../api/client", () => ({
  api: {},
  requestJson: vi.fn(async () => ({})),
}));

vi.mock("./step1/poolApi", () => ({
  MIN_POOL_SIZE: 50,
  previewPool: vi.fn(async () => ({
    universe_size: 5000, hits: 800, excluded_total: 4200, can_generate: true,
    min_pool_size: 50, as_of_date: "2026-08-21", categories: [], samples: [],
    field_coverage: {}, warnings: [],
  })),
  createPoolFromFilter: vi.fn(async () => ({ id: "pool-1", name: "p", member_count: 800 })),
  createSnapshot: vi.fn(async () => ({
    snapshot_id: "snap-1", pool_id: "pool-1", analysis_status: "ready",
    is_locked: true, data_cutoff_at: "2026-08-21", analysis: {}, pool: {},
  })),
  fetchLatestSnapshot: vi.fn(async () => null),
  deleteLatestSnapshot: vi.fn(async () => ({})),
  fetchMembers: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
  removeMembers: vi.fn(async () => ({})),
  createPool: vi.fn(async () => ({ id: "pool-1" })),
  fetchFilterFields: vi.fn(async () => ({ fields: [], categories: [], min_pool_size: 50 })),
  fetchFilterPresets: vi.fn(async () => ({ as_of_date: "2026-08-21", window_days: 20, groups: [], presets: [] })),
  previewImport: vi.fn(),
  importPoolMembers: vi.fn(),
  downloadImportTemplate: vi.fn(),
  exportImportErrors: vi.fn(),
}));

vi.mock("./wizard/step3/fieldApi", () => ({
  createValidation: vi.fn(),
  getValidation: vi.fn(),
  normalizeValidation: vi.fn(),
}));

import MiningShell from "./MiningShell";

const RUNS = [
  {
    id: "run-done", status: "succeeded", created_at: "2026-09-20T10:00:00Z",
    current_generation: 20, max_generation: 20, start_date: "2025-01-01",
    end_date: "2026-09-01", rebalance_frequency: "daily",
  },
  {
    id: "run-live", status: "running", created_at: "2026-09-21T10:00:00Z",
    current_generation: 3, max_generation: 20, start_date: "2025-01-01",
    end_date: "2026-09-01", rebalance_frequency: "weekly",
  },
];
const DRAFTS = [
  {
    draft_id: "draft-1", name: "我的草稿", current_step: 2,
    candidate_pool_snapshot_id: "snap-d", updated_at: "2026-09-22T10:00:00Z",
    steps: { step1: { candidate_pool_snapshot_id: "snap-d", pool_id: "pool-d" } },
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  listRuns.mockResolvedValue({ items: RUNS, total: RUNS.length, page: 1, page_size: 20 });
  listDrafts.mockResolvedValue(DRAFTS);
  getRun.mockResolvedValue({
    id: "run-done", status: "succeeded", candidate_pool_snapshot_id: "snap-d",
    data_cutoff_at: "2026-09-01", start_date: "2025-01-01", end_date: "2026-09-01",
    rebalance_frequency: "daily", target_horizon: 5,
    selected_fields: ["close"], evolution_params: { population_size: 100 },
  });
});

describe("因子挖掘：列表优先 + 抽屉向导 + 生命周期", () => {
  it("默认落在批次列表：有新建入口、run 行、草稿行", async () => {
    render(<MiningShell />);
    await waitFor(() => expect(listRuns).toHaveBeenCalled());
    await waitFor(() => expect(listDrafts).toHaveBeenCalled());
    expect(document.querySelector("[data-mining-new-task]")).toBeTruthy();
    expect(document.querySelector("[data-mining-run-view='run-done']")).toBeTruthy();
    // 草稿作为一等公民：出现「继续编辑」行
    expect(document.querySelector("[data-mining-task-edit='draft-1']")).toBeTruthy();
  });

  it("点「新建挖掘」→ 抽屉编辑态打开（无锁定徽标）", async () => {
    render(<MiningShell />);
    await waitFor(() => expect(document.querySelector("[data-mining-new-task]")).toBeTruthy());
    fireEvent.click(document.querySelector("[data-mining-new-task]") as HTMLElement);
    await waitFor(() => expect(document.querySelector(".ant-drawer-open")).toBeTruthy());
    expect(document.querySelector("[data-mining-drawer-locked]")).toBeNull();
  });

  it("点历史任务「查看」→ 抽屉只读态打开（出现锁定徽标）", async () => {
    render(<MiningShell />);
    await waitFor(() => expect(document.querySelector("[data-mining-run-view='run-done']")).toBeTruthy());
    fireEvent.click(document.querySelector("[data-mining-run-view='run-done']") as HTMLElement);
    await waitFor(() => expect(document.querySelector(".ant-drawer-open")).toBeTruthy());
    await waitFor(() => expect(document.querySelector("[data-mining-drawer-locked]")).toBeTruthy());
    // 查看会拉取完整配置用于回显
    expect(getRun).toHaveBeenCalledWith("run-done");
  });

  it("点「复制为草稿」→ 拉配置并以编辑态打开（无锁定徽标）", async () => {
    render(<MiningShell />);
    await waitFor(() => expect(document.querySelector("[data-mining-run-copy='run-done']")).toBeTruthy());
    // 先切到只读，再复制：验证复制会把模式切回编辑
    fireEvent.click(document.querySelector("[data-mining-run-view='run-done']") as HTMLElement);
    await waitFor(() => expect(document.querySelector("[data-mining-drawer-locked]")).toBeTruthy());
    fireEvent.click(document.querySelector("[data-mining-run-copy='run-done']") as HTMLElement);
    await waitFor(() => expect(document.querySelector("[data-mining-drawer-locked]")).toBeNull());
    expect(getRun).toHaveBeenCalledWith("run-done");
  });

  it("运行中任务给「放弃」，终态任务不给「放弃」", async () => {
    render(<MiningShell />);
    await waitFor(() => expect(document.querySelector("[data-mining-run-view='run-done']")).toBeTruthy());
    // run-live（running）有放弃；run-done（succeeded 终态）无放弃
    expect(document.querySelector("[data-mining-run-discard='run-live']")).toBeTruthy();
    expect(document.querySelector("[data-mining-run-discard='run-done']")).toBeNull();
  });
});
