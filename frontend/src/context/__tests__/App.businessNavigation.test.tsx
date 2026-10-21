import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntdApp, message } from "antd";
import App from "../../App";
// Load the real lazy pages during collection so module transformation does not consume interaction timeouts.
import "../../components/OpportunityCenter";
import "../../components/InvestmentCenter";
import "../../components/symbol-research/SymbolResearchShell";
import "../../components/portfolio-trading/PortfolioTradingShell";
import "../../components/Settings";
import { AppProvider } from "../AppContext";
import { setLocale } from "../../i18n";

const portfolio = { id: 1, name: "本地测试组合", is_default: 1, account_type: "simulated", asset_scope: "mixed", currency: "CNY", total_capital: 100000, cash: 100000, auto_trade_enabled: 0 };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); setLocale("zh-CN");
  window.history.replaceState(null, "", "/");
  // Only the HTTP boundary is replaced. The real app, context, source page and destination page render together.
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/portfolios") return json([portfolio]);
    if (url.includes("/dashboard/workbench?")) return json({ portfolio, candidates: [], latest_scores: [], positions: [], actions: [], watchlists: [], active_rule: null });
    if (url.includes("/system/capabilities")) return json({ capabilities: [] });
    if (url.includes("/scoring/overview")) return json({ feature_enabled: false, config: {}, runtime: { weight_mode: "manual", version: 1 }, health: { warehouse_available: false } });
    if (url.includes("/scoring/tasks?")) return json({ items: [], total: 0 });
    if (url.includes("/system/data-health") || url.includes("/macro/overview")) return json(null);
    if (url.includes("/market-events")) return json({ events: [] });
    if (url.endsWith("/status")) return json({ portfolio_id: 1, current_state: "READY", is_auto_simulation_eligible: false, permissions: { allow_new_buys: false, allow_risk_exits: false, allow_auto_recovery: false, requires_manual_ack: false } });
    if (url.includes("/action-items")) return json({ portfolio_id: 1, summary: { ready: false, blockers: 0 }, checks: {}, items: [] });
    if (url.includes("/signal-rule") && !url.includes("/presets")) return json({});
    if (url.includes("/factors/symbols/") && url.includes("/explanation")) return json(null);
    if (url.includes("/symbols/") && url.includes("/relationships")) return json({ symbol_id: 11, candidate: { has_candidate: false }, observation: { has_observation: false }, portfolio_member: { has_portfolio_membership: false }, position: { has_position: false }, alert: { has_active_alert: false }, degraded: false });
    if (url.includes("/dashboard/symbol-detail")) return json({ symbol: { id: 11, symbol: "600000", name: "测试股票", asset_type: "stock", market: "sh", region: "cn" }, bars: [], position: null, latest_score: null, latest_trade_setup: { id: 1, entry_min: 10, entry_max: 10, stop_loss: 9, target_price: 12, recommended_position_pct: 0.1, recommended_position_amount: 10000, tranche_plan: [], return_scenarios: { reference_price: 10 } }, trade_setups: [], future_buy_plans: [] });
    return json([]);
  }));
});

afterEach(async () => {
  cleanup(); await act(async () => { message.destroy(); }); vi.unstubAllGlobals();
});

it("opens the actual opportunity page from the home opportunity action", async () => {
  render(<AppProvider><App /></AppProvider>);
  const links = await screen.findAllByRole("button", { name: "去发现" });
  fireEvent.click(links[0]);
  expect(await screen.findByRole("heading", { name: "机会中心" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "候选池" })).toBeVisible();
});

it("opens the actual portfolio risk settings from symbol research", async () => {
  window.history.replaceState(null, "", "/?tab=investment&symbol_id=11");
  render(<AppProvider><App /></AppProvider>);
  const configureRisk = await screen.findByRole("button", { name: "前往组合风控配置" }, { timeout: 5000 });
  await waitFor(() => expect(configureRisk).toBeEnabled());
  fireEvent.click(configureRisk);
  expect(await screen.findByRole("heading", { name: "条件模型与风控" }, { timeout: 10000 })).toBeVisible();
  expect(new URL(window.location.href).searchParams.get("subtab")).toBe("strategy");
});

it("opens distinct training and model management pages from their named settings entries", async () => {
  localStorage.setItem("settings_factor_center_subtab", "model");
  render(<AntdApp><AppProvider><App /></AppProvider></AntdApp>);
  fireEvent.click(await screen.findByRole("button", { name: "设置" }));
  fireEvent.click(await screen.findByRole("button", { name: /训练流程/ }));
  await waitFor(() => expect(screen.getByRole("heading", { name: "训练流程" })).toBeVisible());
  expect(screen.getByRole("button", { name: "第 1 步：准备输入" })).toBeVisible();
  expect(localStorage.getItem("settings_active_section")).toBe("factor-model");
  fireEvent.click(screen.getByRole("button", { name: /因子与模型/ }));
  await waitFor(() => expect(screen.getByRole("heading", { name: "因子与模型" })).toBeVisible());
  expect(await screen.findByRole("button", { name: "ETF训练" })).toBeVisible();
  expect(localStorage.getItem("settings_active_section")).toBe("factor-center");
  expect(vi.mocked(fetch).mock.calls.some(([, init]) => String(init?.method || "GET").toUpperCase() !== "GET")).toBe(false);
});
