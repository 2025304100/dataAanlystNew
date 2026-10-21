import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const { mockApi, showToast } = vi.hoisted(() => ({
  showToast: vi.fn(),
  mockApi: {
    scoringGetOverviewAsFactor: vi.fn(), scoringGetFactorModelListAsFactor: vi.fn(),
    scoringListFactorSetsAsFactor: vi.fn(), scoringGetFactorSetReadiness: vi.fn(),
    scoringListTasks: vi.fn(), scoringGetTask: vi.fn(), scoringGetPipelineEta: vi.fn(),
    scoringCreateTask: vi.fn(), scoringActivateModel: vi.fn(), scoringCancelTask: vi.fn(),
    scoringUpdateSystemConfig: vi.fn(), scoringInitializeWarehouse: vi.fn(),
    scoringFallbackToManual: vi.fn(), scoringGetModelRelations: vi.fn(), getFactorModel: vi.fn(),
  },
}));

vi.mock("../../api/client", () => ({ api: mockApi }));
vi.mock("../../context/AppContext", () => ({ useApp: () => ({ locale: "zh-CN", showToast }) }));

import FactorModelSettings from "../FactorModelSettings";

const overview = {
  feature_enabled: true, config: { feature_enabled: true, warehouse_path: "factor.duckdb" },
  runtime: { weight_mode: "manual", score_weight_mode: "manual", active_model_run_id: null, version: 1 },
  health: { status: "healthy", warehouse_available: true, warehouse_path: "factor.duckdb", reasons: [] },
  latest_trade_date: "2026-10-12", average_coverage: 0.98, factor_coverage: [],
};
const sets = [
  { id: "fs-A", name: "趋势集合", label: "趋势集合", status: "frozen", n_members: 2, feature_count: 2 },
  { id: "fs-B", name: "价值集合", label: "价值集合", status: "frozen", n_members: 2, feature_count: 2 },
];
function candidate(id = "candidate-B", factorSetId = "fs-B") {
  return {
    id, status: "validated", model_type: "ridge", production_eligible: true,
    readiness_code: "READY", training_mode: "warehouse", hyperparameters: { factor_set_id: factorSetId },
    weights: [], metrics: { validation_ic: 0.08 }, sample_count: 10000,
    data_cutoff_at: "2026-10-12T08:00:00Z",
  };
}

async function goPage(step: 1 | 2 | 3 | 4) {
  const names = ["准备输入", "训练模型", "验证与启用", "生成评分"];
  fireEvent.click(await screen.findByRole("button", { name: new RegExp(`第 ${step} 步：${names[step - 1]}`) }));
  return screen.findByTestId(`wizard-page-${["prepare", "train", "review", "score"][step - 1]}`);
}
async function chooseSet() {
  const select = await screen.findByTestId("factor-set-select");
  fireEvent.mouseDown(select.querySelector(".ant-select-selector")!);
  fireEvent.click(await screen.findByText("价值集合（2 因子）"));
  await screen.findByText("集合训练资格已确认");
}
async function confirmModelActivation() {
  const review = await screen.findByRole("dialog", { name: "确认启用模型" });
  const confirm = within(review).getByRole("button", { name: "确认启用并生成评分" });
  await waitFor(() => expect(confirm).toBeEnabled());
  fireEvent.click(confirm);
}
function onlyPage(name: "prepare" | "train" | "review" | "score") {
  for (const page of ["prepare", "train", "review", "score"]) {
    if (page === name) expect(screen.getByTestId(`wizard-page-${page}`)).toBeInTheDocument();
    else expect(screen.queryByTestId(`wizard-page-${page}`)).not.toBeInTheDocument();
  }
}

describe("FactorModelSettings — one operation page per wizard step", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mockApi.getFactorModel.mockImplementation(async (id: string) => ({ id, hyperparameters: {} }));
    window.localStorage.clear();
    mockApi.scoringGetOverviewAsFactor.mockResolvedValue(overview);
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: [] });
    mockApi.scoringListFactorSetsAsFactor.mockResolvedValue(sets);
    mockApi.scoringListTasks.mockResolvedValue([]);
    mockApi.scoringGetFactorSetReadiness.mockImplementation(async (id: string) => ({
      ready: true, factor_set_id: id, code: "READY", message: "集合通过训练前置检查", details: {},
    }));
    mockApi.scoringGetPipelineEta.mockResolvedValue({ recommended_seconds: 1200, sample_count: 0 });
    mockApi.scoringCreateTask.mockImplementation(async (payload: Record<string, unknown>) => ({
      id: "new-task", task_type: "factor_pipeline", status: "queued", stage: "queued", percent: 0,
      payload_json: payload, result: null,
    }));
    mockApi.scoringGetModelRelations.mockImplementation(async (id: string) => ({
      model_id: id, status: "validated", factor_set_id: "fs-B", factor_set_name: "价值集合",
      n_members: 2, factors: [], unbound_reason: null,
    }));
  });

  it("recognizes a pending independent-validation candidate as trained without allowing activation", async () => {
    const pending = { ...candidate("pending"), status: "pending_validation", production_eligible: false,
      training_mode: "warehouse", readiness_message: "尚待独立样本及成本后评估" };
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: [pending] });
    render(<FactorModelSettings />);
    await goPage(3);
    expect(screen.getByRole("button", { name: /第 2 步：训练模型/ })).toHaveAttribute("data-step-state", "complete");
    expect(screen.getByRole("button", { name: /第 3 步：验证与启用/ })).toHaveAttribute("data-step-state", "current");
    const row = (await screen.findByText(pending.id)).closest("tr")!;
    expect(within(row).getByText("训练结果通过，仍需检查后续预测")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: /正式启用/ })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "配置重新训练" })).not.toBeInTheDocument();
    expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
  });

  it("starts at input preparation and mounts only the selected operation page", async () => {
    render(<FactorModelSettings />);
    await screen.findByTestId("wizard-page-prepare");
    onlyPage("prepare");
    expect(screen.getByRole("button", { name: /第 1 步：准备输入/ })).toHaveAttribute("aria-current", "step");
    expect(screen.getByRole("button", { name: /第 2 步：训练模型/ })).not.toHaveAttribute("aria-current");
    expect(screen.queryByTestId("factor-set-select")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "模型验证与启用" })).not.toBeInTheDocument();

    await goPage(2);
    onlyPage("train");
    expect(screen.getByRole("button", { name: /第 2 步：训练模型/ })).toHaveAttribute("aria-current", "step");
    expect(screen.queryByRole("region", { name: "本次评分配置" })).not.toBeInTheDocument();
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
  });

  it("reviews the exact requested model and its runtime change before any activation or scoring write", async () => {
    const model = candidate("new-turnover-model");
    mockApi.scoringGetOverviewAsFactor.mockResolvedValue({ ...overview, runtime: { weight_mode: "shadow", score_weight_mode: "manual", active_model_run_id: "currently-active-model", version: 4 } });
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: [model] });
    mockApi.scoringActivateModel.mockResolvedValue({ weight_mode: "ridge", active_model_run_id: model.id, version: 5 });
    render(<FactorModelSettings />);
    await goPage(3);
    const row = (await screen.findByText(model.id)).closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: /正式启用/ }));

    expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
    const review = await screen.findByRole("dialog", { name: "确认启用模型" });
    await waitFor(() => expect(within(review).getByText(model.id)).toBeVisible());
    expect(within(review).getByText("fs-B")).toBeVisible();
    expect(within(review).getByText("currently-active-model")).toBeVisible();
    expect(within(review).getByText(/启用后会自动提交评分任务/)).toBeVisible();
    expect(within(review).getByText("已通过启用资格检查")).toBeVisible();
    fireEvent.click(within(review).getByRole("button", { name: /取\s*消/ }));
    expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
    fireEvent.click(within(row).getByRole("button", { name: /正式启用/ }));
    const confirmation = await screen.findByRole("dialog", { name: "确认启用模型" });
    fireEvent.click(within(confirmation).getByRole("button", { name: "确认启用并生成评分" }));
    await waitFor(() => expect(mockApi.scoringActivateModel).toHaveBeenCalledWith(model.id, "ridge", "settings:ridge"));
    await waitFor(() => expect(mockApi.scoringCreateTask).toHaveBeenCalledWith(expect.objectContaining({ hyperparameters: expect.objectContaining({ expected_model_run_id: model.id }) })));
  });

  it("opens this training's rejected candidate for review instead of a prior eligible model", async () => {
    const rejected = { ...candidate("new-rejected"), status: "rejected", production_eligible: false, readiness_message: "本次验证未通过" };
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: [candidate("older-eligible"), rejected] });
    mockApi.scoringListTasks.mockResolvedValue([{ id: "new-training", status: "done", stage: "done", percent: 100, payload_json: { train_model: true, materialize_scores: false, factor_set_id: "fs-B" }, result: { completion_status: "TRAINING_REJECTED", model: { model_run_id: rejected.id, status: "rejected" } } }]);
    render(<FactorModelSettings />);
    await goPage(2);
    fireEvent.click(await screen.findByRole("button", { name: "查看本次模型验证" }));
    await waitFor(() => expect(mockApi.scoringGetModelRelations).toHaveBeenCalledWith(rejected.id));
    const drawer = await screen.findByTestId("relations-drawer");
    expect(within(drawer).getByText("本次验证未通过")).toBeInTheDocument();
    expect(within(drawer).queryByRole("button", { name: /确认正式启用/ })).not.toBeInTheDocument();
    expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
  });

  it("preserves the selected collection and both windows when using previous and next", async () => {
    render(<FactorModelSettings />);
    await goPage(2);
    await chooseSet();
    const windows = screen.getAllByRole("spinbutton");
    fireEvent.change(windows[0], { target: { value: "420" } });
    fireEvent.blur(windows[0]);
    fireEvent.change(windows[1], { target: { value: "60" } });
    fireEvent.blur(windows[1]);
    fireEvent.click(screen.getByRole("button", { name: /上一页/ }));
    await screen.findByTestId("wizard-page-prepare");
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    await screen.findByTestId("wizard-page-train");

    expect(screen.getByTestId("factor-set-select").querySelector(".ant-select-selection-item")).toHaveTextContent("价值集合");
    expect(screen.getAllByRole("spinbutton")[0]).toHaveValue("420");
    expect(screen.getAllByRole("spinbutton")[1]).toHaveValue("60");
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
    expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
  });

  it("resumes running training on its own page and never shows its progress on the input page", async () => {
    const task = {
      id: "running-training", task_type: "factor_pipeline", status: "running", stage: "train", percent: 75,
      payload_json: { train_model: true, materialize_scores: false, factor_set_id: "fs-B" }, result: null,
    };
    mockApi.scoringListTasks.mockResolvedValue([task]);
    mockApi.scoringGetTask.mockResolvedValue(task);
    render(<FactorModelSettings />);

    await screen.findByText("任务：running-training");
    onlyPage("train");
    expect(screen.getByTestId("pipeline-step-4")).toHaveClass("ps-state-running");
    expect(screen.getByRole("button", { name: /下一步/ })).toBeDisabled();
    await goPage(1);
    onlyPage("prepare");
    expect(screen.queryByText("任务：running-training")).not.toBeInTheDocument();
    await goPage(2);
    expect(screen.getByTestId("pipeline-step-4")).toHaveClass("ps-state-running");
    expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
  });

  it("finishes training on the training page and requires a separate review and human enable action", async () => {
    const trainedModel = candidate();
    let completed = false;
    const done = {
      id: "training-B", task_type: "factor_pipeline", status: "done", stage: "done", percent: 100,
      payload_json: { train_model: true, materialize_scores: false, factor_set_id: "fs-B" },
      result: { completion_status: "AWAITING_MODEL_ACTIVATION", model: { model_run_id: trainedModel.id, status: "validated" } },
    };
    mockApi.scoringGetFactorModelListAsFactor.mockImplementation(async () => ({ items: completed ? [trainedModel] : [] }));
    mockApi.scoringCreateTask.mockImplementation(async (payload: Record<string, unknown>) => payload.train_model
      ? { ...done, status: "running", stage: "train", percent: 75, result: null }
      : { id: "score-B", status: "queued", stage: "queued", percent: 0, payload_json: payload, result: null });
    mockApi.scoringGetTask.mockImplementation(async () => { completed = true; return done; });
    mockApi.scoringActivateModel.mockResolvedValue({ weight_mode: "ridge", active_model_run_id: trainedModel.id, version: 2 });
    render(<FactorModelSettings />);
    await goPage(2);
    await chooseSet();
    const start = screen.getByRole("button", { name: /启动流水线.*训练模式/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);

    await screen.findByText(/训练验证完成，等待激活/, {}, { timeout: 5000 });
    onlyPage("train");
    expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
    const next = screen.getByRole("button", { name: /下一步/ });
    await waitFor(() => expect(next).toBeEnabled());
    fireEvent.click(next);
    await screen.findByTestId("wizard-page-review");
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /正式启用/ }));
    expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
    await confirmModelActivation();
    await waitFor(() => expect(mockApi.scoringActivateModel).toHaveBeenCalledWith(trainedModel.id, "ridge", "settings:ridge"));
  }, 15000);

  it("allows inspecting the score step but blocks publication without an eligible active model", async () => {
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: [{
      ...candidate("demo"), production_eligible: false, readiness_message: "演示模型不可正式运行",
    }] });
    render(<FactorModelSettings />);
    await goPage(4);
    onlyPage("score");
    expect(screen.queryByTestId("factor-set-select")).not.toBeInTheDocument();
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /启动流水线.*不训练模式/ })).toBeDisabled();
    expect(screen.getByTestId("pipeline-no-train-banner")).toHaveTextContent("当前没有已激活且可评分的模型");
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
  });

  it("opens an active model's score page directly and pins the submitted score update to its actual model and set", async () => {
    const active = candidate("active-A", "fs-A");
    mockApi.scoringGetOverviewAsFactor.mockResolvedValue({ ...overview,
      runtime: { weight_mode: "shadow", score_weight_mode: "manual", active_model_run_id: active.id, version: 2 },
    });
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: [active] });
    render(<FactorModelSettings />);
    await screen.findByTestId("wizard-page-score");
    onlyPage("score");
    expect(screen.getByRole("button", { name: /第 4 步：生成评分/ })).toHaveAttribute("aria-current", "step");
    const start = screen.getByRole("button", { name: /启动流水线.*不训练模式/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    await waitFor(() => expect(mockApi.scoringCreateTask).toHaveBeenCalledWith(expect.objectContaining({
      train_model: false, materialize_scores: true, factor_set_id: "fs-A",
      hyperparameters: { expected_model_run_id: "active-A", expected_weight_mode: "shadow", factor_set_id: "fs-A" },
    })));
    expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
  });

  it("keeps historical training evidence separate from a different collection draft and other operation pages", async () => {
    mockApi.scoringListTasks.mockResolvedValue([{
      id: "old-training-A", task_type: "factor_pipeline", status: "done", stage: "done", percent: 100,
      payload_json: { train_model: true, materialize_scores: false, factor_set_id: "fs-A" },
      result: { completion_status: "AWAITING_MODEL_ACTIVATION", model: { model_run_id: "candidate-A", status: "validated" } },
    }]);
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: [candidate("candidate-A", "fs-A")] });
    render(<FactorModelSettings />);
    await goPage(2);
    await chooseSet();
    expect(screen.getByRole("region", { name: "最近一次运行" })).toHaveTextContent("训练新模型");
    expect(screen.getByText("任务：old-training-A").closest(".factor-task-strip")).toHaveTextContent("评分未生成");
    expect(screen.getByRole("button", { name: /下一步/ })).toBeDisabled();
    await goPage(4);
    expect(screen.queryByText("任务：old-training-A")).not.toBeInTheDocument();
    expect(screen.queryByText(/评分已生成/)).not.toBeInTheDocument();
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
    expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
  });

  it("blocks table and drawer activation during training and again when the overview cannot be confirmed", async () => {
    const model = candidate();
    const running = {
      id: "unfinished-training", task_type: "factor_pipeline", status: "running", stage: "train", percent: 75,
      payload_json: { train_model: true, materialize_scores: false, factor_set_id: "fs-B" }, result: null,
    };
    const done = { ...running, status: "done", stage: "done", percent: 100,
      result: { completion_status: "AWAITING_MODEL_ACTIVATION", model: { model_run_id: model.id, status: "validated" } },
    };
    let finished = false;
    mockApi.scoringListFactorSetsAsFactor.mockResolvedValue([sets[1]]);
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: [model] });
    mockApi.scoringListTasks.mockImplementation(async () => [finished ? done : running]);
    mockApi.scoringGetTask.mockImplementation(async () => finished ? done : running);
    render(<FactorModelSettings />);
    await screen.findByText("任务：unfinished-training");
    await goPage(3);
    expect(screen.getByRole("button", { name: /正式启用/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /正式启用/ }));
    fireEvent.click(screen.getByTestId(`view-relations-${model.id}`));
    const firstDrawer = await screen.findByTestId("relations-drawer");
    expect(within(firstDrawer).getByRole("button", { name: /确认正式启用/ })).toBeDisabled();
    expect(within(firstDrawer).getByRole("button", { name: /确认影子运行/ })).toBeDisabled();
    fireEvent.click(within(firstDrawer).getByRole("button", { name: /确认正式启用/ }));
    expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));

    finished = true;
    await waitFor(() => expect(screen.getByRole("button", { name: /正式启用/ })).toBeEnabled(), { timeout: 5000 });
    mockApi.scoringGetOverviewAsFactor.mockRejectedValue(new Error("仓库概览暂时无法读取"));
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    await screen.findByText("仓库概览暂时无法读取");
    expect(screen.getByRole("button", { name: /正式启用/ })).toBeDisabled();
    fireEvent.click(screen.getByTestId(`view-relations-${model.id}`));
    const failedReadDrawer = await screen.findByTestId("relations-drawer");
    expect(within(failedReadDrawer).getByRole("button", { name: /确认正式启用/ })).toBeDisabled();
    expect(within(failedReadDrawer).getByRole("button", { name: /确认影子运行/ })).toBeDisabled();
    expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
    expect(mockApi.scoringCreateTask).not.toHaveBeenCalled();
  }, 15000);

  it("accepts only one activation when different eligible models are enabled in rapid succession", async () => {
    const modelB = candidate();
    const modelC = candidate("candidate-C");
    mockApi.scoringListFactorSetsAsFactor.mockResolvedValue([sets[1]]);
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: [modelB, modelC] });
    let resolveActivation!: (runtime: Record<string, unknown>) => void;
    mockApi.scoringActivateModel.mockImplementation(() => new Promise((resolve) => { resolveActivation = resolve; }));
    render(<FactorModelSettings />);
    await goPage(3);
    await screen.findByText(modelC.id);
    const first = within(screen.getByText(modelB.id).closest("tr")!).getByRole("button", { name: /正式启用/ });
    const second = within(screen.getByText(modelC.id).closest("tr")!).getByRole("button", { name: /正式启用/ });
    expect(first).toBeEnabled();
    expect(second).toBeEnabled();
    act(() => {
      fireEvent.click(first);
      fireEvent.click(second);
      fireEvent.click(first);
    });
    expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
    const review = await screen.findByRole("dialog", { name: "确认启用模型" });
    expect(within(review).getByText(modelB.id)).toBeInTheDocument();
    expect(within(review).queryByText(modelC.id)).not.toBeInTheDocument();
    await confirmModelActivation();
    expect(mockApi.scoringActivateModel).toHaveBeenCalledTimes(1);
    expect(mockApi.scoringActivateModel).toHaveBeenCalledWith(modelB.id, "ridge", "settings:ridge");
    expect(second).toBeDisabled();
    await act(async () => resolveActivation({ weight_mode: "ridge", active_model_run_id: modelB.id, version: 2 }));
    await waitFor(() => expect(mockApi.scoringCreateTask).toHaveBeenCalledTimes(1));
    expect(mockApi.scoringCreateTask).toHaveBeenCalledWith(expect.objectContaining({
      hyperparameters: expect.objectContaining({ expected_model_run_id: modelB.id, expected_weight_mode: "ridge" }),
    }));
    expect(mockApi.scoringActivateModel).toHaveBeenCalledTimes(1);
  });
});
