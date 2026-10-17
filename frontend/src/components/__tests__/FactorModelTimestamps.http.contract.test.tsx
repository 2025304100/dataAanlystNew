import { App } from "antd";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setLocale } from "../../i18n";

vi.mock("../../context/AppContext", () => ({ useApp: () => ({ locale: "zh-CN", showToast: vi.fn() }) }));

import FactorModelSettings from "../FactorModelSettings";
import FactorModelPage from "../factors/FactorModelPage";
import trustedResearchFixture from "../../utils/__tests__/fixtures/trustedResearchCandidate.json";

beforeEach(() => {
  vi.stubEnv("TZ", "Asia/Shanghai");
  localStorage.clear();
  window.history.replaceState(null, "", window.location.pathname);
});
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); setLocale("zh-CN"); });

it.each(["settings", "factor-page"])("actual scoring brief is enriched from exact read-only model detail in the live list entry (%s)", async (entry) => {
  const { model, scoring_brief: brief } = trustedResearchFixture;
  const writes: string[] = [], detailReads: string[] = [];
  const respond = (body: unknown) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
  vi.stubGlobal("fetch", vi.fn(async (input: string | Request, init?: RequestInit) => {
    const path = (typeof input === "string" ? input : input.url).split("?")[0];
    if (init?.method && init.method !== "GET") { writes.push(path); throw new Error("Unexpected write"); }
    if (path.endsWith("/scoring/overview")) return respond({ feature_enabled: true, warehouse_available: true, health_status: "healthy", weight_mode: "manual", runtime_active_model_id: null, runtime_version: 11, latest_trade_date: "2026-10-14", factor_coverage: [] });
    if (path.endsWith("/scoring/models")) return respond([brief]);
    if (path.endsWith("/scoring/factorsets") || path.endsWith("/scoring/tasks")) return respond([]);
    if (path.endsWith("/scoring/pipeline/eta")) return respond({ recommended_seconds: 1200, sample_count: 0 });
    if (path.endsWith(`/factor-models/${model.id}`)) { detailReads.push(path); return respond(model); }
    throw new Error(`Unexpected HTTP request: ${path}`);
  }));
  if (entry === "settings") {
    render(<FactorModelSettings />);
    fireEvent.click(await screen.findByRole("button", { name: /第 3 步：验证与启用/ }));
    const row = (await screen.findByTestId(`view-relations-${model.id}`)).closest("tr")!;
    expect(await within(row).findByText("研究验证通过，待独立验证")).toBeVisible();
    expect(within(row).queryByText("已拒绝")).not.toBeInTheDocument();
    expect(within(row).getByRole("button", { name: /正式启用/ })).toBeDisabled();
  } else {
    render(<App><FactorModelPage /></App>);
    fireEvent.click(await screen.findByRole("tab", { name: "2. 验证与启用模型" }));
    const table = await screen.findByTestId("model-table-wrapper");
    const label = await within(table).findByText("研究验证通过，待独立验证");
    const row = label.closest("tr")!;
    expect(within(row).getByText("仅研究，暂不能启用或生成评分")).toBeVisible();
    expect(within(row).queryByText("已拒绝")).not.toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /正式启用|影子运行/ })).not.toBeInTheDocument();
  }
  expect(detailReads).toHaveLength(1);
  expect(writes).toHaveLength(0);
});

it.each([
  ["2026-10-16T05:10:17", "zh-CN"],
  ["2026-10-16T05:10:17Z", "zh-CN"],
  ["2026-10-16T13:10:17+08:00", "zh-CN"],
  ["2026-10-16T05:10:17", "en-US"],
] as const)("both model entry points show the same local availability time and do not invite activation of a rejected model (%s, %s)", async (timestamp, locale) => {
  setLocale(locale);
  const id = "utc-candidate";
  const writes: string[] = [];
  const respond = (body: unknown) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
  vi.stubGlobal("fetch", vi.fn(async (input: string | Request, init?: RequestInit) => {
    const path = (typeof input === "string" ? input : input.url).split("?")[0];
    if (init?.method && init.method !== "GET") { writes.push(path); throw new Error("Unexpected write"); }
    if (path.endsWith("/scoring/overview")) return respond({ feature_enabled: true, warehouse_available: true, health_status: "healthy", weight_mode: "manual", runtime_active_model_id: null, runtime_version: 11, latest_trade_date: "2026-10-14", factor_coverage: [] });
    if (path.endsWith("/scoring/models")) return respond([{ id, name: id, status: "rejected", production_eligible: false, training_mode: "warehouse", readiness_code: "MODEL_NOT_VALIDATED", validation_ic: -0.002448, factorset_id: "fs-timezone", sample_count: 1000, data_cutoff_at: timestamp }]);
    if (path.endsWith("/scoring/factorsets") || path.endsWith("/scoring/tasks")) return respond([]);
    if (path.endsWith("/scoring/pipeline/eta")) return respond({ recommended_seconds: 1200, sample_count: 0 });
    if (path.endsWith(`/scoring/models/${id}/relations`)) return respond({ model_id: id, factor_set_id: "fs-timezone", n_members: 1, factors: [{ factor_code: "turnover_z20", factor_version_id: 100073 }], unbound_reason: null });
    if (path.endsWith(`/factor-models/${id}`)) return respond({ id, status: "rejected", production_eligible: false, hyperparameters: { factor_set_id: "fs-timezone" }, data_cutoff_at: timestamp, metrics: { validation_ic: -0.002448 }, sample_count: 1000, weights: [], audit: [] });
    throw new Error(`Unexpected HTTP request: ${path}`);
  }));

  const settings = render(<FactorModelSettings />);
  fireEvent.click(await screen.findByRole("button", { name: /第 3 步：验证与启用/ }));
  fireEvent.click(await screen.findByTestId(`view-relations-${id}`));
  const drawer = await screen.findByTestId("relations-drawer");
  await within(drawer).findByText("turnover_z20");
  expect(within(drawer).getByText("2026-10-16 13:10:17")).toBeVisible();
  settings.unmount();

  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole("tab", { name: locale === "zh-CN" ? "2. 验证与启用模型" : /2\./ }));
  const table = await screen.findByTestId("model-table-wrapper");
  expect(within(table).getByText("2026-10-16 13:10:17")).toBeVisible();
  fireEvent.click(within(table).getByText(id));
  const detail = await screen.findByTestId("model-validation-detail");
  expect(within(detail).getByText("2026-10-16 13:10:17")).toBeVisible();
  expect(screen.queryByText("训练完成的新模型等待本人激活；激活后会提交固定该模型版本的评分任务。")).not.toBeInTheDocument();
  expect(within(table).getByText(locale === "zh-CN" ? "训练只生成候选。请先查看验证结果和启用资格；通过后仍需本人确认，才会启用并提交该版本的评分任务。" : "Training creates a candidate only. Review its validation results and activation eligibility; an eligible model still requires your confirmation before activation and scoring for that version.")).toBeVisible();
  expect(writes).toHaveLength(0);
});

it.each(["zh-CN", "en-US"] as const)("preserves an explicitly marked rank model's research warning through both detail and activation review entry points (%s)", async (locale) => {
  setLocale(locale);
  const id = "rank-candidate";
  const writes: string[] = [];
  const hp = { factor_set_id: "fs-rank", protocol_version: "ridge-temporal-cross-sectional-rank-v1", coefficient_stage: "outer_training_purged", fit_policy: "cross_sectional_rank_v1",
    fit_target_policy: { target: "within_trade_date_average_rank_divided_by_n_plus_one_minus_half", ties: "average", sample_weight: "n_rows / (n_dates * date_row_count)", weight_normalization: "mean_one", validation_target: "original_untransformed_target_5d_return" },
    research_evaluation_status: "previously_observed_development_comparison", research_selection_plan_sha256: "a".repeat(64),
    research_data_quality_limitations: "RAW_ENGLISH_SOURCE_CONFLICT_MUST_NOT_RENDER",
  };
  const warning = locale === "zh-CN" ? "该验收区间此前已被查看，本次仅作研究对照，不是新的盲测。形式门槛通过也不能证明可靠上线；启用仍需你本人确认。" : "This evaluation interval has already been observed and is used for research comparison, not a new blind test. Passing formal gates does not establish reliable production performance; activation still requires your confirmation.";
  const qualityWarning = locale === "zh-CN" ? "同代码同日的历史行情存在尚未核实的来源冲突，本模型仅用于研究。即使统计门槛通过，也应先核实数据来源并重新验证，再考虑启用。" : "Historical market data for the same security and date contains unresolved source conflicts. This model is for research only. Even if statistical gates pass, verify the sources and validate again before considering activation.";
  const respond = (body: unknown) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
  vi.stubGlobal("fetch", vi.fn(async (input: string | Request, init?: RequestInit) => {
    const path = (typeof input === "string" ? input : input.url).split("?")[0];
    if (init?.method && init.method !== "GET") { writes.push(path); throw new Error("Unexpected write"); }
    if (path.endsWith("/scoring/overview")) return respond({ feature_enabled: true, warehouse_available: true, health_status: "healthy", weight_mode: "manual", runtime_active_model_id: null, runtime_version: 11, latest_trade_date: "2026-10-14", factor_coverage: [] });
    if (path.endsWith("/scoring/models")) return respond([{ id, name: id, status: "validated", production_eligible: true, training_mode: "warehouse", readiness_code: "READY", validation_ic: 0.02, factorset_id: "fs-rank", sample_count: 1000, data_cutoff_at: "2026-10-16T05:10:17" }]);
    if (path.endsWith("/scoring/factorsets") || path.endsWith("/scoring/tasks")) return respond([]);
    if (path.endsWith("/scoring/pipeline/eta")) return respond({ recommended_seconds: 1200, sample_count: 0 });
    if (path.endsWith(`/scoring/models/${id}/relations`)) return respond({ model_id: id, factor_set_id: "fs-rank", n_members: 1, factors: [{ factor_code: "turnover_z20", factor_version_id: 100073 }], unbound_reason: null });
    if (path.endsWith(`/factor-models/${id}`)) return respond({ id, status: "validated", production_eligible: true, hyperparameters: hp, data_cutoff_at: "2026-10-16T05:10:17", metrics: { validation_ic: 0.02 }, sample_count: 1000, weights: [], audit: [] });
    throw new Error(`Unexpected HTTP request: ${path}`);
  }));
  const settings = render(<FactorModelSettings />);
  fireEvent.click(await screen.findByRole("button", { name: /第 3 步：验证与启用/ }));
  fireEvent.click(await screen.findByTestId(`view-relations-${id}`));
  const drawer = await screen.findByTestId("relations-drawer");
  await within(drawer).findByText("turnover_z20");
  expect(within(drawer).getByText(locale === "zh-CN" ? "逐日横截面居中秩" : "Centered daily cross-sectional rank")).toBeVisible();
  expect(within(drawer).getByText(locale === "zh-CN" ? "每个交易日总权重相同" : "Equal total weight per trading date")).toBeVisible();
  expect(within(drawer).getByText(warning)).toBeVisible();
  expect(within(drawer).getByText(qualityWarning)).toBeVisible();
  expect(within(drawer).queryByText(hp.research_data_quality_limitations)).not.toBeInTheDocument();
  fireEvent.click(within(drawer).getByRole("button", { name: /确认正式启用/ }));
  const settingsReview = await screen.findByRole("dialog", { name: locale === "zh-CN" ? "确认启用模型" : "Confirm model activation" });
  await waitFor(() => expect(within(settingsReview).getByText(warning)).toBeVisible());
  expect(within(settingsReview).getByText(qualityWarning)).toBeVisible();
  expect(within(settingsReview).queryByText(hp.research_data_quality_limitations)).not.toBeInTheDocument();
  expect(writes).toHaveLength(0);
  settings.unmount();

  render(<App><FactorModelPage /></App>);
  fireEvent.click(await screen.findByRole("tab", { name: locale === "zh-CN" ? "2. 验证与启用模型" : "2. Review and enable models" }));
  const table = await screen.findByTestId("model-table-wrapper");
  fireEvent.click(within(table).getByText(id));
  const detail = await screen.findByTestId("model-validation-detail");
  expect(within(detail).getByText(warning)).toBeVisible();
  expect(within(detail).getByText(qualityWarning)).toBeVisible();
  expect(within(detail).queryByText(hp.research_data_quality_limitations)).not.toBeInTheDocument();
  expect(within(detail).getByText(locale === "zh-CN" ? "逐日横截面居中秩" : "Centered daily cross-sectional rank")).toBeVisible();
  fireEvent.click(within(table).getByRole("button", { name: locale === "zh-CN" ? /正式启用/ : /Activate Live/ }));
  const modelReview = await screen.findByRole("dialog", { name: locale === "zh-CN" ? /激活模型/ : /Activate Model/ });
  await waitFor(() => expect(within(modelReview).getByText(warning)).toBeVisible());
  expect(within(modelReview).getByText(qualityWarning)).toBeVisible();
  expect(within(modelReview).queryByText(hp.research_data_quality_limitations)).not.toBeInTheDocument();
  expect(writes).toHaveLength(0);
});

it.each(["settings", "factor-page"])("direct activation reads exact model research evidence, retries failures, and rejects stale responses (%s)", async (entry) => {
  const a = "direct-rank-A", b = "direct-plain-B";
  const pending = new Map<string, Array<(value: Response) => void>>();
  const writes: string[] = [];
  const respond = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  const brief = (id: string) => ({ id, name: id, status: "validated", production_eligible: true, training_mode: "warehouse", readiness_code: "READY", validation_ic: 0.02, factorset_id: "fs-direct", sample_count: 1000, data_cutoff_at: "2026-10-16T05:10:17" });
  const detail = (id: string, research: boolean) => ({ id, hyperparameters: { factor_set_id: "fs-direct", ...(research ? { research_evaluation_status: "previously_observed_development_comparison" } : {}) }, status: "validated", production_eligible: true, metrics: {}, weights: [] });
  vi.stubGlobal("fetch", vi.fn(async (input: string | Request, init?: RequestInit) => {
    const path = (typeof input === "string" ? input : input.url).split("?")[0];
    if (init?.method && init.method !== "GET") { writes.push(path); throw new Error("Unexpected write"); }
    if (path.endsWith("/scoring/overview")) return respond({ feature_enabled: true, warehouse_available: true, health_status: "healthy", weight_mode: "manual", runtime_active_model_id: null, runtime_version: 11, latest_trade_date: "2026-10-14", factor_coverage: [] });
    if (path.endsWith("/scoring/models")) return respond([brief(a), brief(b)]);
    if (path.endsWith("/scoring/factorsets") || path.endsWith("/scoring/tasks")) return respond([]);
    if (path.endsWith("/scoring/pipeline/eta")) return respond({ recommended_seconds: 1200, sample_count: 0 });
    const id = [a, b].find((candidate) => path.endsWith(`/factor-models/${candidate}`));
    if (id) return new Promise<Response>((resolve) => pending.set(id, [...(pending.get(id) || []), resolve]));
    throw new Error(`Unexpected HTTP request: ${path}`);
  }));
  if (entry === "settings") {
    render(<FactorModelSettings />);
    fireEvent.click(await screen.findByRole("button", { name: /第 3 步：验证与启用/ }));
  } else {
    render(<App><FactorModelPage /></App>);
    fireEvent.click(await screen.findByRole("tab", { name: "2. 验证与启用模型" }));
  }
  const open = async (id: string) => {
    const row = (await screen.findByText(id)).closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: /正式启用/ }));
    return screen.findByRole("dialog", { name: entry === "settings" ? /确认启用模型/ : /激活模型/ });
  };
  const confirm = (dialog: HTMLElement) => within(dialog).getByRole("button", { name: entry === "settings" ? "确认启用并生成评分" : /确\s*认$/ });
  const resolve = async (id: string, index: number, response: Response) => {
    await waitFor(() => expect(pending.get(id)?.[index]).toBeDefined());
    await act(async () => pending.get(id)![index](response));
  };
  let dialog = await open(a);
  expect(confirm(dialog)).toBeDisabled();
  await resolve(a, 0, respond({ detail: "Temporary model read failure" }, 503));
  let retry = await within(dialog).findByRole("button", { name: /重新读取模型/ });
  expect(confirm(dialog)).toBeDisabled();
  fireEvent.click(retry);
  await resolve(a, 1, respond(detail("wrong-model", true)));
  retry = await within(dialog).findByRole("button", { name: /重新读取模型/ });
  expect(confirm(dialog)).toBeDisabled();
  expect(within(dialog).queryByTestId("model-research-warning")).not.toBeInTheDocument();
  fireEvent.click(retry);
  await waitFor(() => expect(pending.get(a)).toHaveLength(3));
  fireEvent.click(within(dialog).getByRole("button", { name: /取\s*消/ }));
  dialog = await open(b);
  await resolve(a, 2, respond(detail(a, true)));
  expect(confirm(dialog)).toBeDisabled();
  expect(within(dialog).queryByTestId("model-research-warning")).not.toBeInTheDocument();
  await resolve(b, 0, respond(detail(b, false)));
  await waitFor(() => expect(confirm(dialog)).toBeEnabled());
  expect(within(dialog).queryByTestId("model-research-warning")).not.toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: /取\s*消/ }));
  dialog = await open(a);
  expect(confirm(dialog)).toBeDisabled();
  await resolve(a, 3, respond(detail(a, true)));
  await within(dialog).findByTestId("model-research-warning");
  await waitFor(() => expect(confirm(dialog)).toBeEnabled());
  expect(writes).toHaveLength(0);
});
