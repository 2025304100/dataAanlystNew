import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, waitFor } from "@testing-library/react";

/**
 * G5+A4 验收测试：「5 步向导可点通到结果页」+「提交接线」（A4，2026-09-20）。
 *
 * **强引导门控（2026-10-14 重构）**：向导改为顺序推进 ——
 *   1. 「下一步」在当前步未完成时禁用（悬浮给出缺失原因）；
 *   2. 步骤条中「未解锁」的未来步骤禁用点击（锁形图标 + 未解锁态）；
 *   3. 已完成步骤可随时点击回退修改（回退不跨越、不锁定）；
 *   4. 只读态（查看历史任务）不设门控，5 步均可浏览。
 *
 * A4 覆盖（提交回调断言）：
 *   1. 缺候选池快照时无法到达提交步（门控前置拦截，不再依赖提交时兜底）；
 *   2. step1 生成快照 → step4 提交确认 → createRun 带快照 id 调用，
 *      成功后自动流转第 5 步并渲染进化跟踪；
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
  // 回退到已锁定步时 MiningPoolStep 重挂载会拉已存快照（归一化形状）
  fetchSnapshot: vi.fn(async () => ({
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

vi.mock("./step3/fieldApi", () => ({
  createValidation: vi.fn(async () => ({ task_id: "validation-flow" })),
  getValidation: vi.fn(async () => ({ task_id: "validation-flow", status: "passed", passed: true,
    verdict: "pass", blocked: [], warnings: [], valid_until: "2099-01-01T00:00:00Z" })),
}));

import { factorMiningApi } from "../../../../api/factorMining";
import MiningShell from "../MiningShell";
import { createValidation, getValidation } from "./step3/fieldApi";

beforeEach(() => {
  window.localStorage.clear();
  vi.clearAllMocks();
});

/** antd DatePicker：data-* 落在容器上，需对内部 input 输入并回车提交（同 PortfolioReviewDrawer 套路）。 */
const setPickerDate = (attr: string, value: string) => {
  const node = document.querySelector(`[${attr}]`) as HTMLElement;
  const input = (node.tagName === "INPUT" ? node : node.querySelector("input")) as HTMLInputElement;
  fireEvent.change(input, { target: { value } });
  fireEvent.keyDown(input, { key: "Enter", keyCode: 13 });
  fireEvent.blur(input);
};

/** Step1：等预览定型 → 生成挖掘物料 → 等锁定横幅（完成判据 = 快照落库）。 */
async function completePoolStep() {
  await waitFor(() => {
    expect(document.querySelector("[data-pool-preview-stats]")).toBeTruthy();
  });
  fireEvent.click(document.querySelector("[data-pool-generate]") as HTMLElement);
  await waitFor(() => {
    expect(document.querySelector("[data-pool-lock-banner]")).toBeTruthy();
  });
}

/** Step2：起止日期（完成判据 = 两个日期都已选）。 */
async function completeTimeStep() {
  setPickerDate("data-time-start", "2025-01-01");
  setPickerDate("data-time-end", "2025-12-31");
  await waitFor(() => expect(document.querySelector("[data-mining-next]")).toBeEnabled());
}

/** Step3：勾一个字段（完成判据 = 至少 1 个字段）。 */
async function completeFieldStep() {
  await waitFor(() => {
    expect(document.querySelector('[data-field-checkbox="close"]')).toBeTruthy();
  });
  fireEvent.click(document.querySelector('[data-field-checkbox="close"]') as HTMLElement);
  fireEvent.click(document.querySelector("[data-field-start]") as HTMLElement);
  await waitFor(() => expect(document.querySelector("[data-mining-next]")).toBeEnabled());
}

/** Step4：碰一下任意进化参数（完成判据 = evolution_params 上报过）。 */
function touchEvolutionStep() {
  fireEvent.click(document.querySelector("[data-evo-mode-advanced]") as HTMLElement);
}

/** 一路完成 step1~step3 并推进到第 4 步（evolution，含提交按钮）。调用前需已 render。 */
async function reachSubmitStep() {
  await completePoolStep();
  fireEvent.click(document.querySelector("[data-mining-next]") as HTMLElement);
  await completeTimeStep();
  fireEvent.click(document.querySelector("[data-mining-next]") as HTMLElement);
  await completeFieldStep();
  fireEvent.click(document.querySelector("[data-mining-next]") as HTMLElement);
  await waitFor(() => {
    expect(document.querySelector("[data-evo-submit]")).toBeTruthy();
  });
}

/** 完成 step1~step4（含碰一下进化参数），此时 5 步全部解锁可点。 */
async function completeAllConfigSteps() {
  await reachSubmitStep();
  touchEvolutionStep();
}

describe("G5：5 步向导可点通", () => {
  it("勾选字段还不能继续，必须校验通过；修改日期后报告失效", async () => {
    render(<MiningShell />);
    await completePoolStep();
    fireEvent.click(document.querySelector("[data-mining-next]") as HTMLElement);
    await completeTimeStep();
    fireEvent.click(document.querySelector("[data-mining-next]") as HTMLElement);
    await waitFor(() => expect(document.querySelector('[data-field-checkbox="close"]')).toBeTruthy());
    fireEvent.click(document.querySelector('[data-field-checkbox="close"]') as HTMLElement);
    expect(document.querySelector("[data-mining-next]")).toBeDisabled();
    fireEvent.click(document.querySelector("[data-field-start]") as HTMLElement);
    await waitFor(() => expect(document.querySelector("[data-mining-next]")).toBeEnabled());
    expect(createValidation).toHaveBeenLastCalledWith(expect.objectContaining({ config: expect.objectContaining({
      candidate_pool_snapshot_id: "snap-1", start_date: "2025-01-01", end_date: "2025-12-31",
      target_horizon: 5, rebalance_frequency: "daily", selected_fields: ["close"],
    }) }));
    expect(factorMiningApi.computeSplitBudget).toHaveBeenLastCalledWith(expect.objectContaining({
      candidate_pool_snapshot_id: "snap-1",
    }));
    fireEvent.click(document.querySelector('[data-mining-step-jump="time-target"]') as HTMLElement);
    setPickerDate("data-time-start", "2025-02-01");
    await waitFor(() => expect(document.querySelector("[data-mining-next]")).toBeEnabled());
    fireEvent.click(document.querySelector("[data-mining-next]") as HTMLElement);
    expect(document.querySelector("[data-mining-next]")).toBeDisabled();
  });
  it.each(["blocked", "failed"])("字段校验 %s 时保持第三步，重新选择字段出口可用", async (status) => {
    vi.mocked(getValidation).mockResolvedValueOnce({ task_id: "validation-bad", status, passed: false,
      verdict: "block", blocked: [{ field: "close", issue: "没有历史值" }], warnings: [],
      valid_until: "2099-01-01T00:00:00Z", progress: { done: 1, total: 1 } });
    render(<MiningShell />);
    await completePoolStep();
    fireEvent.click(document.querySelector("[data-mining-next]") as HTMLElement);
    await completeTimeStep();
    fireEvent.click(document.querySelector("[data-mining-next]") as HTMLElement);
    await waitFor(() => expect(document.querySelector('[data-field-checkbox="close"]')).toBeTruthy());
    fireEvent.click(document.querySelector('[data-field-checkbox="close"]') as HTMLElement);
    fireEvent.click(document.querySelector("[data-field-start]") as HTMLElement);
    await waitFor(() => expect(document.querySelector("[data-field-blocked]")).toBeTruthy());
    expect(document.querySelector("[data-mining-next]")).toBeDisabled();
    fireEvent.click(document.querySelector("[data-field-exit-reselect]") as HTMLElement);
    expect(document.querySelector("[data-field-selected-count]")?.textContent).toBe("0");
    expect(document.querySelector("[data-field-blocked]")).toBeNull();
    expect(factorMiningApi.createRun).not.toHaveBeenCalled();
  });
  it("默认第 1 步，步骤条含全部 5 步", () => {
    render(<MiningShell />);
    expect(document.querySelector("[data-mining-step-bar]")).toBeTruthy();
    expect(document.querySelectorAll("[data-mining-step]").length).toBe(5);
    expect(document.querySelector('[data-mining-step-panel="pool"]')).toBeTruthy();
  });

  it("第 1 步「上一步」禁用", () => {
    render(<MiningShell />);
    expect(
      (document.querySelector("[data-mining-prev]") as HTMLButtonElement).disabled,
    ).toBe(true);
  });
});

describe("强引导：顺序门控（不越步骤 / 可回退 / 不跨越）", () => {
  it("未完成当前步时「下一步」禁用，悬浮说明缺什么；完成后自动解锁", async () => {
    render(<MiningShell />);
    const next = () => document.querySelector("[data-mining-next]") as HTMLButtonElement;
    // 第 1 步未生成物料 → 下一步禁用且带原因
    expect(next().disabled).toBe(true);
    expect(next().title).toContain("生成挖掘物料");
    // 步骤条：未来步骤全部锁定（禁用 + 锁态）
    expect(
      (document.querySelector('[data-mining-step-jump="field"]') as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(document.querySelector('[data-mining-step="field"]')?.getAttribute("data-mining-step-state")).toBe("locked");

    // 完成第 1 步 → 下一步解锁、第 2 步可点、第 3 步仍锁
    await completePoolStep();
    await waitFor(() => expect(next().disabled).toBe(false));
    expect(
      (document.querySelector('[data-mining-step-jump="time-target"]') as HTMLButtonElement).disabled,
    ).toBe(false);
    expect(
      (document.querySelector('[data-mining-step-jump="field"]') as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("逐步完成配置后可依次到达 5 个步骤面板，第 5 步「下一步」禁用", async () => {
    render(<MiningShell />);
    const next = () => document.querySelector("[data-mining-next]") as HTMLButtonElement;
    await completePoolStep();
    fireEvent.click(next());
    expect(document.querySelector('[data-mining-step-panel="time-target"]')).toBeTruthy();
    expect(next().disabled).toBe(true); // 未选日期
    await completeTimeStep();
    fireEvent.click(next());
    expect(document.querySelector('[data-mining-step-panel="field"]')).toBeTruthy();
    expect(next().disabled).toBe(true); // 未勾字段
    await completeFieldStep();
    await waitFor(() => expect(next().disabled).toBe(false));
    fireEvent.click(next());
    expect(document.querySelector('[data-mining-step-panel="evolution"]')).toBeTruthy();
    expect(next().disabled).toBe(true); // 未碰进化参数
    touchEvolutionStep();
    expect(next().disabled).toBe(true);
    expect(document.querySelector('[data-mining-step-jump="run"]')).toBeDisabled();
    vi.mocked(factorMiningApi.createRun).mockResolvedValueOnce({run_id:"submitted-flow",task_id:"flow-task",queue_position:0,eta_seconds:null,split_budget:null});
    fireEvent.click(document.querySelector("[data-evo-submit]") as HTMLElement);
    fireEvent.click(document.querySelector("[data-evo-confirm-submit]") as HTMLElement);
    await waitFor(() => expect(document.querySelector("[data-run-track]")).toBeTruthy());
    // 到达第 5 步后「下一步」禁用
    expect(next().disabled).toBe(true);
  });

  it("锁定步骤不可点击（点了不跳转）；已完成步骤可点击回退修改", async () => {
    render(<MiningShell />);
    // 未解锁：点击无跳转（按钮禁用，onClick 不触发）
    fireEvent.click(document.querySelector('[data-mining-step-jump="field"]') as HTMLElement);
    expect(document.querySelector('[data-mining-step-panel="pool"]')).toBeTruthy();
    expect(document.querySelector('[data-mining-step-panel="field"]')).toBeNull();

    // 完成第 1 步后：第 2 步可点（回退式浏览），第 3 步仍锁
    await completePoolStep();
    fireEvent.click(document.querySelector('[data-mining-step-jump="time-target"]') as HTMLElement);
    expect(document.querySelector('[data-mining-step-panel="time-target"]')).toBeTruthy();
    fireEvent.click(document.querySelector('[data-mining-step-jump="field"]') as HTMLElement);
    expect(document.querySelector('[data-mining-step-panel="time-target"]')).toBeTruthy();

    // 回到第 1 步修改（已锁定，按钮是「查看看板」）→ 再点步骤条回第 2 步
    fireEvent.click(document.querySelector('[data-mining-step-jump="pool"]') as HTMLElement);
    expect(document.querySelector('[data-mining-step-panel="pool"]')).toBeTruthy();
    fireEvent.click(document.querySelector('[data-mining-step-jump="time-target"]') as HTMLElement);
    expect(document.querySelector('[data-mining-step-panel="time-target"]')).toBeTruthy();
  });

  it("每步顶部有引导横幅：第 N 步/共 5 步 + 完成标志（完成后打上已完成标）", async () => {
    render(<MiningShell />);
    const guide = document.querySelector("[data-mining-step-guide]");
    expect(guide?.textContent ?? "").toContain("第 1 步 / 共 5 步");
    expect(guide?.textContent ?? "").toContain("候选股票池");
    expect(guide?.getAttribute("data-mining-step-guide-done")).toBeNull();
    await completePoolStep();
    await waitFor(() => {
      expect(
        document.querySelector("[data-mining-step-guide]")?.getAttribute("data-mining-step-guide-done"),
      ).toBe("true");
    });
  });

  it("顺序规则提示只在编辑态出现", async () => {
    render(<MiningShell />);
    expect(document.querySelector("[data-mining-wizard-seq-hint]")).toBeTruthy();
  });

  it("日期倒序时保持在时间步骤并锁住后续配置", async () => {
    render(<MiningShell />);
    await completePoolStep();
    fireEvent.click(document.querySelector("[data-mining-next]") as HTMLElement);
    await completeTimeStep();
    setPickerDate("data-time-start", "2026-08-01");
    expect(document.querySelector("[data-mining-next]")).toBeDisabled();
    expect(document.querySelector('[data-mining-step-jump="field"]')).toBeDisabled();
  });

  it("更改日期后清除旧预算，检查失败时阻断并允许重试", async () => {
    render(<MiningShell />);
    await completePoolStep();
    fireEvent.click(document.querySelector("[data-mining-next]") as HTMLElement);
    await completeTimeStep();
    expect(document.querySelector("[data-split-budget]")).toBeTruthy();
    vi.mocked(factorMiningApi.computeSplitBudget).mockRejectedValueOnce(new Error("budget unavailable"));
    setPickerDate("data-time-end", "2025-12-30");
    expect(document.querySelector("[data-mining-next]")).toBeDisabled();
    await waitFor(() => expect(document.querySelector("[data-mining-budget-retry]")).toBeTruthy());
    expect(document.querySelector("[data-split-budget]")).toBeNull();
    fireEvent.click(document.querySelector("[data-mining-budget-retry]") as HTMLElement);
    await waitFor(() => expect(document.querySelector("[data-mining-next]")).toBeEnabled());
  });

  it("有效样本未达到后端门槛时不能继续", async () => {
    vi.mocked(factorMiningApi.computeSplitBudget).mockResolvedValueOnce({
      frequency: "weekly", total_points: 89, train_points: 53, val_points: 17, test_points: 17,
      purge_points: 1, embargo_points: 1, purge_trading_days: 5, embargo_trading_days: 5,
      tail_loss: 0, frequency_floor: 104, meets_floor: false, statistically_degraded: false,
    });
    render(<MiningShell />);
    await completePoolStep();
    fireEvent.click(document.querySelector("[data-mining-next]") as HTMLElement);
    setPickerDate("data-time-start", "2025-01-01");
    setPickerDate("data-time-end", "2025-12-31");
    await waitFor(() => expect(document.querySelector("[data-split-floor-fail]")).toBeTruthy());
    expect(document.querySelector("[data-mining-next]")).toBeDisabled();
    expect(document.querySelector('[data-mining-step-jump="field"]')).toBeDisabled();
  });

  it("填写进化参数仍需提交任务，不能进入空运行页", async () => {
    render(<MiningShell />);
    await completeAllConfigSteps();
    fireEvent.click(document.querySelector('[data-mining-step-jump="run"]') as HTMLElement);
    expect(document.querySelector('[data-mining-step-panel="evolution"]')).toBeTruthy();
    expect(document.querySelector("[data-mining-run-empty]")).toBeNull();
    expect(document.querySelector('[data-mining-step-jump="run"]')).toBeDisabled();
    expect(document.querySelector("[data-run-track]")).toBeNull();
  });

  it("第 5 步有运行数据时渲染进化跟踪", async () => {
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

  it("缺候选池快照时无法到达提交步骤（强引导门控前置拦截）", async () => {
    render(<MiningShell />);
    // 不生成快照 → 第 4 步（提交步）始终锁定，提交按钮不可达
    expect(
      (document.querySelector('[data-mining-step-jump="evolution"]') as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(document.querySelector("[data-evo-submit]")).toBeNull();
    expect(factorMiningApi.createRun).not.toHaveBeenCalled();
  });

  it("生成快照后提交 → createRun 带快照 id，第 5 步渲染进化跟踪", async () => {
    vi.mocked(factorMiningApi.createRun).mockResolvedValueOnce({
      run_id: "run-1", task_id: "task-1", queue_position: 0,
      eta_seconds: 30, split_budget: null,
    });
    render(<MiningShell />);
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
    render(<MiningShell />);
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
    fireEvent.click(document.querySelector('[data-mining-new-task]') as HTMLElement);
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
    const rows = document.querySelectorAll("[data-result-rows] tr");
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain("mean(close,5)/mean(close,20)-1");
    expect(rows[1].textContent).toContain("0.41");
  });
});
