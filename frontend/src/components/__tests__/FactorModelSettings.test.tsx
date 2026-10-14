import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const { mockApi, showToast } = vi.hoisted(() => ({
  showToast: vi.fn(),
  mockApi: {
    getFactorOverview: vi.fn(async (): Promise<any> => ({
      config: {
        feature_enabled: true,
        warehouse_path: "factor.duckdb",
        updated_by: "test",
        updated_at: null,
      },
      feature_enabled: true,
      warehouse_error: null,
      runtime: {
        weight_mode: "manual",
        score_weight_mode: "manual",
        active_model_run_id: null,
        updated_by: "environment",
        fallback_reason: null,
        version: 1,
        updated_at: null,
      },
      health: {
        status: "healthy",
        warehouse_available: true,
        warehouse_path: "factor.duckdb",
        schema_version: "1",
        calc_batch_id: "batch-1",
        latest_bar_date: "2026-07-14",
        raw_tables: [],
        factors: [],
        reasons: [],
      },
      latest_trade_date: "2026-07-14",
      factor_coverage: [
        {
          factor_code: "ep_ttm",
          latest_trade_date: "2026-07-14",
          universe_symbols: 100,
          eligible_symbols: 90,
          imputed_symbols: 0,
          coverage: 0.9,
        },
      ],
    })),
    getFactorModels: vi.fn(async () => ({
      runtime: {
        weight_mode: "manual",
        score_weight_mode: "manual",
        active_model_run_id: null,
        updated_by: "environment",
        fallback_reason: null,
        version: 1,
        updated_at: null,
      },
      items: [
        {
          id: "ridge-20260714",
          model_type: "ridge",
          asset_type: "stock",
          target_code: "target_5d_return",
          train_start_date: "2025-01-01",
          train_end_date: "2026-04-30",
          validation_start_date: "2026-05-01",
          validation_end_date: "2026-07-14",
          data_cutoff_at: "2026-07-14T18:00:00",
          feature_versions: {},
          hyperparameters: {},
          metrics: { validation_ic: 0.08 },
          sample_count: 10000,
          symbol_count: 300,
          trade_date_count: 250,
          status: "validated",
          production_eligible: true,
          rejection_reason: null,
          artifact_path: null,
          created_at: "2026-07-14T18:00:00",
          activated_at: null,
          weights: [],
        },
      ],
    })),
    listFactorPipelineTasks: vi.fn(async () => []),
    getFactorPipelineTask: vi.fn(),
    getFactorPipelineEta: vi.fn(async () => ({
      avg_seconds: 0,
      median_seconds: 0,
      sample_count: 0,
      fallback_seconds: 1200,
      recommended_seconds: 1200,
      train_model: true,
      full_refresh: false,
    })),
    createFactorPipelineTask: vi.fn(async () => ({
      id: "task-1",
      task_type: "factor_pipeline",
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
      created_at: null,
      started_at: null,
      finished_at: null,
      updated_at: null,
    })),
    scoringActivateModel: vi.fn(async () => ({})),
    scoringCancelTask: vi.fn(async () => ({})),
    scoringCreateTask: vi.fn(async () => ({})),
    scoringFallbackToManual: vi.fn(async () => ({})),
    scoringGetModelRelations: vi.fn(async () => ({})),
    scoringGetPipelineEta: vi.fn(async () => ({})),
    scoringGetTask: vi.fn(async () => ({})),
    scoringInitializeWarehouse: vi.fn(async () => ({})),
    scoringUpdateSystemConfig: vi.fn(async () => ({})),
    cancelFactorPipelineTask: vi.fn(),
    updateFactorSystemConfig: vi.fn(async () => ({})),
    initializeFactorWarehouse: vi.fn(async () => ({})),
    scoringGetOverviewAsFactor: vi.fn(async (): Promise<any> => ({ feature_enabled: true, config: {}, runtime: {}, health: {}, latest_trade_date: null, factor_coverage: [] })),
    scoringListTasks: vi.fn(async () => []),  // 组件对 tasks 直接 .find()，必须返回数组,
    getInboxNotifications: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    scoringGetFactorDefinition: vi.fn(async () => ({})),
    scoringGetFactorModelListAsFactor: vi.fn(async (): Promise<any> => ({ items: [], total: 0, page: 1, page_size: 20 })),
    scoringListFactorSetsAsFactor: vi.fn(async () => [
      {
        id: "fs-20d",
        name: "20日换手率Z分数集合",
        label: "20日换手率Z分数集合",
        status: "frozen",
        n_members: 5,
        feature_count: 5,
      },
    ]),
    getEvaluationTaskHeartbeat: vi.fn(async () => ({})),
    activateFactorModel: vi.fn(async () => ({})),
    fallbackFactorModel: vi.fn(async () => ({})),
  },
}));

vi.mock("../../api/client", () => ({ api: mockApi }));
vi.mock("../../context/AppContext", () => ({
  useApp: () => ({ locale: "zh-CN", showToast }),
}));

import FactorModelSettings from "../FactorModelSettings";

describe("FactorModelSettings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.scoringCreateTask.mockResolvedValue({ id: "settings-score-task", status: "queued", stage: "queued", percent: 0 } as any);
  });
  it("validated 演示模型或未知资格模型仍不能激活正式运行", async () => {
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValueOnce({ items: [{ id: "demo-model", status: "validated", production_eligible: false, training_mode: "offline_minimal", readiness_message: "演示模型不可用于正式运行", metrics: {}, sample_count: 250, weights: [] }] });
    render(<FactorModelSettings initialTrainModel={false} />);
    await screen.findByText("demo-model");
    const productionButton = screen.getByRole("button", { name: /正式启用|生产运行|生产激活|Ridge/ });
    expect(productionButton).toBeDisabled();
    fireEvent.click(productionButton);
    expect(mockApi.scoringActivateModel).not.toHaveBeenCalled();
  });
  it("loads runtime and starts scoring for the exact activated shadow model", async () => {
    mockApi.scoringGetOverviewAsFactor.mockResolvedValue({
      config: {
        feature_enabled: true,
        warehouse_path: "factor.duckdb",
        updated_by: "test",
        updated_at: null,
      },
      feature_enabled: true,
      warehouse_error: null,
      runtime: {
        weight_mode: "manual",
        score_weight_mode: "manual",
        active_model_run_id: null,
        updated_by: "environment",
        fallback_reason: null,
        version: 1,
        updated_at: null,
      },
      health: {
        status: "healthy",
        warehouse_available: true,
        warehouse_path: "factor.duckdb",
        schema_version: "1",
        calc_batch_id: "batch-1",
        latest_bar_date: "2026-07-14",
        raw_tables: [],
        factors: [],
        reasons: [],
      },
      latest_trade_date: "2026-07-14",
      factor_coverage: [
        {
          factor_code: "ep_ttm",
          latest_trade_date: "2026-07-14",
          universe_symbols: 100,
          eligible_symbols: 90,
          imputed_symbols: 0,
          coverage: 0.9,
        },
      ],
    });
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({
      items: [
        {
          id: "ridge-20260714",
          model_type: "ridge",
          asset_type: "stock",
          target_code: "target_5d_return",
          data_cutoff_at: "2026-07-14T18:00:00",
          feature_versions: {},
          hyperparameters: { factor_set_id: "fs-20d" },
          metrics: { validation_ic: 0.08 },
          sample_count: 10000,
          symbol_count: 300,
          trade_date_count: 250,
          status: "validated",
          production_eligible: true,
          rejection_reason: null,
          artifact_path: null,
          created_at: "2026-07-14T18:00:00",
          activated_at: null,
          weights: [],
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
    });

    render(<FactorModelSettings />);

    await waitFor(() => {
      expect(screen.getByText("ridge-20260714")).toBeInTheDocument();
    });
    expect(screen.getByText("90.0%")).toBeInTheDocument();

    mockApi.scoringActivateModel.mockResolvedValue({ weight_mode: "shadow", active_model_run_id: "ridge-20260714", version: 2 } as any);
    fireEvent.click(screen.getByRole("button", { name: /影子运行/ }));
    await waitFor(() => {
      expect(mockApi.scoringActivateModel).toHaveBeenCalledWith(
        "ridge-20260714",
        "shadow",
        "settings:shadow",
      );
    });

    await waitFor(() => {
      expect(mockApi.scoringCreateTask).toHaveBeenCalledWith(
        expect.objectContaining({
          train_model: false,
          materialize_scores: true,
          factor_set_id: "fs-20d",
          hyperparameters: expect.objectContaining({ expected_model_run_id: "ridge-20260714", expected_weight_mode: "shadow", factor_set_id: "fs-20d" }),
          factor_scope: expect.objectContaining({ asset_type: "stock", factor_set_id: "fs-20d" }),
        }),
      );
      expect(screen.getByText("任务：settings-score-task")).toBeInTheDocument();
      expect(showToast).toHaveBeenCalledWith("success", expect.stringMatching(/模型已激活.*settings-score-task.*生成/));
    });
  });

  it("retains production activation and identifies a failed scoring submission separately", async () => {
    const overview = await mockApi.getFactorOverview();
    mockApi.scoringGetOverviewAsFactor.mockResolvedValue(overview);
    const modelList = await mockApi.getFactorModels();
    modelList.items[0].hyperparameters = { factor_set_id: "fs-20d" };
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue(modelList as any);
    mockApi.scoringActivateModel.mockResolvedValue({ weight_mode: "ridge", active_model_run_id: "ridge-20260714", version: 2 } as any);
    mockApi.scoringCreateTask.mockRejectedValueOnce(new Error("PIPELINE_BUSY"));
    render(<FactorModelSettings />);
    fireEvent.click(await screen.findByRole("button", { name: /正式启用|生产运行|生产激活|Ridge/ }));
    await screen.findByText(/模型已激活，评分生成未启动.*PIPELINE_BUSY/);
    await waitFor(() => expect(screen.getAllByText("ridge-20260714").length).toBeGreaterThanOrEqual(2));
    expect(mockApi.scoringGetOverviewAsFactor).toHaveBeenCalledTimes(2);
    expect(mockApi.scoringCreateTask).toHaveBeenCalledWith(expect.objectContaining({
      hyperparameters: expect.objectContaining({ expected_model_run_id: "ridge-20260714", expected_weight_mode: "ridge" }),
    }));
  });

  it("blocks pipeline while disabled and can enable the feature", async () => {
    mockApi.scoringGetOverviewAsFactor.mockResolvedValue({
      config: {
        feature_enabled: false,
        warehouse_path: "factor.duckdb",
        updated_by: "environment",
        updated_at: null,
      },
      feature_enabled: false,
      warehouse_error: "warehouse_not_initialized",
      runtime: {
        weight_mode: "manual",
        score_weight_mode: "manual",
        active_model_run_id: null,
        updated_by: "environment",
        fallback_reason: null,
        version: 1,
        updated_at: null,
      },
      health: {
        status: "failed",
        warehouse_available: false,
        warehouse_path: "factor.duckdb",
        schema_version: null,
        calc_batch_id: null,
        latest_bar_date: null,
        raw_tables: [],
        factors: [],
        reasons: ["warehouse_not_initialized"],
      },
      latest_trade_date: null,
      factor_coverage: [],
    });

    render(<FactorModelSettings />);

    const runButton = await screen.findByRole("button", { name: /启动流水线/ });
    expect(runButton).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "启用因子功能" }));
    await waitFor(() => {
      expect(mockApi.scoringUpdateSystemConfig).toHaveBeenCalledWith(true, "settings:toggle");
    });
  });

  it("offers warehouse initialization when enabled but unavailable", async () => {
    mockApi.scoringGetOverviewAsFactor.mockResolvedValue({
      config: {
        feature_enabled: true,
        warehouse_path: "factor.duckdb",
        updated_by: "test",
        updated_at: null,
      },
      feature_enabled: true,
      warehouse_error: "warehouse_not_initialized",
      runtime: {
        weight_mode: "manual",
        score_weight_mode: "manual",
        active_model_run_id: null,
        updated_by: "environment",
        fallback_reason: null,
        version: 1,
        updated_at: null,
      },
      health: {
        status: "failed",
        warehouse_available: false,
        warehouse_path: "factor.duckdb",
        schema_version: null,
        calc_batch_id: null,
        latest_bar_date: null,
        raw_tables: [],
        factors: [],
        reasons: ["warehouse_not_initialized"],
      },
      latest_trade_date: null,
      factor_coverage: [],
    });

    render(<FactorModelSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "初始化仓库" }));
    await waitFor(() => {
      expect(mockApi.scoringInitializeWarehouse).toHaveBeenCalledTimes(1);
    });
  });
});
