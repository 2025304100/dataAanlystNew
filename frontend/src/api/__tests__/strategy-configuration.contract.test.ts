import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../client";

afterEach(() => vi.unstubAllGlobals());

describe("真实配置写入的HTTP契约", () => {
  it("历史训练薄封默认请求真实仓库并返回异步任务", async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ id: "queued-real-task", status: "queued" }), { status: 202, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const task = await api.trainFactorModel("frozen-set");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/scoring/factorsets/frozen-set/train");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toMatchObject({ mode: "warehouse" });
    expect(task).toMatchObject({ id: "queued-real-task", status: "queued" });
    await api.scoringTrainModel("frozen-set");
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toMatchObject({ mode: "warehouse" });
  });
  it("应用组合策略以单一事务请求提交且保持显式关闭接管", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ rule_id: 8, applied: true, binding_result: null, requires_confirmation: true, auto_trade_enabled: false, signal_rule_id: 9 }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const payload = { rule: { is_active: true }, signal_rule: { mode: "general" }, binding: { factor_model_run_id: "actual-model", factor_set_id: "actual-set", run_mode: "production_sim" as const, pit_mode: "strict_pit_safe" as const }, auto_trade_enabled: false };
    const result = await api.savePortfolioStrategyConfiguration(7, payload);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/portfolios/7/strategy-configuration");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(payload);
    expect(result.auto_trade_enabled).toBe(false);
  });
  it("克隆因子集合提交存在的scoring路由和新集合ID", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: "cloned-set" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await api.cloneFactorSet("real-set", { actor: "本人" });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/scoring/factorsets/real-set/clone");
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body).toMatchObject({ actor: "本人" });
    expect(body.new_factorset_id).toMatch(/^fs-/);
  });
});
