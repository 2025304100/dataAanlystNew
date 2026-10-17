import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("../../context/AppContext", () => ({ useApp: () => ({ locale: "zh-CN", showToast: vi.fn() }) }));

import FactorModelSettings from "../FactorModelSettings";
import { setLocale } from "../../i18n";

beforeEach(() => localStorage.clear());
afterEach(() => { vi.unstubAllGlobals(); setLocale("zh-CN"); });

it.each(["exact", "english", "legacy", "zero", "invalid", "pre_gate", "failed", "wrong_model"])("reads only the exact model's cutoff and training evidence through the real HTTP client (%s detail)", async (outcome) => {
  if (outcome === "english") setLocale("en-US");
  const modelId = "ridge-4afc1347543b6ccdff057c6f";
  const detailRequests: string[] = [];
  const writes: string[] = [];
  const respond = (data: unknown) => new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
  vi.stubGlobal("fetch", vi.fn(async (input: string | Request, options?: RequestInit) => {
    const path = (typeof input === "string" ? input : input.url).split("?")[0];
    if (options?.method && options.method !== "GET") { writes.push(path); throw new Error(`Unexpected write: ${path}`); }
    if (path.endsWith("/scoring/overview")) return respond({ feature_enabled: true, warehouse_available: true, health_status: "healthy", warehouse_path: "factor.duckdb", weight_mode: "manual", runtime_active_model_id: null, runtime_version: 11, latest_trade_date: "2026-10-14", factor_coverage: [] });
    if (path.endsWith("/scoring/models")) return respond([{
      id: modelId, name: modelId, status: "rejected", production_eligible: false, training_mode: "warehouse", readiness_code: "MODEL_NOT_VALIDATED", readiness_message: "Model status is rejected", validation_ic: -0.0024479, sample_count: 1348059, factorset_id: "fs-turnover-v3", data_cutoff_at: "2026-10-15T16:26:21",
      // The real scoring brief intentionally has no hyperparameters/cutoff date.
    }]);
    if (path.endsWith("/scoring/factorsets") || path.endsWith("/scoring/tasks")) return respond([]);
    if (path.endsWith("/scoring/pipeline/eta")) return respond({ recommended_seconds: 1200, sample_count: 0 });
    if (path.endsWith(`/scoring/models/${modelId}/relations`)) return respond({ model_id: modelId, factor_set_id: "fs-turnover-v3", factor_set_name: "换手率 v3", n_members: 1, factors: [{ factor_code: "turnover_z20", factor_version_id: 100031 }], unbound_reason: null });
    if (path.endsWith(`/factor-models/${modelId}`)) {
      detailRequests.push(modelId);
      if (outcome === "failed") return new Response(JSON.stringify({ detail: "Model detail unavailable" }), { status: 503 });
      // Native detail is a date source, never an override of scoring eligibility.
      const counts = outcome === "zero" ? { training_sample_count: 0, validation_sample_count: 0, validation_date_count: 0, inner_training_sample_count: 0, tuning_sample_count: 0 } : outcome === "invalid" ? { training_sample_count: -4, validation_sample_count: "260245", validation_date_count: 50.5, inner_training_sample_count: true, tuning_sample_count: null } : { training_sample_count: 1060384, validation_sample_count: 260245, validation_date_count: 50, inner_training_sample_count: 800000, tuning_sample_count: 250000 };
      const qualityLimitations: Record<string, unknown> = { legacy: "  \n  ", zero: 0, invalid: { message: "not a string" }, english: false, wrong_model: "Another model's source conflict" };
      return respond({ id: outcome === "wrong_model" ? "another-model" : modelId, status: outcome === "pre_gate" ? "rejected" : "validated", production_eligible: outcome !== "pre_gate", rejection_reason: outcome === "pre_gate" ? "[P2-G Pre] ic_gate: below original threshold" : null, artifact_path: outcome === "pre_gate" ? null : undefined, hyperparameters: { factor_set_id: "fs-turnover-v3", data_cutoff_date: "2026-10-14", research_data_quality_limitations: qualityLimitations[outcome], ...(outcome === "legacy" ? {} : { protocol_version: "ridge-temporal-holdout-v2", coefficient_stage: "outer_training_purged" }) }, data_cutoff_at: "2026-10-15T16:26:21", metrics: outcome === "legacy" ? { validation_ic: -0.0024479 } : { validation_ic: -0.0024479, ...counts, validation_icir: 0.09865815, validation_ic_method: "pooled_spearman", validation_icir_method: "daily_spearman_mean_over_sample_std_ddof1" }, sample_count: 1348059, weights: [] });
    }
    throw new Error(`Unexpected mocked HTTP request: ${path}`);
  }));

  render(<FactorModelSettings />);
  fireEvent.click(await screen.findByRole("button", { name: /第 3 步：验证与启用/ }));
  fireEvent.click(await screen.findByTestId(`view-relations-${modelId}`));
  const drawer = await screen.findByTestId("relations-drawer");
  await within(drawer).findByText("turnover_z20");
  if (outcome === "exact") {
    expect(within(drawer).getByText("2026-10-14")).toBeVisible();
    expect(within(drawer).getByText("总样本")).toBeVisible();
    expect(within(drawer).getByText("实际拟合样本")).toBeVisible();
    expect(within(drawer).getByText("1,060,384")).toBeVisible();
    expect(within(drawer).getByText("260,245")).toBeVisible();
    expect(within(drawer).getByText("50")).toBeVisible();
    expect(within(drawer).getByText("0.0987")).toBeVisible();
    expect(within(drawer).getByText("800,000")).toBeVisible();
    expect(within(drawer).getByText("250,000")).toBeVisible();
    expect(within(drawer).getByText("ridge-temporal-holdout-v2")).toBeVisible();
    expect(within(drawer).getByText("外层训练集拟合")).toBeVisible();
    expect(within(drawer).getByText("池化 Spearman")).toBeVisible();
    expect(within(drawer).getByText("每日 IC 均值 / 样本标准差")).toBeVisible();
  } else if (outcome === "english") {
    expect(within(drawer).getByText("Total samples")).toBeVisible();
    expect(within(drawer).getByText("Actual fit samples")).toBeVisible();
    expect(within(drawer).getByText("Holdout samples")).toBeVisible();
    expect(within(drawer).getByText("Outer training fit")).toBeVisible();
    expect(within(drawer).getByText("Pooled Spearman")).toBeVisible();
    expect(within(drawer).getByText("Daily IC mean / sample standard deviation")).toBeVisible();
  } else if (outcome === "pre_gate") {
    expect(within(drawer).getByText("训练段准入样本")).toBeVisible();
    expect(within(drawer).getByText("1,060,384")).toBeVisible();
    expect(within(drawer).getByText("未拟合（准入未通过）")).toBeVisible();
    expect(within(drawer).queryByText("实际拟合样本")).not.toBeInTheDocument();
    expect(within(drawer).queryByText("外层训练集拟合")).not.toBeInTheDocument();
  } else if (["legacy", "zero", "invalid"].includes(outcome)) {
    expect(within(drawer).getByText("2026-10-14")).toBeVisible();
    const evidence = within(drawer).getByTestId("model-training-evidence");
    if (outcome === "zero") expect(within(evidence).getAllByText("0")).toHaveLength(5);
    else {
      expect(within(evidence).getAllByText("未记录").length).toBeGreaterThanOrEqual(5);
      expect(within(evidence).queryByText("1,348,059")).not.toBeInTheDocument();
    }
    if (outcome === "legacy") {
      expect(within(evidence).queryByText("ridge-temporal-holdout-v2")).not.toBeInTheDocument();
      expect(within(evidence).queryByText("外层训练集拟合")).not.toBeInTheDocument();
    }
  }
  else {
    expect(within(drawer).getByText("未记录样本截止日")).toBeVisible();
    expect(within(drawer).queryByText("2026-10-14")).not.toBeInTheDocument();
    expect(within(drawer).queryByText("1,060,384")).not.toBeInTheDocument();
    expect(within(drawer).queryByText("ridge-temporal-holdout-v2")).not.toBeInTheDocument();
  }
  await waitFor(() => expect(detailRequests).toEqual([modelId]));
  expect(within(drawer).queryByTestId("model-research-warning")).not.toBeInTheDocument();
  expect(within(drawer).queryByTestId("model-data-quality-warning")).not.toBeInTheDocument();
  expect(within(drawer).queryByText("逐日横截面居中秩")).not.toBeInTheDocument();
  expect(within(drawer).queryByRole("button", { name: /确认正式启用/ })).not.toBeInTheDocument();
  expect(writes).toHaveLength(0);
});
