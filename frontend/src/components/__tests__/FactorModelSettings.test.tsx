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
    })),
    cancelFactorPipelineTask: vi.fn(),
    updateFactorSystemConfig: vi.fn(async () => ({})),
    initializeFactorWarehouse: vi.fn(async () => ({})),
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
  });

  it("loads runtime and supports shadow activation and pipeline start", async () => {
    render(<FactorModelSettings />);

    await waitFor(() => {
      expect(screen.getByText("ridge-20260714")).toBeInTheDocument();
    });
    expect(screen.getByText("90.0%")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /影子运行/ }));
    await waitFor(() => {
      expect(mockApi.activateFactorModel).toHaveBeenCalledWith(
        "ridge-20260714",
        "shadow",
        "settings:shadow",
      );
    });

    fireEvent.click(screen.getByRole("button", { name: /运行流水线/ }));
    await waitFor(() => {
      expect(mockApi.createFactorPipelineTask).toHaveBeenCalledWith(
        expect.objectContaining({
          full_refresh: false,
          train_model: true,
          materialize_scores: true,
          window_days: 250,
          validation_days: 50,
        }),
      );
    });
  });

  it("blocks pipeline while disabled and can enable the feature", async () => {
    mockApi.getFactorOverview.mockResolvedValueOnce({
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

    const runButton = await screen.findByRole("button", { name: /运行流水线/ });
    expect(runButton).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "启用因子功能" }));
    await waitFor(() => {
      expect(mockApi.updateFactorSystemConfig).toHaveBeenCalledWith(true);
    });
  });

  it("offers warehouse initialization when enabled but unavailable", async () => {
    mockApi.getFactorOverview.mockResolvedValueOnce({
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
      expect(mockApi.initializeFactorWarehouse).toHaveBeenCalledTimes(1);
    });
  });
});
