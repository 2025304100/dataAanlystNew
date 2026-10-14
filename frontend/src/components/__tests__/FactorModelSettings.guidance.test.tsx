import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

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
    getWatchlists: vi.fn(),
    getExternalDataOverview: vi.fn(),
    getExternalDataCoverage: vi.fn(),
    getExternalDataSyncCapabilities: vi.fn(),
  },
}));

vi.mock("../../api/client", () => ({ api: mockApi }));
vi.mock("../../context/AppContext", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../context/AppContext")>();
  return { ...actual, useApp: () => ({ locale: "zh-CN", showToast }) };
});
vi.mock("../../utils/navigate", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../utils/navigate")>();
  return { ...actual, navigate: (path: string) => mockNavigate(path) };
});

import FactorModelSettings from "../FactorModelSettings";

const overview = {
  feature_enabled: true,
  config: { feature_enabled: true, warehouse_path: "factor.duckdb" },
  runtime: { weight_mode: "manual", score_weight_mode: "manual", active_model_run_id: null, version: 1 },
  health: { status: "healthy", warehouse_available: true, warehouse_path: "factor.duckdb", reasons: [] },
  latest_trade_date: "2026-10-12",
  average_coverage: 0.98,
  factor_coverage: [],
};

function frozenSet(id: string, name: string) {
  return { id, name, label: name, status: "frozen", n_members: 2, feature_count: 2, members_trainable_ratio: 1 };
}

function candidate(id: string, factorSetId: string) {
  return {
    id, model_type: "ridge", status: "validated", production_eligible: true,
    readiness_code: "READY", readiness_message: "真实训练来源与因子版本通过", training_mode: "warehouse",
    hyperparameters: { factor_set_id: factorSetId }, weights: [], metrics: { validation_ic: 0.08 },
    sample_count: 10000, data_cutoff_at: "2026-10-12T08:00:00Z",
  };
}

function enableModel(id = "active-A", factorSetId = "fs-A") {
  const model = candidate(id, factorSetId);
  mockApi.scoringGetOverviewAsFactor.mockResolvedValue({ ...overview,
    runtime: { weight_mode: "ridge", score_weight_mode: "ridge", active_model_run_id: id, version: 2 },
  });
  mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: [model] });
  return model;
}

async function chooseSet(label = "价值因子集合") {
  if (!screen.queryByTestId("factor-set-select")) await goPage(2);
  const select = await screen.findByTestId("factor-set-select");
  fireEvent.mouseDown(select.querySelector(".ant-select-selector")!);
  fireEvent.click(await screen.findByText(`${label}（2 因子）`));
  await waitFor(() => expect(select.querySelector(".ant-select-selection-item")).toHaveTextContent(label));
  return select;
}

async function goPage(step: 1 | 2 | 3 | 4) {
  const names = ["准备输入", "训练模型", "验证与启用", "生成评分"];
  fireEvent.click(await screen.findByRole("button", { name: new RegExp(`第 ${step} 步：${names[step - 1]}`) }));
  return screen.findByTestId(`wizard-page-${["prepare", "train", "review", "score"][step - 1]}`);
}

function expectNoSubmittedChanges() {
  expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
  expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
  expect(mockApi.scoringUpdateSystemConfig).not.toHaveBeenCalled();
}

describe("FactorModelSettings — guided user actions", () => {
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
      ready: true, factor_set_id: factorSetId, code: "READY", message: "集合已通过训练前置检查", details: {},
    }));
    mockApi.scoringGetPipelineEta.mockResolvedValue({ recommended_seconds: 1200, sample_count: 0 });
    mockApi.getWatchlists.mockResolvedValue([]);
    mockApi.getExternalDataOverview.mockResolvedValue({ datasets: [], total_records: 0, covered_symbols: 0, available_datasets: 0, running_tasks: 0 });
    mockApi.getExternalDataCoverage.mockResolvedValue({ datasets: [], generated_at: "2026-10-14T08:00:00Z" });
    mockApi.getExternalDataSyncCapabilities.mockResolvedValue({});
    mockApi.scoringGetModelRelations.mockImplementation(async (modelId: string) => ({
      model_id: modelId, status: "validated", factor_set_id: "fs-B", factor_set_name: "价值因子集合",
      n_members: 2, factors: [], data_cutoff_at: "2026-10-12T08:00:00Z", created_at: null, unbound_reason: null,
    }));
  });

  it("directs completed inputs with ineligible old models toward retraining instead of activation", async () => {
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: [{
      ...candidate("legacy-demo", "fs-B"), production_eligible: false,
      readiness_code: "MODEL_TRAINING_SOURCE_UNVERIFIED", readiness_message: "演示或来源不明的模型不可启用",
    }] });
    mockApi.scoringListTasks.mockResolvedValue([{
      id: "old-inputs", task_type: "factor_pipeline", status: "done", stage: "done", percent: 100,
      payload_json: { train_model: false, materialize_scores: true },
      result: { completion_status: "AWAITING_MODEL_ACTIVATION", scoring_bridge: { status: "SKIPPED", materialized_count: 0 } },
    }]);
    render(<FactorModelSettings />);
    await goPage(3);
    await screen.findByText("legacy-demo");
    expect(screen.getByRole("button", { name: /正式启用/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "配置重新训练" }));
    await screen.findByTestId("wizard-page-train");
    expect(screen.getByTestId("factor-set-select")).toBeInTheDocument();
    await goPage(4);
    expect(screen.getByText("任务：old-inputs").closest(".factor-task-strip")).toHaveTextContent("评分未生成");
    expectNoSubmittedChanges();
  });

  it("reviews the selected collection's candidate while another collection's model is active", async () => {
    const modelA = enableModel();
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: [modelA, candidate("candidate-B", "fs-B")] });
    render(<FactorModelSettings />);
    await screen.findByTestId("wizard-page-score");
    await chooseSet();
    expect(screen.getByRole("button", { name: /第 2 步：训练模型/ })).toHaveAttribute("aria-current", "step");
    expect(screen.getByRole("button", { name: /第 3 步：验证与启用/ })).not.toHaveAttribute("aria-current");
    await goPage(3);
    fireEvent.click(await screen.findByTestId("view-relations-candidate-B"));
    await waitFor(() => expect(mockApi.scoringGetModelRelations).toHaveBeenCalledWith("candidate-B"));
    expect(screen.getByRole("region", { name: "模型验证与启用" })).toHaveTextContent("candidate-B");
    expectNoSubmittedChanges();
  });

  it("defaults to the actual active model's score operation page without any training controls", async () => {
    enableModel();
    render(<FactorModelSettings />);
    await screen.findByTestId("wizard-page-score");
    expect(screen.getByRole("button", { name: /第 4 步：生成评分/ })).toHaveAttribute("aria-current", "step");
    expect(screen.getByRole("region", { name: "本次评分配置" })).toBeInTheDocument();
    expect(screen.queryByTestId("factor-set-select")).not.toBeInTheDocument();
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "模型验证与启用" })).not.toBeInTheDocument();
    expectNoSubmittedChanges();
  });

  it("uses navigation as a draft and exposes controls only on their operation page", async () => {
    render(<FactorModelSettings />);
    await screen.findByTestId("wizard-page-prepare");
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    await goPage(2);
    expect(screen.getAllByRole("spinbutton")).toHaveLength(2);
    await goPage(4);
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    expect(screen.queryByTestId("factor-set-select")).not.toBeInTheDocument();
    await goPage(1);
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    await goPage(2);
    expect(screen.getAllByRole("spinbutton")).toHaveLength(2);
    expectNoSubmittedChanges();
  });

  it("keeps running training and its duration estimate bound to submitted payload when viewing another page", async () => {
    const trainingTask = {
      id: "running-training", task_type: "factor_pipeline", status: "running", stage: "train", percent: 75,
      message: "Training ridge model", started_at: "2026-10-14T04:00:00Z",
      payload_json: { train_model: true, materialize_scores: false, full_refresh: true, factor_set_id: "fs-B" },
      result: null,
    };
    mockApi.scoringListTasks.mockResolvedValue([trainingTask]);
    mockApi.scoringGetTask.mockResolvedValue(trainingTask);
    render(<FactorModelSettings />);
    await screen.findByText("任务：running-training");
    await waitFor(() => expect(mockApi.scoringGetPipelineEta).toHaveBeenCalledWith(true, true));
    expect(screen.getByTestId("pipeline-step-4")).toHaveClass("ps-state-running");
    await goPage(1);
    expect(screen.queryByText("任务：running-training")).not.toBeInTheDocument();
    await goPage(2);
    expect(screen.getByTestId("pipeline-step-4")).toHaveClass("ps-state-running");
    expect(screen.getByRole("region", { name: "最近一次运行" })).toHaveTextContent("训练新模型");
    expect(mockApi.scoringGetPipelineEta.mock.calls.every(([train, full]) => train === true && full === true)).toBe(true);
    expectNoSubmittedChanges();
  });

  it("shows low input coverage with an explicit supplement action while a structurally ready set remains trainable", async () => {
    mockApi.scoringGetOverviewAsFactor.mockResolvedValue({ ...overview, average_coverage: 0.323 });
    mockApi.scoringListFactorSetsAsFactor.mockResolvedValue([frozenSet("fs-B", "价值因子集合")]);
    render(<FactorModelSettings />);
    await screen.findByTestId("wizard-page-prepare");
    await waitFor(() => expect(screen.getByTestId("wizard-page-prepare")).toHaveTextContent("32.3%"));
    await goPage(2);
    await screen.findByText("集合训练资格已确认");
    expect(screen.getByRole("button", { name: /启动流水线.*训练模式/ })).toBeEnabled();
    await goPage(1);
    fireEvent.click(screen.getByRole("button", { name: "准备因子输入" }));
    await screen.findByRole("region", { name: "因子输入操作页" });
    await waitFor(() => expect(mockApi.getExternalDataOverview).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("button", { name: "仅计算已有输入" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("factor-set-select")).not.toBeInTheDocument();
    expectNoSubmittedChanges();
  });

  it("preserves the chosen training collection after preparing inputs and returning to training", async () => {
    render(<FactorModelSettings />);
    await chooseSet();
    await screen.findByText("集合训练资格已确认");
    await goPage(1);
    fireEvent.click(screen.getByRole("button", { name: "准备因子输入" }));
    await screen.findByRole("region", { name: "因子输入操作页" });
    fireEvent.click(screen.getByRole("button", { name: "返回输入检查" }));
    await screen.findByRole("region", { name: "因子输入检查" });
    await goPage(2);
    const select = await screen.findByTestId("factor-set-select");
    await waitFor(() => expect(select.querySelector(".ant-select-selection-item")).toHaveTextContent("价值因子集合"));
    expect(screen.getByTestId("wizard-page-train")).toBeInTheDocument();
    await screen.findByText("集合训练资格已确认");
    expectNoSubmittedChanges();
  });
});
