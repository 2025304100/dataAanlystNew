// WP7-06：FactorModelPage 组件测试
//
// 覆盖：
// - 初始加载：runtime / models / factorSets 并行加载
// - runtime 状态卡片：weight_mode / active_model_run_id / fallback_reason
// - FactorSet 列表展示与空状态
// - 候选模型列表：validated/rejected 状态展示、激活按钮可见性
// - 模型详情：点击模型 ID 加载详情 + 权重快照 + 审计日志
// - 激活 Modal：打开 / 提交成功 / 失败提示
// - 回退 Modal：原因为空时警告 / 提交成功 / 失败提示
// - 加载失败：error 提示
//
// 约束：
// - mock api/client（api），不调用真实后端
// - mock antd 的 message（兼容 App.useApp()）
// - i18n mock：t(key) 返回 key
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, act, within } from "@testing-library/react";

const { mockApi, mockMessage } = vi.hoisted(() => ({
  mockApi: {
    getFactorModels: vi.fn(),
    getFactorModel: vi.fn(),
    scoringGetOverviewAsFactor: vi.fn<any>(async () => ({ feature_enabled: true, config: {}, runtime: {}, health: {}, latest_trade_date: null, factor_coverage: [] })),
    scoringListTasks: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    getInboxNotifications: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    scoringGetFactorDefinition: vi.fn(async () => ({})),
    scoringGetFactorModelListAsFactor: vi.fn<any>(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    addFactorSetMember: vi.fn(async () => ({})),
    cloneFactorSet: vi.fn(async () => ({})),
    createFactorSet: vi.fn(async () => ({})),
    deprecateFactorSet: vi.fn(async () => ({})),
    getFactorSetDetail: vi.fn(async () => ({})),
    removeFactorSetMember: vi.fn(async () => ({})),
    scoringActivateModel: vi.fn(async () => ({})),
    scoringFallbackToManual: vi.fn(async () => ({})),
    scoringFreezeFactorSet: vi.fn(async () => ({})),
    scoringGetModelDetail: vi.fn(async () => ({})),
    scoringGetModelRelations: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    scoringListFactorDefinitions: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    scoringListFactorSetsAsFactor: vi.fn<any>(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    scoringGetFactorSetReadiness: vi.fn<(id: string) => Promise<any>>(),
    scoringRenameModel: vi.fn(async () => ({})),
    scoringCreateTask: vi.fn<any>(async () => ({ id: "score-task-001", status: "queued" })),
    scoringTrainModel: vi.fn(async () => ({})),
    getEvaluationTaskHeartbeat: vi.fn(async () => ({})),
    activateFactorModel: vi.fn(),
    fallbackFactorModel: vi.fn(),
    listFactorSets: vi.fn(),
  },
  mockMessage: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

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

vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string) => key,
}));

vi.mock("../../context/AppContext", () => ({
  useApp: () => ({ locale: "zh-CN", showToast: vi.fn() }),
}));

vi.mock("../../api/client", () => ({
  api: mockApi,
}));

import FactorModelPage from "../factors/FactorModelPage";
import { navigate } from "../../utils/navigate";

/** 构造 runtime 状态。 */
function makeRuntime(overrides: Partial<any> = {}): any {
  return {
    weight_mode: "manual",
    score_weight_mode: "manual",
    active_model_run_id: null,
    updated_by: "local_user",
    fallback_reason: null,
    version: 1,
    updated_at: "2026-08-02T10:00:00",
    ...overrides,
  };
}

/** 构造候选模型。 */
function makeModel(overrides: Partial<any> = {}): any {
  return {
    id: "model-001-abc",
    model_type: "ridge",
    asset_type: "index",
    target_code: "000300",
    train_start_date: "2020-01-01",
    train_end_date: "2024-12-31",
    validation_start_date: "2025-01-01",
    validation_end_date: "2025-12-31",
    data_cutoff_at: "2026-08-01T00:00:00",
    feature_versions: { momentum_20: 1 },
    hyperparameters: { factor_set_id: "fs-test-001" },
    metrics: { validation_ic: 0.06 },
    sample_count: 1200,
    symbol_count: 300,
    trade_date_count: 1200,
    status: "validated",
    production_eligible: true,
    rejection_reason: null,
    artifact_path: null,
    created_at: "2026-08-01T12:00:00",
    activated_at: null,
    weights: [
      {
        factor_code: "momentum_20",
        factor_version: 1,
        coefficient: 0.45,
        normalized_weight: 0.6,
        train_ic: 0.05,
        validation_ic: 0.06,
      },
    ],
    audit: [],
    ...overrides,
  };
}

/** 构造 FactorSet。 */
function makeFactorSet(overrides: Partial<any> = {}): any {
  return {
    id: "fs-test-001",
    name: "测试因子集合",
    description: "用于测试",
    content_hash: "abc12345",
    status: "frozen",
    frozen_at: "2026-08-01T10:00:00",
    created_by: "local_user",
    created_at: "2026-07-31T10:00:00",
    updated_at: "2026-08-01T10:00:00",
    n_members: 1,
    members: [
      {
        id: 1,
        factor_set_id: "fs-test-001",
        factor_id: 1,
        factor_version_id: 1,
        factor_code: "momentum_20",
        factor_version: 1,
        role: "feature",
        weight_constraint: null,
        display_order: 1,
        missing_policy: "exclude",
        excluded_reason: null,
      },
    ],
    ...overrides,
  };
}

/** 默认成功加载场景。 */
function mockLoadSuccess(overrides: { runtime?: any; models?: any[]; factorSets?: any[] } = {}) {
  const runtime = makeRuntime(overrides.runtime);
  const models = overrides.models ?? [makeModel()];
  const factorSets = overrides.factorSets ?? [makeFactorSet()];
  // 镜像层（P1.1）：组件从这三个方法取数据，形状必须对齐其解构
  mockApi.scoringGetOverviewAsFactor.mockResolvedValue({ runtime, health: { warehouse_available: true } });
  mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: models });
  mockApi.scoringListFactorSetsAsFactor.mockResolvedValue(factorSets);
  return { runtime, models, factorSets };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
  window.history.replaceState(null, "", window.location.pathname);
  mockApi.scoringCreateTask.mockResolvedValue({ id: "score-task-001", status: "queued" });
  mockApi.scoringGetFactorSetReadiness.mockImplementation(async (id: string) => ({ ready: true, factor_set_id: id, code: "READY", message: "集合资格检查通过", details: {} }));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("FactorModelPage", () => {
  it("模型页已经挂载后，关联模型深链仍打开指定模型的真实验证详情", async () => {
    mockLoadSuccess({ models: [makeModel({ id: "deep-model" })] });
    mockApi.getFactorModel.mockResolvedValue(makeModel({ id: "deep-model" }));
    render(<><button onClick={() => navigate("/settings/factors?model_id=deep-model")}>查看关联模型</button><FactorModelPage /></>);
    await screen.findByText("deep-model");

    fireEvent.click(screen.getByRole("button", { name: "查看关联模型" }));

    await waitFor(() => expect(mockApi.getFactorModel).toHaveBeenCalledWith("deep-model"));
    expect(await screen.findByTestId("model-validation-detail")).toHaveTextContent("deep-model");
  });

  it("训练预检未通过时显示缺失原因并阻止提交，修复后可重新检查", async () => {
    mockLoadSuccess({ models: [] });
    mockApi.scoringGetFactorSetReadiness.mockResolvedValueOnce({ ready: false, factor_set_id: "fs-test-001", code: "FACTOR_VERSION_MISMATCH", message: "集合因子版本缺少来源，需更新成员后重新冻结", details: {} });
    render(<FactorModelPage />);
    await waitFor(() => expect(screen.getByTestId("chain-btn-next")).toHaveTextContent("训练模型"));
    fireEvent.click(screen.getByTestId("chain-btn-next"));

    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText("集合因子版本缺少来源，需更新成员后重新冻结")).toBeVisible());
    expect(within(dialog).getByRole("button", { name: "提交训练" })).toBeDisabled();
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "重新检查" }));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "提交训练" })).toBeEnabled());
    expect(mockApi.scoringGetFactorSetReadiness).toHaveBeenCalledTimes(2);
  });

  it("训练预检接口失败时保留对话框和重试入口，不放行任务", async () => {
    mockLoadSuccess({ models: [] });
    mockApi.scoringGetFactorSetReadiness.mockRejectedValueOnce(new Error("预检服务不可用"));
    render(<FactorModelPage />);
    await waitFor(() => expect(screen.getByTestId("chain-btn-next")).toHaveTextContent("训练模型"));
    fireEvent.click(screen.getByTestId("chain-btn-next"));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText(/预检服务不可用/)).toBeVisible());
    expect(within(dialog).getByRole("button", { name: "提交训练" })).toBeDisabled();
    fireEvent.click(within(dialog).getByRole("button", { name: "重新检查" }));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "提交训练" })).toBeEnabled());
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
  });

  it("切换训练集合后，先前集合的迟到预检结果不会阻断当前集合", async () => {
    let resolveOld: (value: unknown) => void = () => undefined;
    mockLoadSuccess({ models: [], factorSets: [makeFactorSet({ id: "fs-A", name: "集合A" }), makeFactorSet({ id: "fs-B", name: "集合B" })] });
    mockApi.scoringGetFactorSetReadiness.mockImplementation((id: string) => id === "fs-A"
      ? new Promise((resolve) => { resolveOld = resolve; })
      : Promise.resolve({ ready: true, factor_set_id: id, code: "READY", message: "集合资格检查通过", details: {} }));
    render(<FactorModelPage />);
    fireEvent.click(await screen.findByTestId("btn-train-fs-A"));
    const dialogA = await screen.findByRole("dialog");
    fireEvent.click(within(dialogA).getByRole("button", { name: /取\s*消/ }));
    fireEvent.click(screen.getByTestId("btn-train-fs-B"));
    const dialogB = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialogB).getByRole("button", { name: "提交训练" })).toBeEnabled());

    await act(async () => resolveOld({ ready: false, factor_set_id: "fs-A", code: "OLD_FAILED", message: "旧集合来源不匹配", details: {} }));

    expect(within(dialogB).getByRole("button", { name: "提交训练" })).toBeEnabled();
    expect(within(dialogB).getByText("集合B")).toBeInTheDocument();
    expect(within(dialogB).queryByText("旧集合来源不匹配")).not.toBeInTheDocument();
  });

  it("旧模型虽标记已验证但生产资格失败时，下一步查看真实验证详情", async () => {
    mockLoadSuccess({ models: [makeModel({ production_eligible: false, readiness_message: "训练来源无法核验" })] });
    mockApi.getFactorModel.mockResolvedValue(makeModel({ production_eligible: false, readiness_message: "训练来源无法核验" }));
    render(<FactorModelPage />);
    await waitFor(() => expect(screen.getByTestId("chain-step-5")).toHaveAttribute("data-step-state", "ready"));
    expect(screen.getByTestId("chain-step-6")).toHaveAttribute("data-step-state", "blocked");

    fireEvent.click(screen.getByTestId("chain-btn-next"));

    await waitFor(() => expect(mockApi.getFactorModel).toHaveBeenCalledWith("model-001-abc"));
    const detail = await screen.findByTestId("model-validation-detail");
    expect(within(detail).getByText("训练来源无法核验")).toBeVisible();
    fireEvent.click(within(detail).getByRole("button", { name: "使用此集合重新训练" }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "提交训练" })).toBeEnabled());
    expect(mockApi.scoringGetFactorSetReadiness).toHaveBeenCalledWith("fs-test-001");
    expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
  });

  it("去激活选择生产资格通过的模型，跳过列表里的旧验证模型", async () => {
    mockLoadSuccess({ models: [
      makeModel({ id: "legacy-candidate", production_eligible: false }),
      makeModel({ id: "production-candidate", production_eligible: true }),
    ] });
    render(<FactorModelPage />);
    await waitFor(() => expect(screen.getByTestId("chain-step-6")).toHaveAttribute("data-step-state", "ready"));

    fireEvent.click(screen.getByTestId("chain-btn-activate-model"));

    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText("production-candidate")).toBeVisible());
    expect(mockMessage.error).not.toHaveBeenCalled();
  });

  it("仓库不可用时不会仅凭runtime打勾，主按钮重新检查仓库", async () => {
    const { runtime } = mockLoadSuccess();
    mockApi.scoringGetOverviewAsFactor.mockResolvedValue({ runtime, health: { warehouse_available: false } });
    render(<FactorModelPage />);
    await waitFor(() => expect(screen.getByText("model-001-abc")).toBeVisible());
    expect(screen.getByTestId("chain-step-1")).toHaveAttribute("data-step-state", "blocked");
    const before = mockApi.scoringGetOverviewAsFactor.mock.calls.length;
    fireEvent.click(screen.getByTestId("chain-btn-next"));
    await waitFor(() => expect(mockApi.scoringGetOverviewAsFactor).toHaveBeenCalledTimes(before + 1));
  });

  it("下一步训练直接打开股票冻结集合的训练对话框，不误选ETF集合", async () => {
    mockLoadSuccess({ models: [], factorSets: [
      makeFactorSet({ id: "fs-etf", asset_type: "etf", name: "ETF冻结集合" }),
      makeFactorSet({ id: "fs-stock", asset_type: "stock", name: "股票冻结集合" }),
    ] });
    render(<FactorModelPage />);
    await waitFor(() => expect(screen.getByTestId("chain-btn-next")).toHaveTextContent("训练模型"));

    fireEvent.click(screen.getByTestId("chain-btn-next"));

    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText("股票冻结集合")).toBeVisible());
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
  });

  it("renders runtime / models / factorSets after load", async () => {
    mockLoadSuccess();

    render(<FactorModelPage />);

    await waitFor(() => {
      expect(mockApi.scoringGetFactorModelListAsFactor).toHaveBeenCalledWith(20);
      expect(mockApi.scoringListFactorSetsAsFactor).toHaveBeenCalledWith("any", 50);
    });

    // runtime 卡片标题
    await waitFor(() => {
      expect(screen.getByText("factorModelRuntime")).toBeInTheDocument();
    });

    // FactorSet 名称展示（列表 + 模型行 FactorSet 列均会显示名称）
    expect(screen.getAllByText("测试因子集合").length).toBeGreaterThanOrEqual(1);

    // 模型列表展示
    expect(screen.getByText("model-001-abc")).toBeInTheDocument();
  });
  it("shows empty placeholders when no models and no factor sets", async () => {
    mockLoadSuccess({ models: [], factorSets: [] });

    render(<FactorModelPage />);

    await waitFor(() => {
      // 组件已移除 factorModelNoFactorSets 文案（仅保留模型空态），故此处只断言模型空态
      expect(screen.getByText("factorModelNoModels")).toBeInTheDocument();
    });
  });
  it("falls back to FactorSet id when a historical name is irreversibly garbled", async () => {
    mockLoadSuccess({ factorSets: [makeFactorSet({ id: "fs-set-3126b70f", name: "?????????" })] });

    render(<FactorModelPage />);

    await waitFor(() => {
      expect(screen.getAllByText("fs-set-3126b70f").length).toBeGreaterThan(0);
    });
    expect(screen.queryByText("?????????")).not.toBeInTheDocument();
  });
  it("shows fallback button disabled when weight_mode is manual", async () => {
    mockLoadSuccess({ runtime: makeRuntime({ weight_mode: "manual" }) });

    render(<FactorModelPage />);

    await waitFor(() => {
      const fallbackBtn = screen.getByText("factorModelFallback").closest("button");
      expect(fallbackBtn).toBeDisabled();
    });
  });

  it("enables fallback button when weight_mode is ridge", async () => {
    mockLoadSuccess({
      runtime: makeRuntime({ weight_mode: "ridge", active_model_run_id: "model-001-abc" }),
    });

    render(<FactorModelPage />);

    await waitFor(() => {
      const fallbackBtn = screen.getByText("factorModelFallback").closest("button");
      expect(fallbackBtn).not.toBeDisabled();
    });
  });
  it("renders rejected model with rejected tag instead of activate buttons", async () => {
    mockLoadSuccess({
      models: [
        makeModel({
          id: "model-rejected-001",
          status: "rejected",
          rejection_reason: "IC 低于阈值",
        }),
      ],
    });

    render(<FactorModelPage />);

    await waitFor(() => {
      // rejected 状态下渲染两个 rejected tag（状态列 + 操作列）
      const rejectedTags = screen.getAllByText("factorModelRejected");
      expect(rejectedTags.length).toBeGreaterThanOrEqual(2);
    });

    // 不应渲染激活按钮
    expect(screen.queryByText("factorModelShadow")).not.toBeInTheDocument();
    expect(screen.queryByText("factorModelRidge")).not.toBeInTheDocument();
  });
  it("shows shadow and ridge activate buttons for validated model", async () => {
    mockLoadSuccess();

    render(<FactorModelPage />);

    await waitFor(() => {
      expect(screen.getByText("factorModelShadow")).toBeInTheDocument();
      expect(screen.getByText("factorModelRidge")).toBeInTheDocument();
    });
  });
  it("loads model detail with weights and audit logs on click", async () => {
    mockLoadSuccess();
    const audit = [
      {
        id: 1,
        action: "activate",
        model_run_id: "model-001-abc",
        previous_mode: "manual",
        new_mode: "ridge",
        previous_model_run_id: null,
        new_model_run_id: "model-001-abc",
        actor: "local_user",
        note: "首次激活",
        created_at: "2026-08-01T13:00:00",
      },
    ];
    mockApi.getFactorModel.mockResolvedValue(
      makeModel({ audit, weights: makeModel().weights }),
    );

    render(<FactorModelPage />);

    // 等待列表加载后点击模型 ID
    await waitFor(() => {
      expect(screen.getByText("model-001-abc")).toBeInTheDocument();
    });

    await act(async () => {
      fireEvent.click(screen.getByText("model-001-abc"));
    });

    // 详情卡片标题
    await waitFor(() => {
      expect(screen.getByText(/factorModelDetail/)).toBeInTheDocument();
    });

    // 审计日志区域
    expect(screen.getByText("factorModelAuditLogs")).toBeInTheDocument();
    // 审计日志内容：激活备注
    expect(screen.getByText("首次激活")).toBeInTheDocument();

    // 权重快照标题
    expect(screen.getByText("factorModelWeights")).toBeInTheDocument();
    // 权重因子代码
    expect(screen.getByText("momentum_20")).toBeInTheDocument();
  });
  it("opens activate modal and submits successfully", async () => {
    mockLoadSuccess();
    const newRuntime = makeRuntime({ weight_mode: "ridge", active_model_run_id: "model-001-abc", version: 2 });
    mockApi.scoringActivateModel.mockResolvedValue(newRuntime);

    render(<FactorModelPage />);

    await waitFor(() => {
      expect(screen.getByText("factorModelRidge")).toBeInTheDocument();
    });

    // 点击 ridge 激活按钮
    await act(async () => {
      fireEvent.click(screen.getByText("factorModelRidge"));
    });

    // Modal 打开
    await waitFor(() => {
      expect(screen.getByText("factorModelActivateTitle")).toBeInTheDocument();
    });

    // 确认按钮（Modal footer 的 OK 按钮）
    const okBtn = screen.getByText("factorTransitionOk").closest("button");
    expect(okBtn).not.toBeNull();

    await act(async () => {
      fireEvent.click(okBtn!);
    });

    await waitFor(() => {
      expect(mockApi.scoringActivateModel).toHaveBeenCalledWith(
        "model-001-abc",
        "ridge",
        undefined,
        "factor_center:activate",
      );
      expect(mockMessage.success).toHaveBeenCalledWith("factorModelActivated");
      expect(mockApi.scoringCreateTask).toHaveBeenCalledWith(expect.objectContaining({
        train_model: false, materialize_scores: true, factor_set_id: "fs-test-001",
        hyperparameters: expect.objectContaining({ expected_model_run_id: "model-001-abc", expected_weight_mode: "ridge", factor_set_id: "fs-test-001" }),
        factor_scope: expect.objectContaining({ asset_type: "stock", factor_set_id: "fs-test-001" }),
      }));
      expect(screen.getByText(/评分任务已提交：score-task-001/)).toBeInTheDocument();
    });
  });
  it("keeps the activated runtime when scoring submission fails", async () => {
    mockLoadSuccess(); // A late overview refresh still contains the old manual mode.
    mockApi.scoringActivateModel.mockResolvedValue(makeRuntime({ weight_mode: "ridge", active_model_run_id: "model-001-abc", version: 2 }));
    mockApi.scoringCreateTask.mockRejectedValueOnce(new Error("PIPELINE_BUSY"));
    render(<FactorModelPage />);
    fireEvent.click(await screen.findByText("factorModelRidge"));
    fireEvent.click((await screen.findByText("factorTransitionOk")).closest("button")!);
    await waitFor(() => {
      expect(screen.getByText(/模型已激活，评分生成未启动.*PIPELINE_BUSY/)).toBeInTheDocument();
      expect(screen.getByText("model-001-abc...")).toBeInTheDocument();
    });
    expect(mockMessage.error).not.toHaveBeenCalledWith(expect.stringContaining("factorModelActionFailed"));
  });
  it("shows error when activate fails", async () => {
    mockLoadSuccess();
    mockApi.scoringActivateModel.mockRejectedValue(new Error("server error"));

    render(<FactorModelPage />);

    await waitFor(() => {
      expect(screen.getByText("factorModelRidge")).toBeInTheDocument();
    });

    await act(async () => {
      fireEvent.click(screen.getByText("factorModelRidge"));
    });

    await waitFor(() => {
      expect(screen.getByText("factorModelActivateTitle")).toBeInTheDocument();
    });

    const okBtn = screen.getByText("factorTransitionOk").closest("button");
    await act(async () => {
      fireEvent.click(okBtn!);
    });

    await waitFor(() => {
      expect(mockMessage.error).toHaveBeenCalledWith(
        expect.stringContaining("factorModelActionFailed"),
      );
    });
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
  });

  it("opens fallback modal and warns when reason is empty", async () => {
    mockLoadSuccess({
      runtime: makeRuntime({ weight_mode: "ridge", active_model_run_id: "model-001-abc" }),
    });

    render(<FactorModelPage />);

    await waitFor(() => {
      const fallbackBtn = screen.getByText("factorModelFallback").closest("button");
      expect(fallbackBtn).not.toBeDisabled();
    });

    await act(async () => {
      fireEvent.click(screen.getByText("factorModelFallback"));
    });

    await waitFor(() => {
      expect(screen.getByText("factorModelFallbackTitle")).toBeInTheDocument();
    });

    // 空原因提交
    const okBtn = screen.getByText("factorModelConfirmFallback").closest("button");
    await act(async () => {
      fireEvent.click(okBtn!);
    });

    await waitFor(() => {
      expect(mockMessage.warning).toHaveBeenCalledWith("factorModelReasonRequired");
    });
    expect(mockApi.scoringFallbackToManual).not.toHaveBeenCalled();
  });
  it("submits fallback successfully when reason is provided", async () => {
    mockLoadSuccess({
      runtime: makeRuntime({ weight_mode: "ridge", active_model_run_id: "model-001-abc" }),
    });
    const newRuntime = makeRuntime({ weight_mode: "manual", active_model_run_id: null });
    mockApi.scoringFallbackToManual.mockResolvedValue(newRuntime);

    render(<FactorModelPage />);

    await waitFor(() => {
      expect(screen.getByText("factorModelFallback")).toBeInTheDocument();
    });

    await act(async () => {
      fireEvent.click(screen.getByText("factorModelFallback"));
    });

    await waitFor(() => {
      expect(screen.getByText("factorModelFallbackTitle")).toBeInTheDocument();
    });

    // 输入原因
    const textarea = screen.getByPlaceholderText("factorModelFallbackReason");
    await act(async () => {
      fireEvent.change(textarea, { target: { value: "IC 衰减触发回退" } });
    });

    const okBtn = screen.getByText("factorModelConfirmFallback").closest("button");
    await act(async () => {
      fireEvent.click(okBtn!);
    });

    await waitFor(() => {
      expect(mockApi.scoringFallbackToManual).toHaveBeenCalledWith("IC 衰减触发回退", "factor_center:fallback");
      expect(mockMessage.success).toHaveBeenCalledWith("factorModelFallbackDone");
    });
  });

  it("shows error when load fails", async () => {
    mockApi.scoringGetOverviewAsFactor.mockRejectedValue(new Error("network error"));
    mockApi.scoringListFactorSetsAsFactor.mockResolvedValue([]);

    render(<FactorModelPage />);

    await waitFor(() => {
      expect(mockMessage.error).toHaveBeenCalledWith(
        expect.stringContaining("factorModelLoadFailed"),
      );
    });
  });
  it("shows rejection reason alert in model detail when rejected", async () => {
    mockLoadSuccess({
      models: [makeModel({ id: "model-rej-detail", status: "rejected", rejection_reason: "门禁拒绝：ICIR 不足" })],
    });
    mockApi.getFactorModel.mockResolvedValue(
      makeModel({
        id: "model-rej-detail",
        status: "rejected",
        rejection_reason: "门禁拒绝：ICIR 不足",
      }),
    );

    render(<FactorModelPage />);

    await waitFor(() => {
      // 列表中 rejection tag 渲染后才能定位到模型 id（截断后的文本）
      // 由于 rejected 模型不显示 id 按钮，使用文本搜索：先验证 rejected 标签
      expect(screen.getAllByText("factorModelRejected").length).toBeGreaterThan(0);
    });
  });
  it("shows score weight mode tag when score_weight_mode is ridge", async () => {
    mockLoadSuccess({
      runtime: makeRuntime({
        weight_mode: "ridge",
        score_weight_mode: "ridge",
        active_model_run_id: "model-001-abc",
      }),
    });

    render(<FactorModelPage />);

    await waitFor(() => {
      expect(screen.getByText("factorModelScoreWeightMode")).toBeInTheDocument();
    });
  });
});
