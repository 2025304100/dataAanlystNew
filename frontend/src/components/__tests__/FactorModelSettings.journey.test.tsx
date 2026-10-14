import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const { mockApi, mockNavigate, showToast } = vi.hoisted(() => ({
  showToast: vi.fn(),
  mockNavigate: vi.fn(),
  mockApi: {
    scoringGetOverviewAsFactor: vi.fn(),
    scoringGetFactorModelListAsFactor: vi.fn(),
    scoringListFactorSetsAsFactor: vi.fn(),
    scoringGetFactorSetReadiness: vi.fn(),
    scoringListTasks: vi.fn(),
    scoringGetTask: vi.fn(),
    scoringGetPipelineEta: vi.fn(),
    scoringCreateTask: vi.fn(),
    scoringActivateModel: vi.fn(),
    scoringCancelTask: vi.fn(),
    scoringUpdateSystemConfig: vi.fn(),
    scoringInitializeWarehouse: vi.fn(),
    scoringFallbackToManual: vi.fn(),
    scoringGetModelRelations: vi.fn(),
  },
}));

vi.mock("../../api/client", () => ({ api: mockApi }));
vi.mock("../../context/AppContext", () => ({ useApp: () => ({ locale: "zh-CN", showToast }) }));
vi.mock("../../utils/navigate", () => ({ navigate: (path: string) => mockNavigate(path) }));

import FactorModelSettings from "../FactorModelSettings";

const overview = {
  feature_enabled: true,
  config: { feature_enabled: true, warehouse_path: "factor.duckdb" },
  runtime: { weight_mode: "manual", score_weight_mode: "manual", active_model_run_id: null, version: 1 },
  health: { status: "healthy", warehouse_available: true, warehouse_path: "factor.duckdb", reasons: [] },
  latest_trade_date: "2026-10-12",
  factor_coverage: [],
};

function frozenSet(id: string, name: string) {
  return { id, name, label: name, status: "frozen", n_members: 2, feature_count: 2, members_trainable_ratio: 1 };
}

async function chooseSet(label = "价值因子集合") {
  if (!screen.queryByTestId("factor-set-select")) {
    fireEvent.click(await screen.findByRole("button", { name: /第 2 步：训练模型/ }));
  }
  const select = await screen.findByTestId("factor-set-select");
  await waitFor(() => expect(mockApi.scoringListFactorSetsAsFactor).toHaveBeenCalled());
  fireEvent.mouseDown(select.querySelector(".ant-select-selector")!);
  fireEvent.click(await screen.findByText(`${label}（2 因子）`));
  await waitFor(() => expect(select.querySelector(".ant-select-selection-item")).toHaveTextContent(label));
  return select;
}

describe("FactorModelSettings — end-user pipeline journey", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    window.localStorage.clear();
    mockApi.scoringGetOverviewAsFactor.mockResolvedValue(overview);
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: [] });
    mockApi.scoringListFactorSetsAsFactor.mockResolvedValue([
      frozenSet("fs-A", "趋势因子集合"),
      frozenSet("fs-B", "价值因子集合"),
    ]);
    mockApi.scoringListTasks.mockResolvedValue([]);
    mockApi.scoringGetFactorSetReadiness.mockImplementation(async (factorSetId: string) => ({
      ready: true, factor_set_id: factorSetId, code: "READY", message: "因子集合已通过训练预检", details: {},
    }));
    mockApi.scoringGetPipelineEta.mockResolvedValue({ recommended_seconds: 1200, sample_count: 0 });
    mockApi.scoringCreateTask.mockResolvedValue({ id: "train-task", task_type: "factor_pipeline", status: "queued", stage: "queued", percent: 0, message: "queued", payload_json: { train_model: true, materialize_scores: false, factor_set_id: "fs-B" }, result: null });
  });

  it("keeps a manually selected training set when refreshing page data", async () => {
    render(<FactorModelSettings />);
    const select = await chooseSet();

    fireEvent.click(screen.getByRole("button", { name: "刷新" }));

    await waitFor(() => expect(mockApi.scoringListFactorSetsAsFactor).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(select.querySelector(".ant-select-selection-item")).toHaveTextContent("价值因子集合"));
  });

  it("shows the actual selected-set readiness blocker before training", async () => {
    mockApi.scoringGetFactorSetReadiness.mockResolvedValue({
      ready: false, factor_set_id: "fs-B", code: "FACTOR_SET_NOT_READY",
      message: "集合成员缺少可训练版本，请修复因子版本", details: {},
    });
    render(<FactorModelSettings />);
    await chooseSet();

    await within(screen.getByTestId("train-mode-block")).findByText("集合成员缺少可训练版本，请修复因子版本");
    expect(screen.getByRole("button", { name: /启动流水线.*训练模式/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /查看并修复集合|修复集合/ }));
    expect(mockNavigate).toHaveBeenCalledWith(expect.stringContaining("fs-B"));
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
  });

  it("identifies completed input calculation awaiting activation without reporting published scores", async () => {
    mockApi.scoringListTasks.mockResolvedValue([{
      id: "inputs-done", task_type: "factor_pipeline", status: "done", stage: "done", percent: 100,
      message: "Factor calculation completed; scores skipped until explicit model activation",
      result: {
        completion_status: "AWAITING_MODEL_ACTIVATION",
        scoring_bridge: { status: "SKIPPED", reason: "NO_ACTIVE_FACTOR_MODEL", materialized_count: 0 },
      },
    }]);
    render(<FactorModelSettings />);
    fireEvent.click(await screen.findByRole("button", { name: /第 4 步：生成评分/ }));

    await screen.findAllByText(/输入计算完成，等待激活/);
    const strip = screen.getByText("任务：inputs-done").closest(".factor-task-strip")!;
    expect(within(strip as HTMLElement).getByText(/评分未生成/)).toBeInTheDocument();
    expect(strip).not.toHaveTextContent("评分已生成");
  });

  it("shows a submitted training task as queued until the worker actually reaches training", async () => {
    render(<FactorModelSettings />);
    await chooseSet();
    const runButton = screen.getByRole("button", { name: /启动流水线.*训练模式/ });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await waitFor(() => expect(mockApi.scoringCreateTask).toHaveBeenCalledWith(expect.objectContaining({
      train_model: true, materialize_scores: false, factor_set_id: "fs-B",
    })));
    await screen.findByText("任务：train-task");
    expect(screen.getByTestId("pipeline-step-4")).not.toHaveClass("ps-state-running");
    expect(runButton).toBeDisabled();
  });

  it("keeps training blocked when eligibility cannot be read and allows an explicit retry", async () => {
    mockApi.scoringGetFactorSetReadiness.mockRejectedValueOnce(new Error("预检服务暂时不可用"));
    render(<FactorModelSettings />);
    await chooseSet();

    await within(screen.getByTestId("train-mode-block")).findByText(/训练资格读取失败：预检服务暂时不可用/);
    const runButton = screen.getByRole("button", { name: /启动流水线.*训练模式/ });
    expect(runButton).toBeDisabled();
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "重试训练资格检查" }));

    await screen.findByText("集合训练资格已确认");
    await waitFor(() => expect(runButton).toBeEnabled());
    expect(mockApi.scoringGetFactorSetReadiness).toHaveBeenCalledTimes(2);
    expect(mockApi.scoringGetFactorSetReadiness).toHaveBeenNthCalledWith(2, "fs-B");
  });

  it("continues from selected-set training to explicit production activation and model-pinned published scores", async () => {
    const trainedModel = {
      id: "ridge-new", status: "validated", model_type: "ridge", production_eligible: true,
      readiness_code: "READY", readiness_message: "真实数据训练证据通过", training_mode: "warehouse",
      hyperparameters: { factor_set_id: "fs-B" }, weights: [], metrics: { validation_ic: 0.08 },
      sample_count: 10000, data_cutoff_at: "2026-10-12T08:00:00Z",
    };
    const trainingDone = {
      id: "journey-train", task_type: "factor_pipeline", status: "done", stage: "done", percent: 100,
      message: "Factor calculation completed; scores skipped until explicit model activation",
      payload_json: { train_model: true, materialize_scores: false, factor_set_id: "fs-B" },
      result: { completion_status: "AWAITING_MODEL_ACTIVATION", model: { model_run_id: "ridge-new", status: "validated" } },
    };
    const scoringDone = {
      id: "journey-scores", task_type: "factor_pipeline", status: "done", stage: "done", percent: 100,
      message: "Factor pipeline completed", payload_json: { train_model: false, materialize_scores: true, factor_set_id: "fs-B" },
      result: { completion_status: "COMPLETED", scoring_bridge: { status: "PUBLISHED", materialized_count: 20, model_run_id: "ridge-new" } },
    };
    let currentTask: any = null;
    let modelAvailable = false;
    let activated = false;
    mockApi.scoringGetOverviewAsFactor.mockImplementation(async () => activated
      ? { ...overview, runtime: { weight_mode: "ridge", score_weight_mode: "ridge", active_model_run_id: "ridge-new", version: 2 } }
      : overview);
    mockApi.scoringGetFactorModelListAsFactor.mockImplementation(async () => ({ items: modelAvailable ? [trainedModel] : [] }));
    mockApi.scoringListTasks.mockImplementation(async () => currentTask ? [currentTask] : []);
    mockApi.scoringCreateTask.mockImplementation(async (payload: any) => {
      currentTask = payload.train_model
        ? { ...trainingDone, status: "running", stage: "train", percent: 75, result: null }
        : { ...scoringDone, status: "running", stage: "score", percent: 90, result: null };
      return currentTask;
    });
    mockApi.scoringGetTask.mockImplementation(async (taskId: string) => {
      if (taskId === "journey-train") {
        modelAvailable = true;
        currentTask = trainingDone;
      } else currentTask = scoringDone;
      return currentTask;
    });
    mockApi.scoringActivateModel.mockImplementation(async () => {
      activated = true;
      return { weight_mode: "ridge", score_weight_mode: "ridge", active_model_run_id: "ridge-new", version: 2 };
    });
    render(<FactorModelSettings />);
    await chooseSet();
    const runButton = screen.getByRole("button", { name: /启动流水线.*训练模式/ });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await screen.findByText(/训练验证完成，等待激活/, {}, { timeout: 5000 });
    expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
    expect(screen.getByTestId("factor-set-select").querySelector(".ant-select-selection-item")).toHaveTextContent("价值因子集合");
    fireEvent.click(screen.getByRole("button", { name: /第 3 步：验证与启用/ }));
    await screen.findByText("ridge-new");
    fireEvent.click(screen.getByRole("button", { name: /正式启用|生产运行|生产激活/ }));

    await waitFor(() => expect(mockApi.scoringCreateTask).toHaveBeenNthCalledWith(2, expect.objectContaining({
      train_model: false, materialize_scores: true, factor_set_id: "fs-B",
      hyperparameters: expect.objectContaining({ expected_model_run_id: "ridge-new", expected_weight_mode: "ridge", factor_set_id: "fs-B" }),
    })));
    await screen.findAllByText(/评分已生成/, {}, { timeout: 5000 });
    const strip = screen.getByText("任务：journey-scores").closest(".factor-task-strip")!;
    expect(strip).toHaveTextContent("20");
    expect(screen.getByTestId("pipeline-step-5")).toHaveClass("ps-state-ready");
  }, 15000);

  it("does not mark a legacy validated model without production eligibility as an activation-ready candidate", async () => {
    mockApi.scoringListFactorSetsAsFactor.mockResolvedValue([{
      ...frozenSet("fs-B", "价值因子集合"), members: [{ factor_code: "ep_ttm", role: "feature" }, { factor_code: "bp", role: "feature" }],
    }]);
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: [{
      id: "legacy-demo", status: "validated", production_eligible: false, training_mode: "offline_minimal",
      readiness_code: "MODEL_TRAINING_SOURCE_UNVERIFIED", readiness_message: "演示模型不可用于正式运行",
      hyperparameters: { factor_set_id: "fs-B" }, metrics: {}, sample_count: 0, weights: [],
    }] });
    render(<FactorModelSettings />);
    fireEvent.click(await screen.findByRole("button", { name: /第 3 步：验证与启用/ }));
    await screen.findByText("legacy-demo");
    expect(screen.getByRole("button", { name: /正式启用|生产运行|生产激活/ })).toBeDisabled();
    expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
  });

  it("blocks score submission when the active model has no frozen-set binding", async () => {
    mockApi.scoringGetOverviewAsFactor.mockResolvedValue({ ...overview,
      runtime: { weight_mode: "ridge", active_model_run_id: "active-unbound", version: 2 },
    });
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: [{
      id: "active-unbound", status: "validated", production_eligible: true,
      hyperparameters: {}, metrics: {}, sample_count: 10000, weights: [],
    }] });
    render(<FactorModelSettings initialTrainModel={false} />);
    fireEvent.click(await screen.findByRole("button", { name: /第 4 步：生成评分/ }));
    await screen.findByTestId("pipeline-no-train-banner");
    expect(screen.getByRole("button", { name: /启动流水线.*不训练模式/ })).toBeDisabled();
    expect(screen.getByTestId("pipeline-no-train-banner")).toHaveTextContent("未记录冻结因子集合");
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
  });

  it("pins a score-only refresh to the actual active model and its collection", async () => {
    mockApi.scoringGetOverviewAsFactor.mockResolvedValue({ ...overview,
      runtime: { weight_mode: "shadow", active_model_run_id: "active-shadow", version: 2 },
    });
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: [{
      id: "active-shadow", status: "validated", production_eligible: true,
      hyperparameters: { factor_set_id: "fs-A" }, metrics: {}, sample_count: 10000, weights: [],
    }] });
    render(<FactorModelSettings initialTrainModel={false} />);

    const runButton = screen.getByRole("button", { name: /启动流水线.*不训练模式/ });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);
    await waitFor(() => expect(mockApi.scoringCreateTask).toHaveBeenCalledWith(expect.objectContaining({
      train_model: false, materialize_scores: true, factor_set_id: "fs-A",
      factor_scope: { factor_set_id: "fs-A", asset_type: "stock" },
      hyperparameters: { expected_model_run_id: "active-shadow", expected_weight_mode: "shadow", factor_set_id: "fs-A" },
    })));
  });

  it("marks the actual failed scoring stage as blocked without showing uncommitted scores as published", async () => {
    mockApi.scoringListTasks.mockResolvedValue([{
      id: "commit-failed", task_type: "factor_pipeline", status: "failed", stage: "failed", percent: 90,
      message: "模型在提交前已变更，评分未发布", payload_json: { train_model: false, materialize_scores: true },
      errors: [{ extras: { stage: "scoring" } }],
      result: { factors: { row_count: 100 }, scoring_bridge: { status: "PUBLISHED", materialized_count: 20 } },
    }]);
    render(<FactorModelSettings initialTrainModel={false} />);
    fireEvent.click(await screen.findByRole("button", { name: /第 4 步：生成评分/ }));
    await screen.findByText("任务：commit-failed");
    expect(screen.getByTestId("pipeline-step-5")).toHaveClass("ps-state-blocked");
    expect(screen.getByTestId("pipeline-step-5")).not.toHaveClass("ps-state-ready");
    expect(screen.getByText("任务：commit-failed").closest(".factor-task-strip")).not.toHaveTextContent("评分已生成");
  });

  it("does not send training-only window settings in input-only mode", async () => {
    render(<FactorModelSettings initialTrainModel={false} />);
    fireEvent.click(await screen.findByRole("button", { name: /第 4 步：生成评分/ }));
    await screen.findByRole("button", { name: "仅计算输入" });
    fireEvent.click(screen.getByRole("button", { name: "仅计算输入" }));
    const runButton = await screen.findByRole("button", { name: "仅计算已有输入" });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await waitFor(() => expect(mockApi.scoringCreateTask).toHaveBeenCalled());
    expect(mockApi.scoringCreateTask.mock.calls[0][0]).not.toHaveProperty("window_days");
    expect(mockApi.scoringCreateTask.mock.calls[0][0]).not.toHaveProperty("validation_days");
  });
});
