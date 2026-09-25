import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, waitFor } from "@testing-library/react";

/**
 * G5+A4 验收测试：「5 步向导可点通到结果页」+「提交接线」（A4，2026-09-20）。
 *
 * G5 覆盖：默认第 1 步 / 「下一步」逐级推进 / 步骤条点击跳转 /
 * 第 5 步空态（不臆造进度）与有数据时的进化跟踪。
 *
 * A4 覆盖（提交回调断言）：
 *   1. 缺少候选池快照时提交 → 不调 createRun，显示提交错误提示；
 *   2. step1 生成快照 → step4 提交确认 → createRun 带快照 id 调用，
 *      成功后自动流转第 5 步渲染进化跟踪（空态消失）；
 *   3. createRun 失败 → 停留在原步并显示提交错误提示。
 */
vi.mock("../../../../api/factorMining", () => ({
  factorMiningApi: {
    listRuns: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    // P0-2 修复同步：MiningShell 进入 Step3 时拉取 DSL 字段目录（GET /factor-mining/fields）
    listMiningFields: vi.fn(async () => ({
      fields: [
        { field: "close", label_zh: "收盘价", group: "quote", group_label_zh: "行情", availability: "available" },
        { field: "pe_ttm", label_zh: "市盈率TTM", group: "valuation", group_label_zh: "估值", availability: "available" },
        { field: "roe_ttm", label_zh: "ROE(TTM)", group: "financial", group_label_zh: "财报", availability: "available" },
      ],
    })),
    getRun: vi.fn(async (id: string) => ({
      id, status: "running", current_generation: 2, total_generations: 20,
    })),
    createRun: vi.fn(),
    getLockStatus: vi.fn(async () => ({
      miningDomain: { busy: false, taskId: null, runId: null, acquiredAt: null, heartbeatAt: null },
      duckdbWrite: { busy: false, taskId: null, queue: [] },
    })),
    computeSplitBudget: vi.fn(async () => ({
      available: true, frequency: "daily", total_points: 252, train_points: 151,
      val_points: 50, test_points: 50, purge_points: 5, embargo_points: 5,
      purge_trading_days: 5, embargo_trading_days: 5, tail_loss: 5,
      frequency_floor: 252, meets_floor: true, statistically_degraded: false,
      train_start: "2025-01-01", train_end: "2025-12-31", val_end: "2026-03-31",
    })),
    listCandidates: vi.fn(async (runId: string) => ({
      items: [
        { id: "cand-1", run_id: runId, generation: 5, operation: "mutation",
          canonical_formula: "mean(close,5)/mean(close,20)-1", generation_icir: 0.32 },
        { id: "cand-2", run_id: runId, generation: 9, operation: "elite",
          canonical_formula: "rank(close,10)", generation_icir: 0.41 },
      ],
      total: 2, page: 1, page_size: 50,
    })),
    pauseRun: vi.fn(async () => ({})),
    resumeRun: vi.fn(async () => ({})),
    stopRun: vi.fn(async () => ({})),
    discardRun: vi.fn(async () => ({})),
  },
}));

vi.mock("../../../../api/dataMirror", () => ({
  dataMirrorApi: {
    getStatus: vi.fn(async () => ({
      available: true, mirrored_from: "2021-01-01", mirrored_to: "2026-09-07",
      total_rows: 8090000, total_symbols: 7270,
    })),
  },
}));

vi.mock("../../../../api/client", () => ({
  api: { scoringGetOverviewAsFactor: vi.fn(async () => ({})) },
  requestJson: vi.fn(async () => ({})),
}));

vi.mock("./step1/poolApi", () => ({
  MIN_POOL_SIZE: 50,
  previewPool: vi.fn(async () => ({
    universe_size: 5000, hits: 300, excluded_total: 4700, min_pool_size: 50,
    can_generate: true, by_rule: [], warnings: [], blocking_issues: [],
  })),
  createPoolFromFilter: vi.fn(async () => ({ id: "pool-1", name: "p", source_type: "filter" })),
  createPool: vi.fn(async () => ({ id: "pool-1", name: "p", source_type: "import" })),
  createSnapshot: vi.fn(async () => ({
    id: "snap-1", pool_id: "pool-1", is_locked: true,
    analyzed_at: "2026-09-19T00:00:00Z", as_of_date: "2026-09-18",
  })),
  deleteLatestSnapshot: vi.fn(),
  fetchMembers: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
  fetchFilterPresets: vi.fn(async () => ({ as_of_date: null, window_days: 20, groups: [], presets: [], warnings: [] })),
  fetchFilterFields: vi.fn(async () => ({ fields: [], categories: [], min_pool_size: 50 })),
  fetchLatestSnapshot: vi.fn(async () => null),
  downloadImportTemplate: vi.fn(),
  previewImport: vi.fn(),
  exportImportErrors: vi.fn(),
  importPoolMembers: vi.fn(),
}));

import { factorMiningApi } from "../../../../api/factorMining";
import MiningShell from "../MiningShell";

const PANELS = ["pool", "time-target", "field", "evolution", "run"];

/** 生成快照并推进到第 4 步（evolution，含提交按钮） */
async function reachSubmitStep() {
  render(<MiningShell />);
  // DEF-4：生成按钮在预览定型前置灰 —— 先等 300ms 防抖预览落地
  await waitFor(() => {
    expect(document.querySelector("[data-pool-preview-stats]")).toBeTruthy();
  });
  // step1 生成挖掘物料（mock 返回 snap-1，is_locked=true）
  fireEvent.click(document.querySelector("[data-pool-generate]") as HTMLElement);
  await waitFor(() => {
    expect(document.querySelector("[data-pool-lock-banner]")).toBeTruthy();
  });
  // 1→2→3→4 推进 3 次
  for (let i = 0; i < 3; i += 1) {
    fireEvent.click(document.querySelector("[data-mining-next]") as HTMLElement);
  }
}

describe("G5：5 步向导可点通", () => {
  it("默认第 1 步，步骤条含全部 5 步", () => {
    render(<MiningShell />);
    expect(document.querySelector("[data-mining-step-bar]")).toBeTruthy();
    expect(document.querySelectorAll("[data-mining-step]").length).toBe(5);
    expect(document.querySelector('[data-mining-step-panel="pool"]')).toBeTruthy();
  });

  it("连续点「下一步」可依次到达 5 个步骤面板", () => {
    render(<MiningShell />);
    const next = () => document.querySelector("[data-mining-next]") as HTMLElement;
    for (let i = 0; i < PANELS.length; i += 1) {
      expect(
        document.querySelector(`[data-mining-step-panel="${PANELS[i]}"]`),
      ).toBeTruthy();
      if (i < PANELS.length - 1) fireEvent.click(next());
    }
    // 到达第 5 步后「下一步」禁用
    expect((next() as HTMLButtonElement).disabled).toBe(true);
  });

  it("第 1 步「上一步」禁用", () => {
    render(<MiningShell />);
    expect(
      (document.querySelector("[data-mining-prev]") as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("步骤条可点击跳转", () => {
    render(<MiningShell />);
    fireEvent.click(document.querySelector('[data-mining-step-jump="field"]') as HTMLElement);
    expect(document.querySelector('[data-mining-step-panel="field"]')).toBeTruthy();
    expect(document.querySelector('[data-mining-step-panel="pool"]')).toBeNull();
  });

  it("第 5 步无运行数据时显示空态（不臆造进度）", () => {
    render(<MiningShell />);
    fireEvent.click(document.querySelector('[data-mining-step-jump="run"]') as HTMLElement);
    expect(document.querySelector("[data-mining-run-empty]")).toBeTruthy();
    expect(document.querySelector("[data-run-track]")).toBeNull();
  });

  it("第 5 步有运行数据时渲染进化跟踪", () => {
    render(
      <MiningShell
        runProgress={{
          run_id: "run-1", status: "running", generation: 3, max_generations: 20,
          convergence_threshold: 0.01, converged: false, diversity: 0.5,
          final_validation: null, curve: [],
        }}
      />,
    );
    fireEvent.click(document.querySelector('[data-mining-step-jump="run"]') as HTMLElement);
    expect(document.querySelector("[data-run-track]")).toBeTruthy();
    expect(document.querySelector("[data-mining-run-empty]")).toBeNull();
  });
});

describe("A4：提交接线", () => {
  beforeEach(() => {
    // 测试隔离：提交成功后会把 run 写入「最近会话」（会话恢复用），
    // 不清会跨用例污染——后续 mount 的恢复 effect 会恢复上一用例的 run
    window.localStorage.clear();
    vi.clearAllMocks();
  });

  it("缺少候选池快照时提交 → 不调 createRun，显示提交错误", async () => {
    render(<MiningShell />);
    // 直接跳到第 4 步（不进 step1 生成快照）
    fireEvent.click(document.querySelector('[data-mining-step-jump="evolution"]') as HTMLElement);
    fireEvent.click(document.querySelector("[data-evo-submit]") as HTMLElement);
    expect(document.querySelector("[data-resource-modal]")).toBeTruthy();
    fireEvent.click(document.querySelector("[data-evo-confirm-submit]") as HTMLElement);
    await waitFor(() => {
      expect(document.querySelector("[data-mining-submit-error]")).toBeTruthy();
    });
    expect(factorMiningApi.createRun).not.toHaveBeenCalled();
  });

  it("生成快照后提交 → createRun 带快照 id，第 5 步渲染进化跟踪", async () => {
    vi.mocked(factorMiningApi.createRun).mockResolvedValueOnce({
      run_id: "run-1", task_id: "task-1", queue_position: 0,
      eta_seconds: 30, split_budget: null,
    });
    await reachSubmitStep();
    expect(document.querySelector('[data-mining-step-panel="evolution"]')).toBeTruthy();
    // 第 4 步提交 → 资源确认弹窗 → 确认
    fireEvent.click(document.querySelector("[data-evo-submit]") as HTMLElement);
    expect(document.querySelector("[data-resource-modal]")).toBeTruthy();
    fireEvent.click(document.querySelector("[data-evo-confirm-submit]") as HTMLElement);
    await waitFor(() => {
      expect(factorMiningApi.createRun).toHaveBeenCalled();
    });
    // payload 契约：快照 id / 频率 / 进化参数
    expect(factorMiningApi.createRun).toHaveBeenCalledWith(
      expect.objectContaining({
        candidate_pool_snapshot_id: "snap-1",
        rebalance_frequency: "daily",
        evolution_params: expect.objectContaining({ max_generations: 20 }),
      }),
    );
    // 成功 → 自动流转第 5 步，进化跟踪可见，空态消失
    await waitFor(() => {
      expect(document.querySelector("[data-run-track]")).toBeTruthy();
      expect(document.querySelector("[data-mining-run-empty]")).toBeNull();
    });
  });

  it("createRun 失败 → 显示提交错误，且不流转第 5 步", async () => {
    vi.mocked(factorMiningApi.createRun).mockRejectedValueOnce(new Error("busy"));
    await reachSubmitStep();
    fireEvent.click(document.querySelector("[data-evo-submit]") as HTMLElement);
    fireEvent.click(document.querySelector("[data-evo-confirm-submit]") as HTMLElement);
    await waitFor(() => {
      expect(document.querySelector("[data-mining-submit-error]")).toBeTruthy();
    });
    expect(document.querySelector("[data-run-track]")).toBeNull();
  });
});

describe("A5：结果页数据（#20）", () => {
  it("run 成功后拉取候选并渲染结果排行榜，运行中不拉取", async () => {
    const running = {
      run_id: "run-9", status: "running", generation: 3, max_generations: 20,
      convergence_threshold: 0.01, converged: false, curve: [],
    };
    const { rerender } = render(<MiningShell runProgress={running} />);
    fireEvent.click(document.querySelector('[data-mining-step-jump="run"]') as HTMLElement);
    // 运行中：不出现结果区
    expect(document.querySelector("[data-mining-result-section]")).toBeNull();

    // run 达到 succeeded → 拉取候选 → 渲染结果排行榜
    rerender(
      <MiningShell
        runProgress={{ ...running, status: "succeeded", generation: 20 }}
      />,
    );
    await waitFor(() => {
      expect(document.querySelector("[data-mining-result-section]")).toBeTruthy();
    });
    await waitFor(() => {
      expect(factorMiningApi.listCandidates).toHaveBeenCalledWith("run-9", {
        page_size: 50,
      });
    });
    const rows = document.querySelectorAll("[data-mining-result-rows] tbody tr");
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain("mean(close,5)/mean(close,20)-1");
    expect(rows[1].textContent).toContain("0.41");
  });
});