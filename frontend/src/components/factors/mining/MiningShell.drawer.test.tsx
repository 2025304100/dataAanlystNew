import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, waitFor } from "@testing-library/react";

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

const { listRuns, listDrafts, getRun, listCandidates, batchReview, listFactorSets } = vi.hoisted(() => ({
  listRuns: vi.fn(),
  listDrafts: vi.fn(),
  getRun: vi.fn(),
  listCandidates: vi.fn(),
  batchReview: vi.fn(),
  listFactorSets: vi.fn(),
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
    listCandidates,
    batchReview,
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
  api: { listFactorSets },
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
  listCandidates.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 });
  listFactorSets.mockResolvedValue([{ id: "set-draft", name: "研究集合", status: "draft" }]);
  batchReview.mockResolvedValue({ results: [{ candidate_id: "candidate-a", action: "add_to_set", status: "added" }], counts: { added: 1, failed: 0 } });
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

  it("完成任务的结果页没有仍可点击的中断、继续、停止或放弃操作", async () => {
    render(<MiningShell />);
    await waitFor(() => expect(document.querySelector("[data-mining-run-view='run-done']")).toBeTruthy());
    fireEvent.click(document.querySelector("[data-mining-run-view='run-done']") as HTMLElement);
    await waitFor(() => expect(document.querySelector("[data-run-track]")).toBeTruthy());
    expect(document.querySelector("[data-run-pause]")).toBeNull();
    expect(document.querySelector("[data-run-resume]")).toBeNull();
    expect(document.querySelector("[data-run-stop]")).toBeNull();
    expect(document.querySelector("[data-run-discard]")).toBeNull();
  });

  it.each([
    ["succeeded", "已完成"], ["converged", "已收敛"],
    ["failed", "失败"], ["cancelled", "已取消"], ["discarded", "已放弃"],
  ])("历史 %s 任务的当前步骤和详情顶部显示真实终态", async (status, label) => {
    listRuns.mockResolvedValue({ items: [{ ...RUNS[0], status }], total: 1, page: 1, page_size: 20 });
    getRun.mockResolvedValue({ id: "run-done", status, current_generation: 0, total_generations: 1, selected_fields: [] });
    render(<MiningShell />);
    await waitFor(() => expect(document.querySelector("[data-mining-run-view='run-done']")).toBeTruthy());
    fireEvent.click(document.querySelector("[data-mining-run-view='run-done']") as HTMLElement);
    await waitFor(() => expect(document.querySelector("[data-run-track]")).toBeTruthy());
    expect(document.querySelector('[data-mining-step="run"] .mining-step-state')?.textContent).toBe(label);
    expect(document.querySelector('[data-run-converged]')?.textContent).toContain(label);
    expect(document.querySelector('[data-run-converged]')?.textContent).not.toContain("未收敛");
  });

  it("历史任务未保存字段校验信息时说明记录缺失，并提供复制草稿入口", async () => {
    getRun.mockResolvedValue({ id: "run-done", status: "succeeded", current_generation: 0, total_generations: 1, selected_fields: [] });
    render(<MiningShell />);
    await waitFor(() => expect(document.querySelector("[data-mining-run-view='run-done']")).toBeTruthy());
    fireEvent.click(document.querySelector("[data-mining-run-view='run-done']") as HTMLElement);
    await waitFor(() => expect(document.querySelector('[data-mining-step="field"] .mining-step-state')?.textContent).toBe("历史记录缺少校验信息"));
    fireEvent.click(document.querySelector('[data-mining-step-jump="field"]') as HTMLElement);
    expect(document.querySelector('[data-mining-history-validation-missing]')?.textContent).toContain("复制为草稿");
    expect(document.querySelector('[data-field-validate]')).toBeNull();
    expect(document.querySelector('[data-mining-drawer-copy]')).toBeTruthy();
  });

  it("结果候选接口失败时说明加载失败，允许重试而非假装没有候选", async () => {
    listCandidates.mockRejectedValueOnce(new Error("候选服务暂不可用"));
    render(<MiningShell />);
    await waitFor(() => expect(document.querySelector("[data-mining-run-view='run-done']")).toBeTruthy());
    fireEvent.click(document.querySelector("[data-mining-run-view='run-done']") as HTMLElement);
    await waitFor(() => expect(document.querySelector("[data-mining-result-error]")).toBeTruthy());
    expect(document.querySelector("[data-mining-result-empty]")).toBeNull();
    fireEvent.click(document.querySelector("[data-mining-result-retry]") as HTMLElement);
    await waitFor(() => expect(listCandidates).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(document.querySelector("[data-mining-result-empty]")).toBeTruthy());
  });

  it("关闭后重新查看同一成功任务，会重新读取并展示候选结果", async () => {
    listCandidates.mockResolvedValue({ items: [{ id: "candidate-a", run_id: "run-done", canonical_formula: "mean(close, 5)", grade: "A", generation_icir: 0.4 }], total: 1, page: 1, page_size: 50 });
    render(<MiningShell />);
    await waitFor(() => expect(document.querySelector("[data-mining-run-view='run-done']")).toBeTruthy());
    fireEvent.click(document.querySelector("[data-mining-run-view='run-done']") as HTMLElement);
    await waitFor(() => expect(document.querySelector('[data-result-rows]')?.textContent).toContain("mean(close, 5)"));
    fireEvent.click(document.querySelector('.ant-drawer-close') as HTMLElement);
    await waitFor(() => expect(document.querySelector('.ant-drawer-open')).toBeNull());
    await waitFor(() => expect(document.querySelector("[data-mining-run-view='run-done']")).toBeTruthy());
    fireEvent.click(document.querySelector("[data-mining-run-view='run-done']") as HTMLElement);
    await waitFor(() => expect(listCandidates).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(document.querySelector('[data-result-rows]')?.textContent).toContain("mean(close, 5)"));
  });

  it("选择真实候选加入草稿集合时发起公开批量入组 API，并反馈加入结果", async () => {
    listCandidates.mockResolvedValue({ items: [{ id: "candidate-a", run_id: "run-done", factor_version_id: 9, canonical_formula: "mean(close, 5)", grade: "A", generation_icir: 0.4 }], total: 1, page: 1, page_size: 50 });
    render(<MiningShell />);
    await waitFor(() => expect(document.querySelector("[data-mining-run-view='run-done']")).toBeTruthy());
    fireEvent.click(document.querySelector("[data-mining-run-view='run-done']") as HTMLElement);
    await waitFor(() => expect(document.querySelector('[data-result-checkbox="candidate-a"]')).toBeTruthy());
    fireEvent.click(document.querySelector('[data-result-checkbox="candidate-a"]') as HTMLElement);
    fireEvent.click(document.querySelector('[data-result-batch-add]') as HTMLElement);
    await waitFor(() => expect(document.querySelector('[data-mining-set-confirm]')).toBeTruthy());
    fireEvent.click(document.querySelector('[data-mining-set-confirm]') as HTMLElement);
    await waitFor(() => expect(batchReview).toHaveBeenCalledWith([{ candidate_id: "candidate-a", action: "add_to_set", factor_set_id: "set-draft" }]));
    await waitFor(() => expect(document.querySelector('[data-mining-set-feedback]')?.textContent).toContain("1"));
  });

  it("最终IC不会被冒充校正ICIR，接口没有校正值时显示缺失", async () => {
    listCandidates.mockResolvedValue({ items: [{ id: "candidate-a", run_id: "run-done", factor_version_id: 9, canonical_formula: "mean(close, 5)", grade: "A", generation_icir: 0.4, latest_ic: 0.9137 }], total: 1, page: 1, page_size: 50 });
    render(<MiningShell />);
    await waitFor(() => expect(document.querySelector("[data-mining-run-view='run-done']")).toBeTruthy());
    fireEvent.click(document.querySelector("[data-mining-run-view='run-done']") as HTMLElement);
    await waitFor(() => expect(document.querySelector('[data-result-rows]')).toBeTruthy());
    const cells = document.querySelector('[data-result-rows] tr')?.querySelectorAll('td');
    expect(cells?.[3].textContent).toBe("0.400");
    expect(cells?.[4].textContent).toBe("-");
  });

  it("从批次列表查看运行中任务后，代数会持续刷新而非停留在列表快照", async () => {
    getRun.mockResolvedValue({ ...RUNS[1], current_generation: 3, total_generations: 20 });
    render(<MiningShell />);
    await waitFor(() => expect(document.querySelector("[data-mining-run-view='run-live']")).toBeTruthy());
    vi.useFakeTimers();
    try {
      fireEvent.click(document.querySelector("[data-mining-run-view='run-live']") as HTMLElement);
      await act(async () => { await Promise.resolve(); });
      getRun.mockResolvedValue({ ...RUNS[1], current_generation: 4, total_generations: 20 });
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(document.querySelector('[data-run-generation]')?.textContent).toContain("4 / 20");
    } finally {
      vi.useRealTimers();
    }
  });

  it("查看旧任务尚未加载完成时新建任务，迟到的旧配置不能污染新任务", async () => {
    const resolvers: Array<(detail: unknown) => void> = [];
    getRun.mockImplementation(() => new Promise((resolve) => { resolvers.push(resolve); }));
    render(<MiningShell />);
    await waitFor(() => expect(document.querySelector("[data-mining-run-view='run-done']")).toBeTruthy());
    fireEvent.click(document.querySelector("[data-mining-run-view='run-done']") as HTMLElement);
    fireEvent.click(document.querySelector("[data-mining-new-task]") as HTMLElement);
    await act(async () => { resolvers.forEach((resolve) => resolve({ id: "run-done", status: "succeeded", start_date: "2025-03-03", end_date: "2025-08-08", data_cutoff_at: "2025-08-08" })); });
    fireEvent.click(document.querySelector('[data-mining-step-jump="time-target"]') as HTMLElement);
    const start = (document.querySelector('[data-time-start] input') ?? document.querySelector('[data-time-start]')) as HTMLInputElement;
    expect(start.value).toBe("");
  });

  it("批次超过一页时能查看后续历史批次", async () => {
    listRuns.mockImplementation(async ({ page }: { page: number }) => ({ items: page === 1 ? RUNS : [{ ...RUNS[0], id: "older-run" }], total: 21, page, page_size: 20 }));
    render(<MiningShell />);
    await waitFor(() => expect(document.querySelector('[data-mining-runs-next]')).toBeTruthy());
    fireEvent.click(document.querySelector('[data-mining-runs-next]') as HTMLElement);
    await waitFor(() => expect(listRuns).toHaveBeenCalledWith({ page: 2, page_size: 20 }));
    await waitFor(() => expect(document.querySelector('[data-mining-run-view="older-run"]')).toBeTruthy());
  });
});
