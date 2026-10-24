import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntdApp, message } from "antd";
import PortfolioGovernanceTab from "../PortfolioGovernanceTab";
import { AppProvider } from "../../../context/AppContext";
import { setLocale, t } from "../../../i18n";

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const writes: string[] = [];
let capability: unknown;
let reportOptions:Array<{replay_id:string;start_date:string;end_date:string;verdict:string}>;
let report: () => Response | Promise<Response>;
beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); setLocale("zh-CN"); writes.length = 0;
  capability = { real_runner_available: false, message: "当前还不能实际运行新旧方案对比，暂不能据此切换方案。" };
  reportOptions=[];
  report = () => json({});
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input), method = init?.method || "GET";
    if (method !== "GET") writes.push(url);
    if (/^\/api\/v1\/portfolios\/\d+\/g5-dual-run\/capabilities$/.test(url)) return typeof capability === "function" ? capability() : json(capability);
    if (url.endsWith("/g5-dual-run/reports")) return json({ portfolio_id:1, reports:reportOptions });
    if (url.includes("/inspection/repairs?")) return json({ portfolio_id:1, items:[], next_cursor:null });
    if (url.endsWith("/g5-dual-run") && method === "POST") return json({ replay_id: "isolated-report", status: "COMPLETED" });
    if (url.endsWith("/g5-dual-run/isolated-report")) return report();
    if (url === "/api/v1/portfolios") return json([{ id: 1, name: "隔离测试组合", is_default: 1, currency: "CNY" }]);
    if (url.includes("/dashboard/workbench?")) return json({ portfolio: { id: 1, name: "隔离测试组合" }, candidates: [], latest_scores: [], positions: [], actions: [], watchlists: [], active_rule: null });
    if (url.includes("/auto-trade/action-items")) return json({ items: [], summary: { blockers: 0, warnings: 0, info: 0 }, checks: null });
    if (url.includes("/status")) return json({ current_state: "READY", allowed_transitions: [] });
    return json([]);
  }));
});
afterEach(async () => { cleanup(); await act(async () => { message.destroy(); }); vi.unstubAllGlobals(); });

function openPage() {
  render(<AntdApp><AppProvider><PortfolioGovernanceTab portfolioId={1} /></AppProvider></AntdApp>);
  fireEvent.click(screen.getByText("引擎专项验收 · 切换引擎前使用"));
  fireEvent.click(screen.getByRole("button", { name: "双跑验收" }));
}

it("shows the missing real execution capability before the user fills a launch form", async () => {
  openPage();
  expect(await screen.findByText("当前还不能实际运行新旧方案对比，暂不能据此切换方案。" )).toBeVisible();
  const launch = screen.getByRole("button", { name: t("g5.launch") });
  expect(launch).toBeDisabled(); fireEvent.click(launch);
  expect(writes).toEqual([]);
});

function verifiedReply() {
  const actions = ["BUY", "SELL", "HOLD", "NO_ACTION", "REJECTED", "DATA_BLOCKED"];
  const matrix = Object.fromEntries(actions.map(a => [a, Object.fromEntries(actions.map(b => [b, Number(a === "HOLD" && b === "HOLD")]))]));
  return { portfolio_id: 1, status: "COMPLETED", start_date: "2026-08-03", end_date: "2026-08-14", total_days: 10,
    days_replayed: 10, skipped_days: [], avg_action_match_rate: 1, avg_universe_jaccard: 1, total_p0_unexplained: 0,
    total_p1_hold_noaction_flip: 0, failing_days_p0: [], failing_days_p1: [], evidence_verified: true, g5_eligible_for_g6: true,
    daily_reports: [3, 4, 5, 6, 7, 10, 11, 12, 13, 14].map(day => ({ trade_date: `2026-08-${String(day).padStart(2, "0")}`,
      p0_unexplained_count: 0, p1_hold_noaction_flip: 0, attributions: [], action_confusion_matrix: matrix })) };
}

it("previews a stored report in its own drawer and returns to the unchanged acceptance form without launching",async()=>{
  reportOptions=[{replay_id:"isolated-report",start_date:"2026-08-03",end_date:"2026-08-14",verdict:"PASS"}];
  report=()=>json(verifiedReply());openPage();
  const select=await screen.findByRole("combobox",{name:"选择历史双跑报告"});
  fireEvent.mouseDown(select);fireEvent.click(await screen.findByText("2026-08-03 至 2026-08-14 · PASS"));
  expect(await screen.findByRole("heading",{name:"双跑验收报告详情"})).toBeVisible();
  expect(await screen.findByText(t("g5.eligible"))).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"返回双跑验收"}));
  expect(screen.getByRole("combobox",{name:"选择历史双跑报告"}).closest(".ant-select")).toBeVisible();
  expect(screen.getByRole("button",{name:t("g5.launch")})).toBeDisabled();
  expect(writes).toEqual([]);
});

async function launchIsolatedReply() {
  capability = { real_runner_available: true, message: "隔离 HTTP 测试", old_chain: { id: "test-old", label: "隔离旧链", engine_version: "old" }, candidate_chains: [{ id: "test-new", label: "隔离新链", engine_version: "new" }] };
  openPage();
  const launch = screen.getByRole("button", { name: t("g5.launch") });
  const select = await screen.findByRole("combobox", { name: "候选新引擎链" });
  await waitFor(() => expect(select).not.toBeDisabled());
  fireEvent.mouseDown(select);
  fireEvent.click(await screen.findByText("隔离新链 · new"));
  await waitFor(() => expect(launch).toBeEnabled());
  fireEvent.click(launch);
}

it.each([404, 409, 503])("clears an old passing result after a %s response", async status => {
  report = () => json(verifiedReply());
  await launchIsolatedReply();
  expect(await screen.findByText(t("g5.eligible"), {}, { timeout: 6000 })).toBeVisible();
  report = () => json({ detail: { error_code: "G5_REPORT_NOT_FOUND", title_zh: "报告不存在或已清理", detail_zh: "不能据此切换方案。" } }, status);
  fireEvent.click(screen.getByRole("button", { name: t("g5.queryResult") }));
  expect(await screen.findByRole("alert")).toHaveTextContent(status === 404 ? "报告不存在或已清理" : status === 409 ? "真实执行证据未通过核对" : "暂时读不到报告");
  expect(screen.queryByText(t("g5.eligible"))).not.toBeInTheDocument();
});

it.each(["missing-proof", "wrong-portfolio", "missing-matrix", "empty-matrix", "running-pass"])("rejects a misleading successful reply: %s", async change => {
  const value: any = verifiedReply();
  if (change === "missing-proof") delete value.evidence_verified;
  if (change === "wrong-portfolio") value.portfolio_id = 2;
  if (change === "missing-matrix") delete value.daily_reports[0].action_confusion_matrix;
  if (change === "empty-matrix") for (const row of Object.values(value.daily_reports[0].action_confusion_matrix)) for (const key of Object.keys(row as object)) (row as any)[key] = 0;
  if (change === "running-pass") value.status = "RUNNING";
  report = () => json(value);
  await launchIsolatedReply();
  expect(await screen.findByRole("alert")).toHaveTextContent(/不能据此切换方案|不属于当前组合/);
  expect(screen.queryByText(t("g5.eligible"))).not.toBeInTheDocument();
  expect(writes).toEqual(["/api/v1/portfolios/1/g5-dual-run"]);
});

it("keeps launch disabled when capability reading fails and retries only the read", async () => {
  capability = () => json({ detail: "临时读取失败" }, 503);
  openPage();
  expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法确认能否运行");
  expect(screen.getByRole("button", { name: t("g5.launch") })).toBeDisabled();
  capability = { real_runner_available: false, message: "当前还不能实际运行新旧方案对比，暂不能据此切换方案。" };
  fireEvent.click(screen.getByRole("button", { name: "重新读取" }));
  expect(await screen.findByText("当前还不能实际运行新旧方案对比，暂不能据此切换方案。" )).toBeVisible();
  expect(writes).toEqual([]);
});

it("stops reading a report after a terminal not-found response", async () => {
  let reads = 0;
  report = () => { reads += 1; return json({ detail: "报告不存在" }, 404); };
  await launchIsolatedReply();
  expect(await screen.findByRole("alert")).toHaveTextContent("报告不存在或已清理");
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 5200)); });
  expect(reads).toBe(1);
});
