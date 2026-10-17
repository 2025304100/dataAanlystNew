import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { message } from "antd";
import { AppProvider, useApp } from "../AppContext";
import TodayDecision from "../../components/TodayDecision";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
let workbench: ReturnType<typeof deferred<Response>>;
let workbenchStarted: boolean;

function Page({ home = false }: { home?: boolean }) {
  const ctx = useApp();
  return <>
    <button onClick={() => ctx.setActiveTab("settings")}>Open settings</button>
    <button onClick={() => ctx.setActiveTab("decision")}>Open decision</button>
    <button onClick={() => ctx.showToast("error", "Settings save failed")}>Fail settings save</button>
    <output>{ctx.activeTab}</output>
    <output>{ctx.workbench?.portfolio?.name ?? "No workbench"}</output>
    <output aria-label="Workbench error">{ctx.workbenchError ?? "No error"}</output>
    <output aria-label="Primary members">{ctx.primaryWatchlistSymbolIds.size}</output>
    <output aria-label="Cached watchlists">{Object.keys(ctx.watchlistItems).length}</output>
    <output aria-label="News snapshot">{ctx.newsSnapshot ? "Loaded news" : "No news"}</output>
    {home && ctx.activeTab === "decision" && <TodayDecision />}
  </>;
}

beforeEach(() => {
  workbench = deferred<Response>();
  workbenchStarted = false;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/dashboard/workbench?")) { workbenchStarted = true; return workbench.promise; }
    if (url === "/api/v1/portfolios") return json([{ id: 1, name: "Portfolio", is_default: 1 }]);
    if (url.includes("/system/capabilities")) return json({ capabilities: {} });
    if (url.includes("/system/data-health") || url.includes("/macro/overview") || url.includes("/scoring/overview") || url.includes("/status")) return json(null);
    if (url.includes("/signal-rule") && !url.includes("/presets")) return json({});
    if (url.includes("/discovery/scopes/")) return json({});
    return json([]);
  }));
});
afterEach(async () => {
  await act(async () => { workbench.resolve(json({ portfolio: { name: "Portfolio" }, candidates: [], latest_scores: [], watchlists: [] })); });
  cleanup(); await act(async () => { message.destroy(); }); vi.unstubAllGlobals();
});

it("does not show a late home workbench timeout on settings and still shows settings failures", async () => {
  render(<AppProvider><Page /></AppProvider>);
  await waitFor(() => expect(workbenchStarted).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "Open settings" }));
  await act(async () => { workbench.reject(new Error("Home request timed out")); });
  expect(screen.getByText("settings", { selector: "output" })).toBeInTheDocument();
  expect(screen.queryByText("Home request timed out")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Fail settings save" }));
  expect(await screen.findByText("Settings save failed")).toBeInTheDocument();
});

it("retains a current decision workbench failure in its error state and toast", async () => {
  render(<AppProvider><Page /></AppProvider>);
  await waitFor(() => expect(workbenchStarted).toBe(true));
  await act(async () => { workbench.reject(new Error("Current workbench failed")); });
  expect(screen.getByLabelText("Workbench error")).toHaveTextContent("Current workbench failed");
  expect(screen.getByText("Current workbench failed", { selector: "span" })).toBeInTheDocument();
});

it("does not replace the workbench with a success from before leaving and returning", async () => {
  render(<AppProvider><Page /></AppProvider>);
  await waitFor(() => expect(workbenchStarted).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "Open settings" }));
  fireEvent.click(screen.getByRole("button", { name: "Open decision" }));
  await act(async () => { workbench.resolve(json({ portfolio: { name: "Obsolete portfolio" }, candidates: [], latest_scores: [], watchlists: [] })); });
  expect(screen.getByText("No workbench")).toBeInTheDocument();
  expect(screen.queryByText("Obsolete portfolio")).not.toBeInTheDocument();
});

it("does not launch a home refresh after its page has been left while summary data was loading", async () => {
  const macro = deferred<Response>();
  const originalFetch = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, options?: RequestInit) => {
    if (String(input).includes("/macro/overview")) return macro.promise;
    return originalFetch(input, options);
  }));
  render(<AppProvider><Page home /></AppProvider>);
  await waitFor(() => expect(workbenchStarted).toBe(true));
  await act(async () => { workbench.resolve(json({ portfolio: { name: "Current portfolio" }, candidates: [], latest_scores: [], watchlists: [] })); });
  await screen.findByText("Current portfolio", { selector: "output" });
  fireEvent.click(screen.getByRole("button", { name: "Open settings" }));
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    if (String(input).includes("/dashboard/workbench?")) throw new Error("Late home refresh failed");
    return originalFetch(input, options);
  }));
  await act(async () => { macro.resolve(json(null)); });
  expect(screen.queryByText("Late home refresh failed")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Workbench error")).toHaveTextContent("No error");
});

it("automatically loads a current workbench when returning to decision after the initial request was abandoned", async () => {
  const originalFetch = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    if (String(input).includes("/dashboard/workbench?")) {
      if (!workbenchStarted) { workbenchStarted = true; return workbench.promise; }
      return json({ portfolio: { name: "Reloaded portfolio" }, candidates: [], latest_scores: [], watchlists: [] });
    }
    return originalFetch(input, options);
  }));
  render(<AppProvider><Page home /></AppProvider>);
  await waitFor(() => expect(workbenchStarted).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "Open settings" }));
  await act(async () => { workbench.reject(new Error("Abandoned initial workbench")); });
  fireEvent.click(screen.getByRole("button", { name: "Open decision" }));
  expect(await screen.findByText("Reloaded portfolio", { selector: "output" })).toBeInTheDocument();
  expect(screen.queryByText("Abandoned initial workbench")).not.toBeInTheDocument();
});

it("does not commit late membership or watchlist cache updates after leaving the workbench page", async () => {
  const members = deferred<Response>();
  let membersStarted = false;
  const originalFetch = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    if (String(input).includes("/watchlists/7/items")) { membersStarted = true; return members.promise; }
    return originalFetch(input, options);
  }));
  render(<AppProvider><Page /></AppProvider>);
  await waitFor(() => expect(workbenchStarted).toBe(true));
  await act(async () => { workbench.resolve(json({ portfolio: { name: "Portfolio" }, candidates: [], latest_scores: [], watchlists: [{ id: 7, list_type: "watch" }] })); });
  await waitFor(() => expect(membersStarted).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "Open settings" }));
  await act(async () => { members.resolve(json([{ symbol_id: 42 }])); });
  expect(screen.getByLabelText("Primary members")).toHaveTextContent("0");
  expect(screen.getByLabelText("Cached watchlists")).toHaveTextContent("0");
});

it("does not commit a late news snapshot after leaving the workbench page", async () => {
  const news = deferred<Response>();
  let newsStarted = false;
  const originalFetch = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    if (String(input).includes("/news/latest")) { newsStarted = true; return news.promise; }
    return originalFetch(input, options);
  }));
  render(<AppProvider><Page /></AppProvider>);
  await waitFor(() => expect(workbenchStarted).toBe(true));
  await act(async () => { workbench.resolve(json({ portfolio: { name: "Portfolio" }, candidates: [{ symbol_id: 42 }], latest_scores: [], watchlists: [] })); });
  await waitFor(() => expect(newsStarted).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "Open settings" }));
  await act(async () => { news.resolve(json({ symbols_total: 1, macro: { summary: "Old news" } })); });
  expect(screen.getByLabelText("News snapshot")).toHaveTextContent("No news");
});
