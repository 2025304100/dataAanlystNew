import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, waitFor } from "@testing-library/react";

/**
 * U1 修复哨兵（2026-09-23）：「保存草稿」必须真的保存。
 *
 * 缺陷背景：`MiningShell` 从未给 `MiningEvoParamStep` 注入 `onSaveDraft`，
 * 按钮 `onClick` 实为 `undefined` —— 点击**零反应**（验收报告 P1-1：
 * 「无任何网络请求、localStorage 无变化、无 toast 反馈」）。
 *
 * 后端 `POST /factor-mining/drafts` 早已实现（`draft_service.save_draft`），
 * 本卡把它接上：组装 steps(step1~step4) → 保存 → 给出可见反馈 → 二次保存复用 draft_id。
 */
vi.mock("../../../api/factorMining", () => ({
  factorMiningApi: {
    listRuns: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    listMiningFields: vi.fn(async () => ({
      fields: [
        { field: "close", label_zh: "收盘价", group: "quote", group_label_zh: "行情", availability: "available" },
      ],
    })),
    getRun: vi.fn(async (id: string) => ({ id, status: "running", current_generation: 0 })),
    listGenerations: vi.fn(async () => []),
    createRun: vi.fn(),
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
    computeSplitBudget: vi.fn(async () => ({
      available: true, frequency: "daily", total_points: 252, train_points: 151,
      val_points: 50, test_points: 50, purge_points: 5, embargo_points: 5,
      purge_trading_days: 5, embargo_trading_days: 5, tail_loss: 5,
      frequency_floor: 252, meets_floor: true, statistically_degraded: false,
      train_start: "2025-01-01", train_end: "2025-12-31", val_end: "2026-03-31",
    })),
    saveDraft: vi.fn(),
    listDrafts: vi.fn(async () => []),
    deleteDraft: vi.fn(async () => ({ draft_id: "d1", deleted: true })),
  },
}));

vi.mock("../../../api/dataMirror", () => ({
  dataMirrorApi: {
    getStatus: vi.fn(async () => ({
      available: true, mirrored_from: "2021-01-01", mirrored_to: "2026-09-07",
      total_rows: 8090000, total_symbols: 7270,
    })),
  },
}));

vi.mock("../../../api/client", () => ({
  api: { scoringGetOverviewAsFactor: vi.fn(async () => ({})) },
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

import { factorMiningApi } from "../../../api/factorMining";
import MiningShell from "./MiningShell";

const saveDraft = factorMiningApi.saveDraft as unknown as ReturnType<typeof vi.fn>;

function gotoEvolutionStep() {
  const jump = document.querySelector("[data-mining-step-jump='evolution']") as HTMLElement;
  expect(jump).toBeTruthy();
  fireEvent.click(jump);
}

function clickSaveDraft() {
  const btn = document.querySelector("[data-evo-save-draft]") as HTMLElement;
  expect(btn).toBeTruthy();
  fireEvent.click(btn);
}

describe("保存草稿接线（U1）", () => {
  beforeEach(() => {
    saveDraft.mockReset();
    saveDraft.mockResolvedValue({ draft_id: "draft-abc12345", current_step: 4, steps: {} });
  });

  it("点击后真的调用后端草稿接口，并带上四个步骤", async () => {
    render(<MiningShell />);
    gotoEvolutionStep();
    clickSaveDraft();

    await waitFor(() => expect(saveDraft).toHaveBeenCalledTimes(1));
    const payload = saveDraft.mock.calls[0][0] as Record<string, unknown>;
    const steps = payload.steps as Record<string, unknown>;
    expect(Object.keys(steps).sort()).toEqual(["step1", "step2", "step3", "step4"]);
    expect(payload.current_step).toBe(4);
    // 首次保存不带 draft_id（后端新建）
    expect(payload.draft_id == null).toBe(true);
  });

  it("成功后给出可见反馈（含草稿短号）", async () => {
    render(<MiningShell />);
    gotoEvolutionStep();
    clickSaveDraft();
    await waitFor(() => {
      const note = document.querySelector("[data-mining-draft-note]");
      expect(note?.textContent ?? "").toContain("draft-ab");
    });
  });

  it("二次保存复用上次的 draft_id（更新而非新建）", async () => {
    render(<MiningShell />);
    gotoEvolutionStep();
    clickSaveDraft();
    await waitFor(() => expect(saveDraft).toHaveBeenCalledTimes(1));

    clickSaveDraft();
    await waitFor(() => expect(saveDraft).toHaveBeenCalledTimes(2));
    const second = saveDraft.mock.calls[1][0] as Record<string, unknown>;
    expect(second.draft_id).toBe("draft-abc12345");
  });

  it("失败时显示失败提示，不静默", async () => {
    saveDraft.mockRejectedValueOnce(new Error("boom"));
    render(<MiningShell />);
    gotoEvolutionStep();
    clickSaveDraft();
    await waitFor(() => {
      const note = document.querySelector("[data-mining-draft-note]");
      expect(note?.textContent ?? "").toContain("失败");
    });
  });
});

describe("DEF-3：运行操作失败不再静默（resume 撞 409 给可见反馈）", () => {
  it("resume 返回 409 信封 → 显示错误提示条，含后端可读文案", async () => {
    window.localStorage.setItem("mining_last_run_id", "run-409");
    vi.mocked(factorMiningApi.getRun).mockResolvedValue({
      id: "run-409", status: "paused", current_generation: 1,
      max_generation: 20, total_trials: 0, converged: false,
    } as never);
    vi.mocked(factorMiningApi.resumeRun).mockRejectedValue({
      response: {
        data: {
          detail: {
            error_code: "MINING_DOMAIN_BUSY",
            title_zh: "已有挖掘任务进行中",
            detail_zh: "当前任务 task-x 仍在运行，稍后再试",
          },
        },
      },
    } as never);

    render(<MiningShell />);
    // 自动恢复 run-409 → step5；paused 态出现「继续」按钮
    await waitFor(() => {
      expect(document.querySelector("[data-run-resume]")).toBeTruthy();
    });
    fireEvent.click(document.querySelector("[data-run-resume]") as HTMLElement);

    await waitFor(() => {
      const bar = document.querySelector("[data-run-op-error]");
      expect(bar).toBeTruthy();
      expect(bar?.textContent ?? "").toContain("已有挖掘任务进行中");
    });
  });

  it("操作成功后清除错误提示条", async () => {
    window.localStorage.setItem("mining_last_run_id", "run-ok");
    vi.mocked(factorMiningApi.getRun).mockResolvedValue({
      id: "run-ok", status: "paused", current_generation: 1,
      max_generation: 20, total_trials: 0, converged: false,
    } as never);

    render(<MiningShell />);
    await waitFor(() => {
      expect(document.querySelector("[data-run-resume]")).toBeTruthy();
    });
    fireEvent.click(document.querySelector("[data-run-resume]") as HTMLElement);
    await waitFor(() => {
      // 成功路径不出现错误条（resumeRun mock 默认 resolve）
      expect(document.querySelector("[data-run-op-error]")).toBeNull();
    });
  });
});


describe("DEF-10/DEF-5：草稿载入与删除（草稿不再只写不读）", () => {
  const DRAFTS = [
    {
      draft_id: "draft-abcdef123456",
      name: "我的草稿",
      current_step: 2,
      candidate_pool_snapshot_id: "snap-draft-1",
      steps: {
        step1: { candidate_pool_snapshot_id: "snap-draft-1", pool_id: "pool-draft" },
        step2: {
          start_date: "2026-01-05", end_date: "2026-11-01",
          data_cutoff_at: "2026-11-10", rebalance_frequency: "weekly",
          target_horizon: 5, train_ratio: 0.6, validation_ratio: 0.2,
        },
        step3: { selected_fields: ["close", "pe_ttm"] },
        step4: { evolution_params: { population_size: 30, max_generations: 5 } },
      },
    },
  ];

  beforeEach(() => {
    vi.mocked(factorMiningApi.listDrafts).mockResolvedValue(DRAFTS);
    vi.mocked(factorMiningApi.deleteDraft).mockResolvedValue({ deleted: true });
  });

  it("点击「载入草稿」→ 拉列表并渲染草稿项", async () => {
    render(<MiningShell />);
    fireEvent.click(document.querySelector("[data-mining-draft-open]") as HTMLElement);
    await waitFor(() => {
      expect(factorMiningApi.listDrafts).toHaveBeenCalled();
      expect(document.querySelector("[data-mining-draft-list]")).toBeTruthy();
    });
    await waitFor(() => {
      expect(
        document.querySelector('[data-mining-draft-item="draft-abcdef123456"]'),
      ).toBeTruthy();
    });
  });

  it("载入草稿 → 回填配置并给出可见反馈", async () => {
    render(<MiningShell />);
    fireEvent.click(document.querySelector("[data-mining-draft-open]") as HTMLElement);
    await waitFor(() => {
      expect(
        document.querySelector("[data-mining-draft-restore]"),
      ).toBeTruthy();
    });
    fireEvent.click(document.querySelector("[data-mining-draft-restore]") as HTMLElement);
    await waitFor(() => {
      const note = document.querySelector("[data-mining-draft-note]");
      expect(note?.textContent ?? "").toContain("已载入草稿");
    });
    // 跳到草稿记录的第 2 步（0-based index 1 = 时间与切分）
    expect(
      document.querySelector("[data-mining-step-panel='time-target']"),
    ).toBeTruthy();
  });

  it("删除草稿 → 调删除接口并从列表移除", async () => {
    render(<MiningShell />);
    fireEvent.click(document.querySelector("[data-mining-draft-open]") as HTMLElement);
    await waitFor(() => {
      expect(document.querySelector("[data-mining-draft-delete]")).toBeTruthy();
    });
    fireEvent.click(document.querySelector("[data-mining-draft-delete]") as HTMLElement);
    await waitFor(() => {
      expect(factorMiningApi.deleteDraft).toHaveBeenCalledWith("draft-abcdef123456");
    });
    await waitFor(() => {
      expect(
        document.querySelector('[data-mining-draft-item="draft-abcdef123456"]'),
      ).toBeNull();
    });
  });
});
