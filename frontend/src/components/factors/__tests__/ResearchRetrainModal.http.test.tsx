import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ResearchRetrainModal from "../ResearchRetrainModal";
import FactorCoverageSummary from "../FactorCoverageSummary";
import { api, _scoringOverviewToFactorOverview } from "../../../api/client";
import { setLocale } from "../../../i18n";
import { readPendingResearch, savePendingResearch } from "../../../utils/researchRequest";

const plan = (id = "model-original") => ({ model_id: id, factor_set_id: "frozen-original",
  dataset_id: "trusted-original", manifest_sha256: "a".repeat(64), start_date: "2025-01-13", end_date: "2026-10-14",
  symbol_count: 128, bar_count: 53911, trade_date_count: 422, window_days: 250, validation_days: 50,
  research_only: true, independent_validation: false, materialize_scores: false, auto_activate: false });
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

beforeEach(() => { setLocale("zh-CN"); localStorage.clear(); });
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); setLocale("zh-CN"); });

it.each(["zh-CN", "en-US"] as const)("reviews exact immutable inputs before explicit research-only POST (%s)", async locale => {
  setLocale(locale);
  const writes: { path: string; body: any }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    expect(path).toContain("/scoring/models/model-original/research-retrain");
    if (init?.method === "POST") { writes.push({ path, body: JSON.parse(String(init.body)) }); return response({ id: "research-task", status: "queued" }); }
    return response(plan());
  }));
  const onSubmitted = vi.fn();
  render(<ResearchRetrainModal modelId="model-original" onClose={vi.fn()} onSubmitted={onSubmitted} />);
  const submit = screen.getByRole("button", { name: locale === "zh-CN" ? "提交研究重训" : "Submit research retraining" });
  await waitFor(() => expect(submit).toBeEnabled());
  await waitFor(() => expect(screen.getByText("trusted-original")).toBeVisible());
  expect(screen.getByText("a".repeat(64))).toBeVisible();
  expect(writes).toEqual([]);
  fireEvent.click(submit); fireEvent.click(submit);
  await waitFor(() => expect(onSubmitted).toHaveBeenCalledWith({ id: "research-task", status: "queued" }));
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toEqual({ manifest_sha256: "a".repeat(64), note: "", request_id: expect.stringMatching(/^[a-f0-9]{32}$/) });
});

it.each(["wrong_model", "production", "failed", "invalid_counts"])("blocks unsafe or failed preflight without fallback writes (%s)", async scenario => {
  const writes = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async (_input, init) => {
    if (init?.method === "POST") writes();
    const value = { ...plan(), ...(scenario === "wrong_model" ? { model_id: "other" } : {}),
      ...(scenario === "production" ? { materialize_scores: true } : {}), ...(scenario === "invalid_counts" ? { bar_count: 0 } : {}) };
    return response(scenario === "failed" ? { detail: "research_retrain_source_dataset_changed" } : value, scenario === "failed" ? 409 : 200);
  }));
  render(<ResearchRetrainModal modelId="model-original" onClose={vi.fn()} onSubmitted={vi.fn()} />);
  await waitFor(() => expect(screen.getByText("研究输入核验未通过，请关闭后重新检查")).toBeVisible());
  expect(screen.getByRole("button", { name: "提交研究重训" })).toBeDisabled();
  expect(writes).not.toHaveBeenCalled();
});

it("ignores a prior model's late preflight response", async () => {
  let resolveOld: (value: Response) => void = () => {};
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => String(input).includes("/old/")
    ? new Promise<Response>(resolve => { resolveOld = resolve; }) : Promise.resolve(response(plan("new")))));
  const view = render(<ResearchRetrainModal modelId="old" onClose={vi.fn()} onSubmitted={vi.fn()} />);
  view.rerender(<ResearchRetrainModal modelId="new" onClose={vi.fn()} onSubmitted={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "提交研究重训" })).toBeEnabled());
  await act(async () => resolveOld(response({ ...plan("old"), dataset_id: "stale-data" })));
  expect(screen.queryByText("stale-data")).not.toBeInTheDocument();
  await waitFor(() => expect(screen.getByText("trusted-original")).toBeVisible());
});

it("renders the model-enabled mean with real factor rows, dates and denominator", async () => {
  const overview = _scoringOverviewToFactorOverview({ model_factor_coverage_count: 1, model_factor_avg_coverage: .8,
    active_factor_coverage_count: 2, active_factor_avg_coverage: .4,
    latest_trade_date: "2026-10-16", latest_factor_date: "2026-10-14", health_reasons: ["ep_ttm:stale"],
    factor_coverage: [{ factor_code: "ep_ttm", model_enabled: true, universe_symbols: 100, eligible_symbols: 80, coverage: .8, latest_trade_date: "2026-10-14" },
      { factor_code: "hot_rank_attention", model_enabled: false, universe_symbols: 100, eligible_symbols: 0, coverage: 0, latest_trade_date: "2026-10-14" }] });
  render(<FactorCoverageSummary overview={overview} />);
  expect(screen.getByText("80.0%")).toBeVisible();
  expect(overview.average_coverage).toBe(.4);
  expect(overview.health.reasons).toEqual(["ep_ttm:stale"]);
  fireEvent.click(screen.getByRole("button", { name: "查看逐因子覆盖与数据日期" }));
  const table = await screen.findByRole("table");
  await waitFor(() => expect(within(table).getByText("80 / 100")).toBeVisible());
  expect(within(table).getByText("2026-10-14")).toBeVisible();
  expect(within(table).queryByText("hot_rank_attention")).not.toBeInTheDocument();
});

it.each(["review", "submit"])("keeps bounded research verification alive beyond the generic 20-second timeout (%s)", async action => {
  vi.useFakeTimers();
  let aborted = false;
  vi.stubGlobal("fetch", vi.fn((_input, init) => new Promise<Response>((resolve, reject) => {
    const timer = setTimeout(() => resolve(response(action === "review" ? plan("slow-model") : { id: "slow-task" })), action === "review" ? 35000 : 75000);
    init?.signal?.addEventListener("abort", () => { aborted = true; clearTimeout(timer); reject(new DOMException("aborted", "AbortError")); });
  })));
  const result = (action === "review" ? api.scoringGetResearchRetrainPlan("slow-model")
    : api.scoringRetrainResearchModel("slow-model", "a".repeat(64), "", "1".repeat(32))).then(value => ({ value }), error => ({ error }));
  await vi.advanceTimersByTimeAsync(action === "review" ? 35000 : 75000);
  expect(aborted).toBe(false);
  expect(await result).toHaveProperty("value");
});

it.each(["done", "failed", "cancelled"])("recovers a lost POST response without creating a second task (%s)", async status => {
  let body: any;
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") { body = JSON.parse(String(init.body)); throw new TypeError("response lost after commit"); }
    if (String(input).includes("/requests/")) return response({ model_id: "model-original", request_id: body.request_id,
      state: "found", manifest_sha256: body.manifest_sha256, note: body.note, task: { id: "original-task", status } });
    return response(plan());
  });
  vi.stubGlobal("fetch", fetcher);
  const onSubmitted = vi.fn();
  render(<ResearchRetrainModal modelId="model-original" onClose={vi.fn()} onSubmitted={onSubmitted} />);
  const button = screen.getByRole("button", { name: "提交研究重训" });
  await waitFor(() => expect(button).toBeEnabled()); fireEvent.click(button);
  await waitFor(() => expect(screen.getByText("已找到原提交任务")).toBeVisible());
  expect(onSubmitted).not.toHaveBeenCalled();
  expect(readPendingResearch("model-original")?.requestId).toBe(body.request_id);
  fireEvent.click(screen.getByRole("button", { name: "查看原任务" }));
  expect(onSubmitted).toHaveBeenCalledWith({ id: "original-task", status });
  expect(readPendingResearch("model-original")).toBeNull();
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it.each(["not_found", "offline"])("retains a durable receipt across remount and retries only the same explicit request (%s)", async recovery => {
  const posts: any[] = [];
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") { posts.push(JSON.parse(String(init.body))); throw new TypeError("offline"); }
    if (String(input).includes("/requests/")) {
      if (recovery === "offline") throw new TypeError("offline");
      return response({ model_id: "model-original", request_id: posts[0].request_id, state: "not_found", task: null });
    }
    return response(plan());
  });
  vi.stubGlobal("fetch", fetcher);
  const props = { modelId: "model-original", onClose: vi.fn(), onSubmitted: vi.fn() };
  const view = render(<ResearchRetrainModal {...props} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "提交研究重训" })).toBeEnabled());
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "frozen intent" } });
  fireEvent.click(screen.getByRole("button", { name: "提交研究重训" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "按同一请求重试" })).toBeEnabled());
  view.unmount();
  const planGets = fetcher.mock.calls.filter(([url, init]) => !String(url).includes("/requests/") && init?.method !== "POST").length;
  render(<ResearchRetrainModal {...props} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "按同一请求重试" })).toBeEnabled());
  expect(posts).toHaveLength(1);
  expect(fetcher.mock.calls.filter(([url, init]) => !String(url).includes("/requests/") && init?.method !== "POST")).toHaveLength(planGets);
  fireEvent.click(screen.getByRole("button", { name: "按同一请求重试" }));
  await waitFor(() => expect(posts).toHaveLength(2));
  expect(posts[1]).toEqual(posts[0]);
  expect(posts[1].note).toBe("frozen intent");
  expect(props.onSubmitted).not.toHaveBeenCalled();
});

it.each(["model_id", "request_id", "manifest_sha256", "note"])("does not acknowledge a recovery response with mismatched %s", async field => {
  savePendingResearch({ version: 1, modelId: "model-original", requestId: "1".repeat(32), manifestSha256: "a".repeat(64), note: "" });
  const fetcher = vi.fn(async () => response({ model_id: "model-original", request_id: "1".repeat(32),
    manifest_sha256: "a".repeat(64), note: "", state: "found", task: { id: "unexpected-task" }, [field]: "wrong" }));
  vi.stubGlobal("fetch", fetcher);
  const onSubmitted = vi.fn();
  render(<ResearchRetrainModal modelId="model-original" onClose={vi.fn()} onSubmitted={onSubmitted} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "按同一请求重试" })).toBeEnabled());
  expect(screen.queryByRole("button", { name: "查看原任务" })).not.toBeInTheDocument();
  expect(onSubmitted).not.toHaveBeenCalled(); expect(fetcher).toHaveBeenCalledTimes(1);
});

it("blocks a new POST when the durable receipt is corrupt", async () => {
  localStorage.setItem("factor-research:pending:v1:model-original", "{broken");
  const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
  render(<ResearchRetrainModal modelId="model-original" onClose={vi.fn()} onSubmitted={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "提交研究重训" })).toBeDisabled());
  await waitFor(() => expect(screen.getByText(/无法读取或保存提交凭证/)).toBeVisible()); expect(fetcher).not.toHaveBeenCalled();
});

it("blocks submission if the browser cannot persist the request before POST", async () => {
  const writes = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async (_input, init) => { if (init?.method === "POST") writes(); return response(plan()); }));
  render(<ResearchRetrainModal modelId="model-original" onClose={vi.fn()} onSubmitted={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "提交研究重训" })).toBeEnabled());
  const denied = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("quota", "QuotaExceededError"); });
  try {
    fireEvent.click(screen.getByRole("button", { name: "提交研究重训" }));
    await waitFor(() => expect(screen.getByText(/无法读取或保存提交凭证/)).toBeVisible());
    expect(writes).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "提交研究重训" })).toBeDisabled();
  } finally { denied.mockRestore(); }
});
