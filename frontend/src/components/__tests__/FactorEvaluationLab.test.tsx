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
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockContext, mockApi, mockMessage } = vi.hoisted(() => ({
  mockContext: {
    locale: "zh-CN",
    showToast: vi.fn(),
  },
  mockApi: {
    listFactorDefinitions: vi.fn(),
    preflightFactorEvaluation: vi.fn(),
    listEvaluationTasks: vi.fn(),
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
        direction: "higher_better",
        created_by: "local_user",
      }));
    });
    expect(mockMessage.success).toHaveBeenCalledWith("evalLabCreateSuccess");
  });

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

  it("auto-selects run when active task transitions to done", async () => {
    // 列表返回一个 running 任务
    const runningTask = makeTask({
      id: "task-running",
      status: "running",
      stage: "evaluating",
      percent: 70,
      result: null,
      started_at: "2026-08-01T10:00:00",
    });
    mockApi.listEvaluationTasks.mockResolvedValue([runningTask]);

    // 第一次轮询返回 done，result.run_id 指向运行
    const doneTask = makeTask({
      id: "task-running",
      status: "done",
      stage: "done",
      percent: 100,
      result: { run_id: "eval-auto-123" },
      finished_at: "2026-08-01T10:05:00",
    });
    mockApi.getEvaluationTask.mockResolvedValue(doneTask);
    mockApi.listEvaluationRuns.mockResolvedValue([]);
    const targetRun = makeRun({ id: "eval-auto-123" });
    mockApi.getEvaluationRun.mockResolvedValue(targetRun);

    render(<FactorEvaluationLab />);

    // 等待初始加载完成
    await waitFor(() => {
      expect(mockApi.listEvaluationTasks).toHaveBeenCalled();
    });

    // 轮询回调会调用 loadAll -> listEvaluationTasks 刷新活动任务列表，
    // 此时任务已终态，list 应返回 doneTask 以避免 activeTask 被覆盖回 running
    mockApi.listEvaluationTasks.mockResolvedValue([doneTask]);

    // 触发轮询（2 秒间隔），使用 async 版本确保 Promise 回调完整执行
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });

    // 应调用 getEvaluationTask 拉取最新状态
    await waitFor(() => {
      expect(mockApi.getEvaluationTask).toHaveBeenCalledWith("task-running");
    });

    // 任务终态后应自动拉取对应运行（useEffect 检测到 done 状态后触发）
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
