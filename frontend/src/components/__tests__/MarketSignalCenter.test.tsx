import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const { signalApi } = vi.hoisted(() => ({ signalApi: {
  metadata: vi.fn(), settings: vi.fn(), saveSettings: vi.fn(), rules: vi.fn(), createRule: vi.fn(), updateRule: vi.fn(),
  dashboard: vi.fn(), evidence: vi.fn(), addEvidence: vi.fn(), assessments: vi.fn(), predictions: vi.fn(), startJob: vi.fn(), job: vi.fn(),
} }));
vi.mock("../../api/marketSignals", () => ({ signalApi }));
vi.mock("../../context/AppContext", () => ({ useApp: () => ({ locale: "zh-CN" }) }));
import MarketSignalCenter from "../MarketSignalCenter";

let rules: any[];
const stats = { total: 0, matured: 0, hits: 0, misses: 0, pending: 0, unverifiable: 0,
  precision: null, event_clusters: 0, validation_status: "not_verified", historical_baseline_rate: null };
beforeEach(() => {
  vi.resetAllMocks();
  rules = [];
  signalApi.metadata.mockResolvedValue({ symbols: [{ key: "000001", name: "验收银行", asset_type: "stock" }],
    sectors: [{ key: "semiconductor", name: "semiconductor" }], indices: ["000300"], ai_profiles: [], channels: [], readiness: [], schedules: [] });
  signalApi.settings.mockResolvedValue({ enabled: false, ai_profile_id: null, global_daily_limit: 3, max_analysis_per_run: 10, auto_analyze: true });
  signalApi.rules.mockImplementation(async () => [...rules]);
  signalApi.dashboard.mockResolvedValue({ live: stats, shadow: stats, rules: [], target_precision: 0.75, queued: 0, suppressed: 0 });
  for (const name of ["evidence", "assessments", "predictions"] as const) signalApi[name].mockResolvedValue({ total: 0, items: [] });
  signalApi.createRule.mockImplementation(async (value) => { const rule = { ...value, id: 1, revision: 1, target_name: "验收银行" }; rules.push(rule); return rule; });
  signalApi.updateRule.mockImplementation(async (id, value) => { rules = rules.map(row => row.id === id ? { ...row, ...value, revision: row.revision+1 } : row); return rules[0]; });
});
afterEach(cleanup);

it("keeps an existing selected stock editable outside the first search page", async () => {
  rules = [{ id:1, revision:1, name:"远端搜索股票", target_type:"symbol", target_key:"999999", target_name:"已关注标的",
    quote_key:"999999", quote_type:"symbol", min_score:90, window_days:1, min_move_pct:3, adaptive_threshold:false,
    volatility_percentile:80, max_age_hours:24, cooldown_hours:6, max_daily:1, delivery_mode:"instant", digest_time:"16:10",
    mode:"shadow", enabled:true, channel_ids:[] }];
  render(<MarketSignalCenter open onClose={() => {}} />);
  await waitFor(() => expect(screen.getByRole("tab", {name:"提醒与验证"})).toHaveAttribute("aria-selected", "true"));
  fireEvent.click(screen.getByRole("tab", { name:"关注设置" }));
  fireEvent.click(screen.getByRole("button", { name:"编辑 远端搜索股票" }));
  const stock = screen.getByLabelText("关注股票") as HTMLSelectElement;
  expect(stock.value).toBe("999999");
  expect(stock.checkValidity()).toBe(true);
  fireEvent.click(screen.getByRole("button", { name:"保存关注" }));
  await waitFor(() => expect(signalApi.updateRule).toHaveBeenCalledWith(1, expect.objectContaining({ target_key:"999999", min_score:90 })));
});

it("lets a user save an independent stock threshold/window and pause it", async () => {
  render(<MarketSignalCenter open onClose={() => {}} />);
  await screen.findByText("暂无验证样本");
  fireEvent.click(screen.getByRole("button", { name: "新增关注" }));
  fireEvent.change(screen.getByLabelText("规则名称"), { target: { value: "银行观察" } });
  fireEvent.change(screen.getByLabelText("关注股票"), { target: { value: "000001" } });
  fireEvent.change(screen.getByLabelText("最低评分"), { target: { value: "90" } });
  fireEvent.change(screen.getByLabelText("观察窗口"), { target: { value: "1" } });
  fireEvent.click(screen.getByRole("button", { name: "保存关注" }));
  await screen.findByRole("cell", { name: /^银行观察/ });
  expect(signalApi.createRule).toHaveBeenCalledWith(expect.objectContaining({ target_key: "000001", min_score: 90, window_days: 1 }));
  fireEvent.click(screen.getByRole("button", { name: "暂停 银行观察" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "启用 银行观察" })).toBeInTheDocument());
  expect(screen.queryByText("已达标75%")).not.toBeInTheDocument();
});

it("accepts dated original evidence and runs analysis as a visible background job", async () => {
  const article = { id: 11, title: "验收银行项目正式获批", text: "项目投入100亿元，实施条件已经明确。", source: "manual",
    source_url: "https://example.com/policy", published_at: "2026-10-27T08:00:00", time_quality: "manual", kind: "news", metadata: {} };
  let items: any[] = [];
  signalApi.addEvidence.mockImplementation(async () => { items = [article]; return article; });
  signalApi.evidence.mockImplementation(async () => ({ total: items.length, items }));
  signalApi.startJob.mockResolvedValue({ id: "job-1", status: "queued", percent: 0 });
  signalApi.job.mockResolvedValue({ id: "job-1", status: "done", percent: 100, message: "异动任务完成" });
  render(<MarketSignalCenter open onClose={() => {}} />);
  await screen.findByText("暂无验证样本");
  fireEvent.click(screen.getByRole("tab", { name: "消息与分析" }));
  fireEvent.click(screen.getByRole("button", { name: "补充原文证据" }));
  fireEvent.change(screen.getByLabelText("消息标题"), { target: { value: article.title } });
  fireEvent.change(screen.getByLabelText("原文内容"), { target: { value: article.text } });
  fireEvent.change(screen.getByLabelText("来源链接"), { target: { value: article.source_url } });
  fireEvent.change(screen.getByLabelText("发布时间（北京时间）"), { target: { value: "2026-10-27T16:00" } });
  fireEvent.click(screen.getByRole("button", { name: "保存证据" }));
  await screen.findByText(article.title);
  expect(signalApi.addEvidence).toHaveBeenCalledWith(expect.objectContaining({ published_at: "2026-10-27T08:00:00.000Z" }));
  fireEvent.click(screen.getByRole("button", { name: `分析 ${article.title}` }));
  await waitFor(() => expect(signalApi.startJob).toHaveBeenCalledWith(expect.objectContaining({ kind: "analyze", evidence_ids: [11] })));
  await screen.findByText("异动任务完成", {}, { timeout: 5000 });
});
