import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../client";

afterEach(() => vi.unstubAllGlobals());

function response(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

describe("public scoring data used by the training and activation journey", () => {
  it("keeps the frozen collection identity returned by the public model list", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response([{
      id: "warehouse-model", status: "validated", production_eligible: true,
      factorset_id: "frozen-value-v2", factorset_label: "价值集合", factorset_member_count: 2,
    }])));
    const list = await api.scoringGetFactorModelListAsFactor();
    expect(list.items[0].hyperparameters?.factor_set_id).toBe("frozen-value-v2");
  });

  it("preserves the runtime revision and actual score mode for activation refreshes", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response({
      feature_enabled: true, warehouse_available: true, weight_mode: "manual",
      runtime_mode: "shadow", runtime_active_model_id: "warehouse-model",
      score_weight_mode: "manual", active_model_id: null, runtime_version: 8,
    })));
    const overview = await api.scoringGetOverviewAsFactor();
    expect(overview.runtime.version).toBe(8);
    expect(overview.runtime.score_weight_mode).toBe("manual");
    expect(overview.runtime.active_model_run_id).toBe("warehouse-model");
    expect(overview.runtime.weight_mode).toBe("shadow");
  });

  it("keeps an explicitly cleared runtime model pointer instead of restoring a legacy score model", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response({
      runtime_mode: "manual", runtime_active_model_id: null, score_weight_mode: "manual",
      active_model_id: "previous-score-model", weight_mode: "ridge", runtime_version: 9,
    })));

    const overview = await api.scoringGetOverviewAsFactor();

    expect(overview.runtime.active_model_run_id).toBeNull();
    expect(overview.runtime.weight_mode).toBe("manual");
  });

  it("still reads the legacy active model when the authoritative runtime field is absent", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response({
      weight_mode: "ridge", active_model_id: "legacy-model", runtime_version: 7,
    })));

    const overview = await api.scoringGetOverviewAsFactor();

    expect(overview.runtime.active_model_run_id).toBe("legacy-model");
    expect(overview.runtime.weight_mode).toBe("ridge");
  });

  it("shows the measured coverage from the public overview rather than a missing reading", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response({
      feature_enabled: true, warehouse_available: true, active_factor_coverage_count: 4,
      active_factor_avg_coverage: 0.75,
    })));
    const overview = await api.scoringGetOverviewAsFactor();
    expect(overview.average_coverage).toBe(0.75);
  });

  it("reads collection qualification through the public route without starting training", async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => response({
      ready: false, factor_set_id: "frozen/value", code: "NOT_TRAINABLE", message: "成员版本不可训练", details: {},
    }));
    vi.stubGlobal("fetch", fetcher);
    const result = await api.scoringGetFactorSetReadiness("frozen/value");
    expect(result.ready).toBe(false);
    expect(result.message).toBe("成员版本不可训练");
    expect(fetcher.mock.calls[0][0]).toContain("/scoring/factorsets/frozen%2Fvalue/readiness");
    const options = fetcher.mock.calls[0][1] as RequestInit;
    expect(options.method ?? "GET").toBe("GET");
    expect(options.body).toBeUndefined();
  });
});
