import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, waitFor } from "@testing-library/react";

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

vi.mock("./wizard/step1/poolApi", () => ({
  MIN_POOL_SIZE: 50,
  previewPool: vi.fn(async () => ({
    universe_size: 5000, hits: 800, excluded_total: 4200, can_generate: true,
    min_pool_size: 50, as_of_date: "2026-08-21", categories: [], samples: [],
    field_coverage: {}, warnings: [],
  })),
  createPoolFromFilter: vi.fn(async () => ({ id: "pool-1", name: "p", member_count: 800 })),
  // 归一化形状（真实 poolApi.normalizeSnapshot 输出）：业务侧统一读 id
  createSnapshot: vi.fn(async () => ({
    id: "snap-1", pool_id: "pool-1", is_locked: true, data_cutoff_at: "2026-08-21",
  })),
  // 回退到已锁定步时 MiningPoolStep 重挂载会拉已存快照（归一化形状）
  fetchSnapshot: vi.fn(async () => ({
    id: "snap-1", pool_id: "pool-1", is_locked: true, data_cutoff_at: "2026-08-21",
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
  createValidation: vi.fn(async () => ({ task_id: "validation-draft" })),
  getValidation: vi.fn(async () => ({ task_id: "validation-draft", status: "passed", passed: true,
    verdict: "pass", blocked: [], warnings: [], valid_until: "2099-01-01T00:00:00Z" })),
  normalizeValidation: vi.fn(),
}));

import { factorMiningApi } from "../../../api/factorMining";
import MiningShell from "./MiningShell";

const saveDraft = factorMiningApi.saveDraft as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  vi.mocked(factorMiningApi.getRun).mockImplementation(async (id) => ({ id, status: "running", current_generation: 0 } as never));
  vi.mocked(factorMiningApi.resumeRun).mockResolvedValue({} as never);
  vi.mocked(factorMiningApi.listDrafts).mockResolvedValue([]);
});

/** antd DatePicker：data-* 落在容器/输入框上，需对内部 input 输入并回车提交。 */
const setPickerDate = (attr: string, value: string) => {
  const node = document.querySelector(`[${attr}]`) as HTMLElement;
  const input = (node.tagName === "INPUT" ? node : node.querySelector("input")) as HTMLInputElement;
  fireEvent.change(input, { target: { value } });
  fireEvent.keyDown(input, { key: "Enter", keyCode: 13 });
  fireEvent.blur(input);
};

/**
 * 强引导门控（2026-10-14）后到达第 4 步的唯一正路：
 * 逐步完成 step1（生成快照）→ step2（起止日期）→ step3（勾字段），
 * 「下一步」随完成度逐个解锁（不能越步骤）。
 */
async function reachEvolutionStep(mount = true) {
  if (mount) {
    render(<MiningShell />);
    fireEvent.click(document.querySelector("[data-mining-new-task]") as HTMLElement);
  }
  await waitFor(() => {
    expect(document.querySelector("[data-pool-preview-stats]")).toBeTruthy();
  });
  fireEvent.click(document.querySelector("[data-pool-generate]") as HTMLElement);
  await waitFor(() => {
    expect(document.querySelector("[data-pool-lock-banner]")).toBeTruthy();
  });
  fireEvent.click(document.querySelector("[data-mining-next]") as HTMLElement);
  setPickerDate("data-time-start", "2025-01-01");
  setPickerDate("data-time-end", "2025-06-30");
  await waitFor(() => expect((document.querySelector("[data-mining-next]") as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(document.querySelector("[data-mining-next]") as HTMLElement);
  await waitFor(() => {
    expect(document.querySelector('[data-field-checkbox="close"]')).toBeTruthy();
  });
  fireEvent.click(document.querySelector('[data-field-checkbox="close"]') as HTMLElement);
  fireEvent.click(document.querySelector("[data-field-start]") as HTMLElement);
  await waitFor(() => expect(document.querySelector("[data-mining-next]")).toBeEnabled());
  fireEvent.click(document.querySelector("[data-mining-next]") as HTMLElement);
  await waitFor(() => {
    expect(document.querySelector("[data-evo-save-draft]")).toBeTruthy();
  });
}

function clickSaveDraft() {
  const btn = document.querySelector("[data-evo-save-draft]") as HTMLElement;
  expect(btn).toBeTruthy();
  fireEvent.click(btn);
}

function confirmSubmit() {
  fireEvent.click(document.querySelector("[data-evo-submit]") as HTMLElement);
  fireEvent.click(document.querySelector("[data-evo-confirm-submit]") as HTMLElement);
}

describe("保存草稿接线（U1）", () => {
  beforeEach(() => {
    saveDraft.mockReset();
    saveDraft.mockResolvedValue({ draft_id: "draft-abc12345", current_step: 4, steps: {} });
  });

  it("点击后真的调用后端草稿接口，并带上四个步骤", async () => {
    await reachEvolutionStep();
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
    await reachEvolutionStep();
    clickSaveDraft();
    await waitFor(() => {
      const note = document.querySelector("[data-mining-draft-note]");
      expect(note?.textContent ?? "").toContain("draft-ab");
    });
  });

  it("二次保存复用上次的 draft_id（更新而非新建）", async () => {
    await reachEvolutionStep();
    clickSaveDraft();
    await waitFor(() => expect(saveDraft).toHaveBeenCalledTimes(1));

    clickSaveDraft();
    await waitFor(() => expect(saveDraft).toHaveBeenCalledTimes(2));
    const second = saveDraft.mock.calls[1][0] as Record<string, unknown>;
    expect(second.draft_id).toBe("draft-abc12345");
  });

  it("同一会话快速重复保存只产生一个进行中的保存请求", async () => {
    let resolveSave!: (value: unknown) => void;
    saveDraft.mockImplementation(() => new Promise((resolve) => { resolveSave = resolve; }));
    await reachEvolutionStep();
    clickSaveDraft();
    clickSaveDraft();
    expect(saveDraft).toHaveBeenCalledTimes(1);
    await act(async () => { resolveSave({ draft_id: "draft-once" }); });
  });

  it("旧会话保存迟到不会回写新草稿编号，也不阻止新会话独立保存", async () => {
    let resolveOldSave!: (value: unknown) => void;
    saveDraft
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOldSave = resolve; }))
      .mockResolvedValueOnce({ draft_id: "draft-fresh" });
    await reachEvolutionStep();
    clickSaveDraft();
    fireEvent.click(document.querySelector(".ant-drawer-close") as HTMLElement);
    fireEvent.click(document.querySelector("[data-mining-new-task]") as HTMLElement);
    await reachEvolutionStep(false);
    clickSaveDraft();
    await waitFor(() => expect(document.querySelector("[data-mining-draft-note]")?.textContent).toContain("draft-fr"));
    await act(async () => { resolveOldSave({ draft_id: "draft-old-123" }); });
    expect(document.querySelector("[data-mining-draft-note]")?.textContent).toContain("draft-fr");
    clickSaveDraft();
    await waitFor(() => expect(saveDraft).toHaveBeenCalledTimes(3));
    expect(saveDraft.mock.calls[2][0].draft_id).toBe("draft-fresh");
  });

  it("失败时显示失败提示，不静默", async () => {
    saveDraft.mockRejectedValueOnce(new Error("boom"));
    await reachEvolutionStep();
    clickSaveDraft();
    await waitFor(() => {
      const note = document.querySelector("[data-mining-draft-note]");
      expect(note?.textContent ?? "").toContain("失败");
    });
  });

  it("同一会话重复确认提交只创建一个挖掘任务", async () => {
    let resolveCreate!: (value: unknown) => void;
    const createRun = vi.mocked(factorMiningApi.createRun);
    createRun.mockReset();
    createRun.mockImplementation(() => new Promise((resolve) => { resolveCreate = resolve as (value: unknown) => void; }));
    await reachEvolutionStep();
    confirmSubmit();
    confirmSubmit();
    expect(createRun).toHaveBeenCalledTimes(1);
    await act(async () => { resolveCreate({ run_id: "run-created" }); });
  });

  it("提交后关闭并新建，旧创建结果不能劫持新向导或写入最近任务", async () => {
    let resolveOldCreate!: (value: unknown) => void;
    vi.mocked(factorMiningApi.createRun).mockImplementation(() => new Promise((resolve) => { resolveOldCreate = resolve as (value: unknown) => void; }));
    await reachEvolutionStep();
    confirmSubmit();
    fireEvent.click(document.querySelector(".ant-drawer-close") as HTMLElement);
    fireEvent.click(document.querySelector("[data-mining-new-task]") as HTMLElement);
    await act(async () => { resolveOldCreate({ run_id: "run-old-created" }); });
    expect(document.querySelector('[data-mining-step-panel="pool"]')).toBeTruthy();
    expect(document.querySelector("[data-run-track]")).toBeNull();
    expect(window.localStorage.getItem("mining_last_run_id")).toBeNull();
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

  it("完整草稿先复核时间预算，通过后手动继续且保留原快照、字段和进化参数", async () => {
    const completeDraft = {
      ...DRAFTS[0],
      current_step: 4,
      steps: {
        ...DRAFTS[0].steps,
        step2: { ...DRAFTS[0].steps.step2, start_date: "2021-01-01", end_date: "2026-06-30", data_cutoff_at: "2026-08-21", rebalance_frequency: "daily" },
        step3: { selected_fields: ["close"] },
        step4: { evolution_params: { population_size: 100, max_generations: 10, ai_enabled: false } },
      },
    };
    vi.mocked(factorMiningApi.listDrafts).mockResolvedValue([completeDraft]);
    saveDraft.mockResolvedValue({ draft_id: completeDraft.draft_id });
    render(<MiningShell />);
    await waitFor(() => expect(document.querySelector("[data-mining-task-edit]")).toBeTruthy());
    fireEvent.click(document.querySelector("[data-mining-task-edit]") as HTMLElement);
    await waitFor(() => expect(document.querySelector('[data-mining-step-panel="time-target"]')).toBeTruthy());
    expect((document.querySelector("[data-time-start]") as HTMLInputElement).value).toBe("2021-01-01");
    await waitFor(() => expect((document.querySelector("[data-mining-next]") as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(document.querySelector("[data-mining-next]") as HTMLElement);
    await waitFor(() => {
      const field = document.querySelector('[data-field-checkbox="close"]') as HTMLElement;
      const checkbox = field?.matches("input") ? field : field?.querySelector("input");
      expect((checkbox as HTMLInputElement)?.checked).toBe(true);
    });
    expect(document.querySelector("[data-mining-next]")).toBeDisabled();
    fireEvent.click(document.querySelector("[data-field-start]") as HTMLElement);
    await waitFor(() => expect(document.querySelector("[data-mining-next]")).toBeEnabled());
    fireEvent.click(document.querySelector("[data-mining-next]") as HTMLElement);
    clickSaveDraft();
    await waitFor(() => expect(saveDraft).toHaveBeenCalledTimes(1));
    expect(saveDraft.mock.calls[0][0]).toMatchObject({
      draft_id: completeDraft.draft_id,
      candidate_pool_snapshot_id: "snap-draft-1",
      steps: { step3: { selected_fields: ["close"] }, step4: { evolution_params: { population_size: 100, max_generations: 10, ai_enabled: false } } },
    });
    const { createSnapshot } = await import("./wizard/step1/poolApi");
    expect(createSnapshot).not.toHaveBeenCalled();
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

  it("旧会话加载草稿列表失败时不把错误写入新会话", async () => {
    let rejectOldLoad!: (reason: unknown) => void;
    vi.mocked(factorMiningApi.listDrafts).mockImplementation((params) => params?.limit === 20
      ? new Promise((_, reject) => { rejectOldLoad = reject; })
      : Promise.resolve([]));
    render(<MiningShell />);
    fireEvent.click(document.querySelector("[data-mining-new-task]") as HTMLElement);
    fireEvent.click(document.querySelector("[data-mining-draft-open]") as HTMLElement);
    expect(document.querySelector("[data-mining-draft-loading]")).toBeTruthy();
    fireEvent.click(document.querySelector(".ant-drawer-close") as HTMLElement);
    fireEvent.click(document.querySelector("[data-mining-new-task]") as HTMLElement);
    await act(async () => { rejectOldLoad(new Error("旧列表失败")); });
    expect(document.querySelector("[data-mining-draft-note]")).toBeNull();
    expect(document.querySelector("[data-mining-draft-list]")).toBeNull();
  });
});
