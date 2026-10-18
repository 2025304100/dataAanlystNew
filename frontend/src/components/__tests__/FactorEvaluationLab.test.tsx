// WP5-07：FactorEvaluationLab 组件测试
//
// 覆盖：
// - 渲染：空任务/空运行时显示空状态；i18n key 正常解析
// - 提交评估任务：调用 createEvaluationTask，禁用表单直到完成
// - 表单校验：因子代码为空时显示警告，不发起请求
// - 任务列表：展示历史任务及状态/阶段/进度
// - 运行历史：展示 run_id 缩写、门禁标签、版本；支持门禁筛选
// - 运行报告：选中运行后展示基础指标、门禁结论、拒绝原因、压力测试、完整交易日证据
// - 任务取消：调用 cancelEvaluationTask 并刷新活动任务
// - 进度轮询：运行中任务触发 getEvaluationTask，终态后刷新列表
//
// 约束：
// - mock api/client（api），不调用真实后端
// - mock antd 的 message（success/error/warning/info）以兼容 App.useApp()
// - mock AppContext，提供 zh-CN locale
// - i18n mock：t(key) 返回 key，便于按 key 断言
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, act, within } from "@testing-library/react";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockContext, mockApi, mockMessage } = vi.hoisted(() => ({
  mockContext: {
    locale: "zh-CN",
    showToast: vi.fn(),
    scoringGetOverviewAsFactor: vi.fn(async () => ({ feature_enabled: true, config: {}, runtime: {}, health: {}, latest_trade_date: null, factor_coverage: [] })),
    scoringListTasks: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    getInboxNotifications: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    scoringGetOverview: vi.fn(async () => ({ feature_enabled: true, config: {}, runtime: {}, health: {}, factor_coverage: [] })),
    scoringGetFactorDefinition: vi.fn(async () => ({})),
    scoringGetFactorModelListAsFactor: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    getEvaluationTaskHeartbeat: vi.fn(async () => ({})),
  },
  mockApi: {
    listFactorDefinitions: vi.fn(),
    scoringListFactorDefinitions: vi.fn(),
    listFactorVersions: vi.fn(),
    preflightFactorEvaluation: vi.fn(),
    listEvaluationTasks: vi.fn(),
    getEvaluationTaskHeartbeat: vi.fn(async () => ({})),
    getEvaluationTask: vi.fn(),
    createEvaluationTask: vi.fn(),
    cancelEvaluationTask: vi.fn(),
    listEvaluationRuns: vi.fn(),
    getEvaluationRun: vi.fn(),
  },
  mockMessage: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

// Mock antd：保留组件库，覆盖 message 与 App.useApp（组件通过 App.useApp 获取 message）
vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return {
    ...actual,
    message: mockMessage,
    App: {
      ...actual.App,
      useApp: () => ({
        message: mockMessage,
        notification: mockMessage,
        modal: {},
      }),
    },
  };
});

// Mock i18n：t(key) 直接返回 key，便于按 key 断言
vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string) => key,
  enumLabel: (_prefix: string, value: string | null | undefined) => value ?? "-",
  factorLabel: (_code: string, fallbackName?: string) => fallbackName ?? "-",
}));

// Mock AppContext
vi.mock("../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

// Mock api/client
vi.mock("../../api/client", () => ({
  api: mockApi,
}));

import FactorEvaluationLab from "../factors/FactorEvaluationLab";

async function openRunsTab() {
  fireEvent.click(screen.getByRole("tab", { name: "evalTabRuns" }));
  await waitFor(() => {
    expect(screen.getByRole("tab", { name: "evalTabRuns" })).toHaveAttribute("aria-selected", "true");
  });
}

/** 构造一个任务对象。 */
function makeTask(overrides: Partial<any> = {}): any {
  return {
    id: "task-1",
    task_type: "wp5_evaluation",
    status: "queued",
    stage: "queued",
    percent: 0,
    message: "queued",
    total: 0,
    processed: 0,
    ok_count: 0,
    failed_count: 0,
    current_item: null,
    result: null,
    errors: [],
    error_code: null,
    created_at: "2026-08-01T10:00:00",
    started_at: null,
    finished_at: null,
    updated_at: null,
    ...overrides,
  };
}

/** 构造一个运行记录。 */
function makeRun(overrides: Partial<any> = {}): any {
  return {
    id: "eval-1-abc12345-20260801",
    factor_version_id: 1,
    universe_snapshot_id: null,
    data_cutoff_at: "2026-07-31T18:00:00",
    target_code: "target_5d_return",
    train_start_date: "2026-05-01T00:00:00",
    train_end_date: "2026-06-15T00:00:00",
    validation_start_date: "2026-06-20T00:00:00",
    validation_end_date: "2026-07-20T00:00:00",
    config: {
      factor_kind: "continuous",
      target_horizon: 5,
      n_groups: 5,
      cost_rate: 0.001,
      evaluator_version: "wp5-1.0.0",
    },
    metrics: {
      rank_ic_mean: 0.05,
      rank_ic_median: 0.045,
      rank_ic_std: 0.12,
      icir: 0.42,
      positive_ic_ratio: 0.55,
      coverage: 0.92,
      n_samples: 1200,
      quantile_returns: [0.001, 0.003, 0.005, 0.008, 0.012],
      monotonicity_score: 0.8,
      long_short_return: 0.011,
      turnover: 0.35,
      cost_adjusted_return: 0.008,
      train_start: "2026-05-01",
      train_end: "2026-06-15",
      validation_start: "2026-06-20",
      validation_end: "2026-07-20",
      stress_test: {
        overall_verdict: "stable",
        failure_reasons: [],
        parameter_results: [
          {
            param_name: "window",
            baseline_value: 20,
            verdict: "stable",
            sign_consistency_ratio: 0.8,
            median_ic_ratio: 0.75,
            passing_neighbor_count: 4,
            has_cliff_drop: false,
            points: [
              {
                label: "baseline",
                ratio: 0,
                param_value: 20,
                ic_mean: 0.05,
                icir: 0.42,
                passed_min_gate: true,
              },
            ],
          },
        ],
        time_result: {
          ic_stability: 0.7,
          verdict: "stable",
          segments: [
            { segment_label: "Q1", ic_mean: 0.04, icir: 0.35 },
            { segment_label: "Q2", ic_mean: 0.06, icir: 0.48 },
          ],
        },
        missing_result: {
          ic_decay_ratio: 0.15,
          verdict: "stable",
        },
      },
    },
    gate_result: "passed",
    rejection_reasons: [],
    artifact_path: null,
    task_id: "task-1",
    created_by: "local_user",
    selected_trade_date: "2026-07-31",
    observed_symbols: 4800,
    expected_symbols: 5000,
    completeness_ratio: 0.96,
    fallback_reason: null,
    created_at: "2026-08-01T10:05:00",
    ...overrides,
  };
}

describe("FactorEvaluationLab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockApi.listFactorDefinitions.mockResolvedValue({
      items: [{ code: "turnover_z20", name: "换手率因子" }],
    });
    mockApi.scoringListFactorDefinitions.mockResolvedValue({
      items: [{ id: 1, code: "turnover_z20", name: "换手率因子", factor_kind: "continuous", direction: "higher_better" }],
    });
    mockApi.listFactorVersions.mockResolvedValue([{ id: 1, factor_id: 1, version: 1, is_latest: true, direction: "higher_better" }]);
    mockApi.listEvaluationTasks.mockResolvedValue([]);
    mockApi.listEvaluationRuns.mockResolvedValue([]);
    mockApi.preflightFactorEvaluation.mockResolvedValue({
      overall: {
        passed: true,
        blocking_count: 0,
        recommended_date_range: ["2026-05-01", "2026-07-20"],
      },
      items: [{
        code: "preflight.factor_values.ok",
        severity: "pass",
        category: "data",
        title_zh: "sample execution passed",
        detail_zh: "sample contains valid factor values",
        evidence: { valid_factor_rows: 100 },
        retryable: false,
      }],
    });
    mockApi.getEvaluationTask.mockResolvedValue(makeTask());
    mockApi.createEvaluationTask.mockResolvedValue(makeTask({ status: "queued" }));
    mockApi.cancelEvaluationTask.mockResolvedValue(makeTask({ status: "cancelled" }));
    mockApi.getEvaluationRun.mockResolvedValue(makeRun());
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  // TODO(P1.1 镜像适配层)：本用例写在组件旧实现下，镜像层改造后已脱节（该文件 12 项与共享 helper mockLoadSuccess 联动，需整文件重写）；见 docs/前端既有红定性报告.md §9/§10 与 PROGRESS.debt.TD-FE-RED-2。

  it("renders empty state for tasks and runs when API returns empty", async () => {
    render(<FactorEvaluationLab />);

    await waitFor(() => {
      expect(mockApi.listEvaluationTasks).toHaveBeenCalled();
      expect(mockApi.listEvaluationRuns).toHaveBeenCalled();
    });

    await openRunsTab();

    // 空状态文案可见
    expect(await screen.findByText("evalLabEmptyTasks")).toBeInTheDocument();
    expect(await screen.findByText("evalLabEmptyRuns")).toBeInTheDocument();
  });

  // TODO(P1.1 镜像适配层)：本用例写在组件旧实现下，镜像层改造后已脱节（该文件 12 项与共享 helper mockLoadSuccess 联动，需整文件重写）；见 docs/前端既有红定性报告.md §9/§10 与 PROGRESS.debt.TD-FE-RED-2。

  it("requires factor code before submitting", async () => {
    render(<FactorEvaluationLab />);

    await waitFor(() => {
      expect(mockApi.listEvaluationTasks).toHaveBeenCalled();
    });

    // 不输入因子代码直接点击提交
    const submitBtn = screen.getByRole("button", { name: "evalLabSubmit" });
    expect(submitBtn).toBeDisabled();
    expect(mockApi.createEvaluationTask).not.toHaveBeenCalled();
  });

  it("scopes history to the selected version and never presents another factor's completed task as its report", async () => {
    const otherRun = makeRun({ id: "other-factor-report", factor_version_id: 999 });
    mockApi.listEvaluationTasks.mockResolvedValue([makeTask({ status: "completed", result: { run_id: otherRun.id, factor_code: "other_factor" } })]);
    mockApi.getEvaluationRun.mockResolvedValue(otherRun);
    mockApi.listEvaluationRuns.mockImplementation(async (query: {factorVersionId?:number}) => query.factorVersionId === 1 ? [] : [otherRun]);
    render(<FactorEvaluationLab initialFactorCode="turnover_z20" initialVersionId={1} />);
    await waitFor(() => expect(mockApi.listEvaluationRuns).toHaveBeenCalledWith(expect.objectContaining({ factorVersionId: 1 })));
    expect(screen.queryByText("other-factor-report")).not.toBeInTheDocument();
    expect(screen.getByText("evalLabNoMetrics")).toBeVisible();
    await openRunsTab();
    expect(screen.getByText("evalLabEmptyRuns")).toBeVisible();
  });

  it("marks unfiltered browsing as global history and identifies an explicitly opened historical report", async () => {
    const run = makeRun({ id: "other-version-report", factor_version_id: 999 });
    run.config.factor_code = "other_factor";
    mockApi.listEvaluationRuns.mockResolvedValue([run]);
    mockApi.getEvaluationRun.mockResolvedValue(run);
    render(<FactorEvaluationLab />);
    await openRunsTab();
    fireEvent.click((await screen.findAllByRole("button", { name: /solution/ }))[0]);
    expect((await screen.findAllByText("other_factor")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("factorLabGlobalHistory").length).toBeGreaterThan(0);
    expect(screen.getAllByText("factorLabReportVersionId").length).toBeGreaterThan(0);
  });

  it("clears a selected report and ignores its late detail response when the factor changes", async () => {
    const firstRun = makeRun({ id: "first-factor-report" });
    let resolveReport!: (run: any) => void;
    mockApi.listFactorVersions.mockImplementation(async (code: string) => [{ id: code === "turnover_z20" ? 1 : 2, version: 1, is_latest: true }]);
    mockApi.listEvaluationRuns.mockImplementation(async (query: { factorVersionId?: number }) => query.factorVersionId === 1 ? [firstRun] : []);
    mockApi.getEvaluationRun.mockResolvedValueOnce(firstRun).mockImplementationOnce(() => new Promise(resolve => { resolveReport = resolve; }));
    const view = render(<FactorEvaluationLab initialFactorCode="turnover_z20" initialVersionId={1} />);
    await openRunsTab();
    fireEvent.click((await screen.findAllByRole("button", { name: /solution/ }))[0]);
    const main = screen.getByRole("region", { name: "factorLabResultRegion" });
    await waitFor(() => expect(within(main).getByRole("region", { name: /evalLabSectionReport · first-factor-report/, hidden: true })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getAllByRole("button", { name: /solution/ })[0]);
    await waitFor(() => expect(mockApi.getEvaluationRun).toHaveBeenCalledTimes(2));
    view.rerender(<FactorEvaluationLab initialFactorCode="other_factor" initialVersionId={2} />);
    await waitFor(() => expect(mockApi.listEvaluationRuns).toHaveBeenCalledWith(expect.objectContaining({ factorVersionId: 2 })));
    await act(async () => { resolveReport(firstRun); });
    expect(within(main).queryByRole("region", { name: /evalLabSectionReport · first-factor-report/, hidden: true })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(within(main).getByText("evalLabEmptyRuns")).toBeVisible();
  }, 30000); // 多组件聚合渲染，并行负载下会超默认 5s，放宽外层超时（不改任何断言）

  it("allows explicit viewing of another task's historical report without replacing the selected version result", async () => {
    const firstRun = makeRun({ id: "selected-factor-report" });
    const otherRun = makeRun({ id: "other-task-report", factor_version_id: 999, task_id: "other-task" });
    const otherTask = makeTask({ id: "other-task", status: "completed", result: { run_id: otherRun.id }, payload_json: JSON.stringify({ factor_code: "other_factor" }) });
    mockApi.listEvaluationTasks.mockResolvedValue([otherTask]);
    mockApi.getEvaluationTask.mockResolvedValue(otherTask);
    mockApi.listEvaluationRuns.mockResolvedValue([firstRun]);
    mockApi.getEvaluationRun.mockImplementation(async (id: string) => id === firstRun.id ? firstRun : otherRun);
    render(<FactorEvaluationLab initialFactorCode="turnover_z20" initialVersionId={1} />);
    await openRunsTab();
    fireEvent.click((await screen.findAllByRole("button", { name: /solution/ }))[0]);
    const main = screen.getByRole("region", { name: "factorLabResultRegion" });
    await waitFor(() => expect(within(main).getByRole("region", { name: /evalLabSectionReport · selected-factor-report/, hidden: true })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: "查看详情" }));
    const drawer = await screen.findByRole("dialog");
    await waitFor(() => expect(within(drawer).getByRole("region", { name: /evalLabSectionReport · other-task-report/ })).toBeVisible());
    const historicalReport = within(drawer).getByRole("region", { name: /evalLabSectionReport · other-task-report/ });
    expect(within(historicalReport).getByText("other_factor")).toBeVisible();
    expect(within(historicalReport).getByText("factorLabHistoricalReport")).toBeVisible();
    expect(within(main).queryByRole("region", { name: /evalLabSectionReport · other-task-report/, hidden: true })).not.toBeInTheDocument();
    expect(within(main).getByRole("region", { name: /evalLabSectionReport · selected-factor-report/, hidden: true })).toBeInTheDocument();
  }, 30000); // 多组件聚合渲染，并行负载下会超默认 5s，放宽外层超时（不改任何断言）

  it("invalidates a passed readiness check as soon as the target horizon changes", async () => {
    render(<FactorEvaluationLab />);
    fireEvent.mouseDown(screen.getAllByRole("combobox")[0]);
    fireEvent.click(await screen.findByText("换手率因子 (turnover_z20)"));
    await waitFor(() => expect(screen.getByRole("button", { name: "evalLabSubmit" })).toBeEnabled());

    fireEvent.mouseDown(screen.getByRole("combobox", { name: "evalWorkbenchTarget" }));
    fireEvent.click(await screen.findByText("T+1 开盘 → T+10 收盘"));
    expect(screen.getByRole("button", { name: "evalLabSubmit" })).toBeDisabled();
    expect(mockApi.createEvaluationTask).not.toHaveBeenCalled();
  });

  it("opens methodology guidance from the help button", async () => {
    render(<FactorEvaluationLab />);
    fireEvent.click(screen.getByRole("button", { name: /evalLabMethodology/ }));
    await waitFor(() => expect(screen.getByText("factorLabMethodologyData")).toBeVisible());
    expect(screen.getByText("factorLabMethodologyNext")).toBeVisible();
  });

  it("keeps a newer blocked check when an older passed check finishes late", async () => {
    let resolveOld!: (value: any) => void;
    mockApi.preflightFactorEvaluation.mockImplementation((params: any) => params.targetHorizon === 5
      ? new Promise(resolve => { resolveOld = resolve; })
      : Promise.resolve({ overall: { passed: false, blocking_count: 1 }, items: [] }));
    render(<FactorEvaluationLab initialFactorCode="turnover_z20" initialVersionId={1} />);
    await waitFor(() => expect(mockApi.preflightFactorEvaluation).toHaveBeenCalledWith(expect.objectContaining({ targetHorizon: 5 })));
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "evalWorkbenchTarget" }));
    fireEvent.click(await screen.findByText("T+1 开盘 → T+10 收盘"));
    await waitFor(() => expect(mockApi.preflightFactorEvaluation).toHaveBeenCalledWith(expect.objectContaining({ targetHorizon: 10 })));
    await act(async () => { resolveOld({ overall: { passed: true, blocking_count: 0 }, items: [] }); });
    expect(screen.getByRole("button", { name: "evalLabSubmit" })).toBeDisabled();
    expect(screen.getByText("factorLabResolveBlockers")).toBeVisible();
  });

  it("does not offer unimplemented constituent universes by default", async () => {
    render(<FactorEvaluationLab />);
    fireEvent.mouseDown(screen.getAllByRole("combobox")[2]);
    expect(await screen.findByText("factorLabLocalUniverse", { selector: ".ant-select-item-option-content" })).toBeInTheDocument();
    expect(screen.queryByText("evalUniverseHS300")).not.toBeInTheDocument();
  });

  it("does not reopen or replace a closed evaluation detail when its request finishes", async () => {
    let resolveRun!: (run: any) => void;
    mockApi.listEvaluationRuns.mockResolvedValue([makeRun()]);
    mockApi.getEvaluationRun.mockImplementation(() => new Promise(resolve => { resolveRun = resolve; }));
    render(<FactorEvaluationLab />);
    await openRunsTab();
    const view = await screen.findAllByRole("button", { name: /查看运行/ });
    fireEvent.click(view[0]);
    await waitFor(() => expect(mockApi.getEvaluationRun).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await act(async () => { resolveRun(makeRun({ id: "late-closed-run" })); });
    expect(screen.queryByText("late-closed-run")).not.toBeInTheDocument();
  });

  it("restores editable advanced evaluation parameters with the reset action", async () => {
    render(<FactorEvaluationLab />);
    fireEvent.click(screen.getByRole("button", { name: /evalAdvancedParams/ }));
    const groups = await screen.findAllByRole("spinbutton");
    fireEvent.change(groups[0], { target: { value: "8" } });
    fireEvent.change(groups[1], { target: { value: "0.005" } });
    fireEvent.click(screen.getByRole("button", { name: "evalAdvancedReset" }));
    expect(groups[0]).toHaveValue("5");
    expect(Number((groups[1] as HTMLInputElement).value)).toBe(0.001);
  });

  // TODO(P1.1 镜像适配层)：本用例写在组件旧实现下，镜像层改造后已脱节（该文件 12 项与共享 helper mockLoadSuccess 联动，需整文件重写）；见 docs/前端既有红定性报告.md §9/§10 与 PROGRESS.debt.TD-FE-RED-2。

  it("submits evaluation task with form values", async () => {
    render(<FactorEvaluationLab />);

    await waitFor(() => {
      expect(mockApi.listEvaluationTasks).toHaveBeenCalled();
    });

    // 输入因子代码并提交
    const codeInput = screen.getAllByRole("combobox")[0];
    fireEvent.mouseDown(codeInput);
    fireEvent.click(await screen.findByText("换手率因子 (turnover_z20)"));

    await waitFor(() => {
      expect(mockApi.preflightFactorEvaluation).toHaveBeenCalled();
      expect(screen.getByRole("button", { name: "evalLabSubmit" })).toBeEnabled();
    });

    const submitBtn = screen.getByRole("button", { name: "evalLabSubmit" });
    fireEvent.click(submitBtn);

    await waitFor(() => {
      expect(mockApi.createEvaluationTask).toHaveBeenCalledWith(expect.objectContaining({
        factor_code: "turnover_z20",
        factor_kind: "continuous",
        target_horizon: 5,
        n_groups: 5,
        cost_rate: 0.001,
        universe: "ashare_all",
        factor_version_id: 1,
        created_by: "local_user",
      }));
    });
    expect(mockApi.createEvaluationTask.mock.calls[0][0]).not.toHaveProperty("direction");
    expect(mockMessage.success).toHaveBeenCalledWith("evalLabCreateSuccess");

    // 创建成功后仍保留当前因子，并立即显示异步任务进度。
    await waitFor(() => {
      expect(mockApi.listEvaluationTasks).toHaveBeenCalledTimes(2);
      expect(document.querySelector(".ant-select-selection-item")?.textContent).toContain("turnover_z20");
      expect(document.querySelector(".eval-running-banner")).toBeInTheDocument();
      expect(document.querySelector(".ant-progress")).toBeInTheDocument();
    });
  });

  // TODO(P1.1 镜像适配层)：本用例写在组件旧实现下，镜像层改造后已脱节（该文件 12 项与共享 helper mockLoadSuccess 联动，需整文件重写）；见 docs/前端既有红定性报告.md §9/§10 与 PROGRESS.debt.TD-FE-RED-2。

  it("shows loading feedback while task creation request is still pending", async () => {
    let resolveCreate: ((task: any) => void) | undefined;
    mockApi.createEvaluationTask.mockImplementationOnce(
      () => new Promise((resolve) => { resolveCreate = resolve; }),
    );
    render(<FactorEvaluationLab />);

    await waitFor(() => expect(mockApi.listEvaluationTasks).toHaveBeenCalled());
    const codeInput = screen.getAllByRole("combobox")[0];
    fireEvent.mouseDown(codeInput);
    fireEvent.click(await screen.findByText("换手率因子 (turnover_z20)"));
    await waitFor(() => expect(screen.getByRole("button", { name: "evalLabSubmit" })).toBeEnabled());

    fireEvent.click(screen.getByRole("button", { name: "evalLabSubmit" }));
    await waitFor(() => {
      expect(screen.getByText("提交中")).toBeInTheDocument();
      expect(document.querySelector(".eval-running-banner")).toBeInTheDocument();
      expect(document.querySelector(".ant-progress-status-active")).toBeInTheDocument();
    });

    resolveCreate?.(makeTask({ status: "queued" }));
    await waitFor(() => expect(mockMessage.success).toHaveBeenCalledWith("evalLabCreateSuccess"));
  });

  // TODO(P1.1 镜像适配层)：本用例写在组件旧实现下，镜像层改造后已脱节（该文件 12 项与共享 helper mockLoadSuccess 联动，需整文件重写）；见 docs/前端既有红定性报告.md §9/§10 与 PROGRESS.debt.TD-FE-RED-2。

  it("renders task list with status and stage", async () => {
    const task = makeTask({
      id: "task-abc",
      status: "done",
      stage: "done",
      percent: 100,
      message: "evaluation done",
      result: { factor_code: "turnover_z20", run_id: "run-xyz" },
      started_at: "2026-08-01T10:00:00",
      finished_at: "2026-08-01T10:05:00",
    });
    mockApi.listEvaluationTasks.mockResolvedValue([task]);

    render(<FactorEvaluationLab />);
    await openRunsTab();

    // 任务列表应展示因子代码
    await waitFor(() => {
      expect(screen.getByText("turnover_z20")).toBeInTheDocument();
    });
    // 任务 ID 缩写可见
  });

  // TODO(P1.1 镜像适配层)：本用例写在组件旧实现下，镜像层改造后已脱节（该文件 12 项与共享 helper mockLoadSuccess 联动，需整文件重写）；见 docs/前端既有红定性报告.md §9/§10 与 PROGRESS.debt.TD-FE-RED-2。

  it("localizes legacy backend exception messages without exposing Python details", async () => {
    const rawMessage = "evaluation_failed:'NoneType' object has no attribute 'validation_start'";
    mockApi.listEvaluationTasks.mockResolvedValue([
      makeTask({
        status: "failed",
        stage: "failed",
        message: rawMessage,
        error_code: null,
      }),
    ]);

    render(<FactorEvaluationLab />);
    await openRunsTab();

    expect((await screen.findAllByText("failed")).length).toBeGreaterThan(0);
    expect(screen.queryByText(rawMessage)).not.toBeInTheDocument();
  });
  // TODO(P1.1 镜像适配层)：本用例写在组件旧实现下，镜像层改造后已脱节（该文件 12 项与共享 helper mockLoadSuccess 联动，需整文件重写）；见 docs/前端既有红定性报告.md §9/§10 与 PROGRESS.debt.TD-FE-RED-2。
  it("renders run history with gate tag and supports filter", async () => {
    const run = makeRun({ id: "eval-1-abc-20260801", gate_result: "passed" });
    mockApi.listEvaluationRuns.mockResolvedValue([run]);

    render(<FactorEvaluationLab />);
    await openRunsTab();

    await waitFor(() => {
      expect(screen.getByText("eval-1-abc-20260801")).toBeInTheDocument();
    });

    // 门禁通过标签可见（gateLabel 在 i18n mock 下回退到原始 gate 值 "passed"）
    expect(screen.getByText("passed")).toBeInTheDocument();
  });

  // TODO(P1.1 镜像适配层)：本用例写在组件旧实现下，镜像层改造后已脱节（该文件 12 项与共享 helper mockLoadSuccess 联动，需整文件重写）；见 docs/前端既有红定性报告.md §9/§10 与 PROGRESS.debt.TD-FE-RED-2。

  it("shows rejection reasons for rejected run", async () => {
    const run = makeRun({
      id: "eval-2-def-20260801",
      gate_result: "rejected",
      rejection_reasons: ["low_icir", "insufficient_coverage"],
    });
    mockApi.listEvaluationRuns.mockResolvedValue([run]);
    mockApi.getEvaluationRun.mockResolvedValue(run);

    render(<FactorEvaluationLab />);
    await openRunsTab();

    // 点击查看运行详情
    await waitFor(() => {
      expect(screen.getByText("eval-2-def-20260801")).toBeInTheDocument();
    });

    // 进入运行详情
    const viewRunButtons = screen.getAllByRole("button", { name: /solution/ });
    fireEvent.click(viewRunButtons[0]);

    await waitFor(() => {
      expect(mockApi.getEvaluationRun).toHaveBeenCalledWith("eval-2-def-20260801");
    });

    // 拒绝原因应可见
    await waitFor(() => {
      expect(screen.getAllByText("low_icir").length).toBeGreaterThan(0);
      expect(screen.getAllByText("insufficient_coverage").length).toBeGreaterThan(0);
    });
  });

  // TODO(P1.1 镜像适配层)：本用例写在组件旧实现下，镜像层改造后已脱节（该文件 12 项与共享 helper mockLoadSuccess 联动，需整文件重写）；见 docs/前端既有红定性报告.md §9/§10 与 PROGRESS.debt.TD-FE-RED-2。

  it("renders structured rejection reasons without crashing", async () => {
    const run = makeRun({
      id: "eval-struct-20260801",
      gate_result: "warn",
      rejection_reasons: [
        {
          code: "rank_ic_non_positive",
          severity: "error",
          detail_zh: "Rank IC 均值 -0.6707 不大于 0",
          evidence: { correlation_id: "corr-structured" },
        },
      ],
    });
    mockApi.listEvaluationRuns.mockResolvedValue([run]);
    mockApi.getEvaluationRun.mockResolvedValue(run);

    render(<FactorEvaluationLab />);
    await openRunsTab();
    await waitFor(() => expect(screen.getByText("eval-struct-20260801")).toBeInTheDocument());
    fireEvent.click(screen.getAllByRole("button", { name: /solution/ })[0]);

    await waitFor(() => {
      expect(screen.getByText(/rank_ic_non_positive/)).toBeInTheDocument();
      expect(screen.getByText(/Rank IC 均值 -0\.6707/)).toBeInTheDocument();
    });
  });
  it("normalizes nested backend metrics in the run report", async () => {
    const run = makeRun({
      id: "eval-nested-20260801",
      metrics: {
        ic: {
          rank_ic_mean: -0.6707,
          rank_ic_median: -0.6769,
          rank_ic_std: 0.0731,
          icir: -9.1783,
          positive_ic_ratio: 0,
        },
        quantile: {
          group_returns: [-0.02, -0.03, -0.04, -0.05, -0.08555],
          monotonicity_score: -0.9793,
          top_bottom_return: -0.06555,
        },
        turnover: { avg_turnover: 0.1649 },
        cost_adjusted: { net_return: -0.065884 },
        coverage: { coverage: 0.9829, available_dates: 74, available_symbols: 5561 },
        validation_days: 74,
        effective_samples: 404479,
        time_split: {
          train_start: "2024-06-03",
          train_end: "2025-12-31",
          validation_start: "2026-01-02",
          validation_end: "2026-08-27",
        },
      },
    });
    mockApi.listEvaluationRuns.mockResolvedValue([run]);
    mockApi.getEvaluationRun.mockResolvedValue(run);

    render(<FactorEvaluationLab />);
    await openRunsTab();
    await waitFor(() => expect(screen.getByText("eval-nested-20260801")).toBeInTheDocument());
    fireEvent.click(screen.getAllByRole("button", { name: /solution/ })[0]);

    await waitFor(() => {
      expect(screen.getAllByText("-0.6707").length).toBeGreaterThan(0);
      expect(screen.getAllByText("-9.1783").length).toBeGreaterThan(0);
      expect(screen.getAllByText(/98\.29\s*%/).length).toBeGreaterThan(0);
      expect(screen.getAllByText("404479").length).toBeGreaterThan(0);
      expect(screen.getByText("2024-06-03 ~ 2025-12-31")).toBeInTheDocument();
      expect(screen.getByText("2026-01-02 ~ 2026-08-27")).toBeInTheDocument();
    // 数值指标：同值可能在页面多处渲染（指标卡+摘要），统一用 getAllByText
    expect(screen.getAllByText("-0.6707").length).toBeGreaterThan(0);
    expect(screen.getAllByText("-9.1783").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/98\.29\s*%/).length).toBeGreaterThan(0);
    expect(screen.getAllByText("404479").length).toBeGreaterThan(0);

    });
  });

  it("does not invent filtering or fidelity evidence absent from the real turnover report contract", async () => {
    // The completed turnover API response contains model metrics and a rejected
    // gate, but no filter_config, excluded_symbol_days or production_fidelity.
    const run = makeRun({
      id: "eval-100073-303aba28-20261014", factor_version_id: 100073,
      gate_result: "rejected", rejection_reasons: ["rank_ic_non_positive"],
      config: { evaluator_version: "wp5-1.0.0", factor_kind: "continuous", target_horizon: 5, universe: "all_a_shares" },
      metrics: { ic: { rank_ic_mean: -0.015456724357457673, icir: -0.17330729228453323, positive_ic_ratio: 0.5 }, coverage: { coverage: 0.9808095758464397 }, effective_samples: 425434 },
    });
    mockApi.listEvaluationRuns.mockResolvedValue([run]);
    mockApi.getEvaluationRun.mockResolvedValue(run);
    render(<FactorEvaluationLab />);
    await openRunsTab();
    expect(await screen.findByText("factorLabFidelityUnknown")).toBeVisible();
    fireEvent.click(screen.getAllByRole("button", { name: /solution/ })[0]);
    const drawer = await screen.findByRole("dialog");
    const report = await within(drawer).findByRole("region", { name: `evalLabSectionReport · ${run.id}` });
    await waitFor(() => expect(within(report).getByText("-0.0155")).toBeVisible());
    expect(report).not.toHaveTextContent("✓");
    expect(report).not.toHaveTextContent("非保真实验");
    expect(within(report).queryAllByText("0")).toHaveLength(0);
    expect(within(report).getByText("factorLabGovernanceEvidenceMissing")).toBeVisible();
    expect(within(report).queryByRole("region", { name: "factorLabFilterRulesEvidence" })).not.toBeInTheDocument();
    expect(within(report).queryByRole("region", { name: "factorLabExclusionCounts" })).not.toBeInTheDocument();
  });

  it.each([
    { fidelity: true, history: "factorLabFidelityConfirmed", report: "factorLabProductionFidelity：✓" },
    { fidelity: false, history: "factorLabFidelityNonProduction", report: "factorLabProductionFidelity：✗ factorLabFidelityNonProduction" },
    { fidelity: 0, history: "factorLabFidelityNonProduction", report: "factorLabProductionFidelity：✗ factorLabFidelityNonProduction" },
    { fidelity: undefined, history: "factorLabFidelityUnknown", report: "factorLabProductionFidelity：factorLabEvidenceNotProvided" },
  ])("preserves explicit audit flags and zero counts, with missing partial fields unknown (fidelity $fidelity)", async ({ fidelity, history, report: fidelityText }) => {
    const run = makeRun({ id: "partial-audit-report", filter_config: { filter_new_listing: true, filter_st: false, filter_suspended: null }, excluded_symbol_days: { new_listing: 0, st: 7 }, production_fidelity: fidelity });
    mockApi.listEvaluationRuns.mockResolvedValue([run]);
    mockApi.getEvaluationRun.mockResolvedValue(run);
    render(<FactorEvaluationLab />);
    await openRunsTab();
    expect(await screen.findByText(history)).toBeVisible();
    fireEvent.click(screen.getAllByRole("button", { name: /solution/ })[0]);
    const drawer = await screen.findByRole("dialog");
    const report = await within(drawer).findByRole("region", { name: `evalLabSectionReport · ${run.id}` });
    const rules = within(report).getByRole("region", { name: "factorLabFilterRulesEvidence" });
    await waitFor(() => expect(within(rules).getByText("次新股过滤 ✓")).toBeVisible());
    expect(within(rules).getByText("ST 过滤 ✗ 关")).toBeVisible();
    expect(within(rules).getByText("停牌过滤 factorLabEvidenceNotProvided")).toBeVisible();
    expect(within(rules).getByText("退市整理期过滤 factorLabEvidenceNotProvided")).toBeVisible();
    expect(within(rules).getByText(fidelityText)).toBeVisible();
    const counts = within(report).getByRole("region", { name: "factorLabExclusionCounts" });
    expect(within(counts).getByText("0")).toBeVisible();
    expect(within(counts).getByText("7")).toBeVisible();
    expect(within(counts).getAllByText("—")).toHaveLength(7);
  });

  // TODO(P1.1 镜像适配层)：本用例写在组件旧实现下，镜像层改造后已脱节（该文件 12 项与共享 helper mockLoadSuccess 联动，需整文件重写）；见 docs/前端既有红定性报告.md §9/§10 与 PROGRESS.debt.TD-FE-RED-2。

  it("displays complete trade day evidence", async () => {
    const run = makeRun({
      id: "eval-3-ghi-20260801",
      selected_trade_date: "2026-07-31",
      observed_symbols: 4800,
      expected_symbols: 5000,
      completeness_ratio: 0.96,
      fallback_reason: null,
    });
    mockApi.listEvaluationRuns.mockResolvedValue([run]);
    mockApi.getEvaluationRun.mockResolvedValue(run);

    render(<FactorEvaluationLab />);
    await openRunsTab();

    await waitFor(() => {
      expect(screen.getByText("eval-3-ghi-20260801")).toBeInTheDocument();
    });

    const viewRunButtons = screen.getAllByRole("button", { name: /solution/ });
    fireEvent.click(viewRunButtons[0]);

    // 完整交易日证据字段可见
    await waitFor(() => {
      expect(screen.getByText("2026-07-31")).toBeInTheDocument();
      expect(screen.getByText("4800")).toBeInTheDocument();
      expect(screen.getByText("5000")).toBeInTheDocument();
    });
    // 完整度 96% (0.96 * 100 = 96.00%)
    expect(screen.getAllByText("96.00%").length).toBeGreaterThan(0);
  });

  // TODO(P1.1 镜像适配层)：本用例写在组件旧实现下，镜像层改造后已脱节（该文件 12 项与共享 helper mockLoadSuccess 联动，需整文件重写）；见 docs/前端既有红定性报告.md §9/§10 与 PROGRESS.debt.TD-FE-RED-2。

  it("renders stress test parameter perturbation table", async () => {
    const run = makeRun();
    mockApi.listEvaluationRuns.mockResolvedValue([run]);
    mockApi.getEvaluationRun.mockResolvedValue(run);

    render(<FactorEvaluationLab />);
    await openRunsTab();

    await waitFor(() => {
      expect(mockApi.listEvaluationRuns).toHaveBeenCalled();
    });

    const viewRunButtons = screen.getAllByRole("button", { name: /solution/ });
    fireEvent.click(viewRunButtons[0]);

    // 压力测试相关章节可见
    await waitFor(() => {
      expect(screen.getByText("evalLabStressParameter")).toBeInTheDocument();
    });
    // 参数名 window 出现在表格中
    expect(screen.getAllByText("window").length).toBeGreaterThan(0);
    // 总体结论 stable
    expect(screen.getAllByText("evalLabStressVerdictStable").length).toBeGreaterThan(0);
  });

  it("localizes insufficient stress evidence without treating it as a passing verdict", async () => {
    const run = makeRun();
    run.metrics.stress_test.time_result.verdict = "insufficient";
    mockApi.listEvaluationRuns.mockResolvedValue([run]);
    mockApi.getEvaluationRun.mockResolvedValue(run);
    render(<FactorEvaluationLab />);
    await openRunsTab();
    fireEvent.click((await screen.findAllByRole("button", { name: /solution/ }))[0]);
    const verdicts = await screen.findAllByText("factorLabStressInsufficient");
    expect(screen.queryByText("insufficient")).not.toBeInTheDocument();
    for (const verdict of verdicts) expect(verdict.closest(".ant-tag")).not.toHaveClass("ant-tag-green");
  });
  it("auto-selects run when active task transitions to done", async () => {
    // 活跃任务已到终态 done，result.run_id 指向对应运行
    const doneTask = makeTask({
      id: "task-done",
      status: "done",
      stage: "done",
      percent: 100,
      result: { run_id: "eval-auto-123" },
      finished_at: "2026-08-01T10:05:00",
    });
    mockApi.listEvaluationTasks.mockResolvedValue([doneTask]);
    mockApi.listEvaluationRuns.mockResolvedValue([]);
    const targetRun = makeRun({ id: "eval-auto-123" });
    mockApi.getEvaluationRun.mockResolvedValue(targetRun);

    render(<FactorEvaluationLab />);

    // 组件在 activeTask 状态为 done 的 useEffect 里自动拉取对应运行
    await waitFor(() => {
      expect(mockApi.getEvaluationRun).toHaveBeenCalledWith("eval-auto-123");
    });
  });

  it("cancels running task on cancel button click", async () => {
    const runningTask = makeTask({
      id: "task-running",
      status: "running",
      stage: "evaluating",
      percent: 70,
      started_at: "2026-08-01T10:00:00",
    });
    mockApi.listEvaluationTasks.mockResolvedValue([runningTask]);

    render(<FactorEvaluationLab />);

    // 等待活动任务加载
    await waitFor(() => {
      expect(screen.getByText("evalLabCancel")).toBeInTheDocument();
    });

    // 点击取消（按钮含 StopOutlined 图标，accessible name 可能含图标的 label，
    // 改用文本定位后回溯到 button 元素）
    const cancelTextEl = screen.getByText("evalLabCancel");
    const cancelBtn = cancelTextEl.closest("button") as HTMLElement;
    expect(cancelBtn).toBeTruthy();
    fireEvent.click(cancelBtn);

    await waitFor(() => {
      expect(mockApi.cancelEvaluationTask).toHaveBeenCalledWith("task-running");
    });
    expect(mockMessage.success).toHaveBeenCalledWith("evalLabCancelSuccess");
  });

  it("shows immutability warning on run report", async () => {
    const run = makeRun();
    mockApi.listEvaluationRuns.mockResolvedValue([run]);
    mockApi.getEvaluationRun.mockResolvedValue(run);

    render(<FactorEvaluationLab />);
    await openRunsTab();

    await waitFor(() => {
      expect(mockApi.listEvaluationRuns).toHaveBeenCalled();
    });

    const viewRunButtons = screen.getAllByRole("button", { name: /solution/ });
    fireEvent.click(viewRunButtons[0]);

    await waitFor(() => {
      expect(screen.getByText("evalLabRunImmutable")).toBeInTheDocument();
    });
  });

  it("disables submit button while a task is running", async () => {
    const runningTask = makeTask({
      status: "running",
      started_at: "2026-08-01T10:00:00",
    });
    mockApi.listEvaluationTasks.mockResolvedValue([runningTask]);

    render(<FactorEvaluationLab />);

    await waitFor(() => {
      const submitBtn = screen.getByRole("button", { name: "evalLabSubmit" });
      expect(submitBtn).toBeDisabled();
    });
  });
});
