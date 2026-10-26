import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { api, showToast } = vi.hoisted(() => ({
  api: {
    getMarketEvents: vi.fn(), getMarketEventCollectRuns: vi.fn(), getMarketEventHotspots: vi.fn(),
    collectMarketEvents: vi.fn(), updateMarketEvent: vi.fn(),
  },
  showToast: vi.fn(),
}));
vi.mock("../../api/client", () => ({ api }));
vi.mock("../../context/AppContext", () => ({ useApp: () => ({ locale: "zh-CN", showToast }) }));
vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, values: Record<string, unknown>) => `${key}:${JSON.stringify(values)}`,
}));
// Only the separate modal is stubbed. The actual MarketNews and AntD controls
// render and perform their real filtering, empty-state and request logic.
vi.mock("../ThemeManagementModal", () => ({ default: () => null }));

import MarketNews from "../MarketNews";

const event = (title: string, id = 1) => ({
  id, title, summary: null, impact_scope: "other", importance_level: 3,
  sentiment: "positive", source: "test", published_at: new Date().toISOString(),
  expires_at: null, affected_sectors: "solar_pv",
});
const list = (title?: string) => ({ events: title ? [event(title)] : [], total: title ? 1 : 0, by_scope: {}, by_level: {} });
const hotspots = (days: number, count = 3) => ({
  days, generated_at: new Date().toISOString(),
  sectors: count ? [{ sector_id: "solar_pv", bull: count, bear: 0, neutral: 0, total: count, max_level: 3, event_ids: [1] }] : [],
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}
function countText() { return document.querySelector(".mn-sector-count")?.textContent; }
async function loaded() { await waitFor(() => expect(countText()).toBe("3market_news_count_unit")); }

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  api.getMarketEvents.mockResolvedValue(list("初始市场消息"));
  api.getMarketEventCollectRuns.mockResolvedValue([]);
  api.getMarketEventHotspots.mockImplementation(async (days: number) => hotspots(days));
  api.collectMarketEvents.mockResolvedValue({ collected: 0, skipped_duplicate: 0, errors: [], status: "done" });
  api.updateMarketEvent.mockResolvedValue({});
});
afterEach(cleanup);

describe("R06: hotspot panel stays independently operable", () => {
  it("shows hotspots while the list is empty", async () => {
    api.getMarketEvents.mockResolvedValue(list());
    render(<MarketNews />);
    await loaded();
    expect(screen.getByText("market_news_empty")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "market_news_last_365d" })).toBeInTheDocument();
  });

  it("keeps time buttons for an empty window and can reach a populated year", async () => {
    api.getMarketEvents.mockResolvedValue(list());
    api.getMarketEventHotspots.mockImplementation(async (days: number) => hotspots(days, days === 365 ? 19 : 0));
    render(<MarketNews />);
    await screen.findByText("market_news_no_bull_data");
    fireEvent.click(screen.getByRole("button", { name: "market_news_last_365d" }));
    await waitFor(() => expect(countText()).toBe("19market_news_count_unit"));
    expect(screen.getByRole("button", { name: "market_news_last_7d" })).toBeInTheDocument();
  });

  it("does not hide independent hotspots when list loading fails", async () => {
    api.getMarketEvents.mockRejectedValue(new Error("list unavailable"));
    render(<MarketNews />);
    await loaded();
    expect(screen.getByText("list unavailable")).toBeInTheDocument();
  });

  it("retains the panel after a keyword returns no list matches", async () => {
    api.getMarketEvents.mockImplementation(async ({ keyword }: { keyword?: string }) => keyword ? list() : list("正常消息"));
    render(<MarketNews />);
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: /market_news_filter$/ }));
    fireEvent.change(screen.getByPlaceholderText("market_news_keyword_placeholder"), { target: { value: "无匹配关键词" } });
    await screen.findByText("market_news_empty");
    expect(countText()).toBe("3market_news_count_unit");
  });
});

describe("R07: only the latest query may publish state", () => {
  it.each(["success", "failure"])("ignores an old hotspot %s after the new window succeeds", async (outcome) => {
    const old = deferred<ReturnType<typeof hotspots>>();
    const latest = deferred<ReturnType<typeof hotspots>>();
    api.getMarketEventHotspots.mockImplementation((days: number) => days === 7 ? old.promise : days === 365 ? latest.promise : Promise.resolve(hotspots(days)));
    render(<MarketNews />);
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: "market_news_last_7d" }));
    await waitFor(() => expect(api.getMarketEventHotspots).toHaveBeenCalledWith(7));
    fireEvent.click(screen.getByRole("button", { name: "market_news_last_365d" }));
    await waitFor(() => expect(api.getMarketEventHotspots).toHaveBeenCalledWith(365));
    // The old 30-day count must not be labelled as 365-day data while loading.
    expect(countText()).toBeUndefined();
    await act(async () => latest.resolve(hotspots(365, 99)));
    await waitFor(() => expect(countText()).toBe("99market_news_count_unit"));
    await act(async () => outcome === "success" ? old.resolve(hotspots(7, 7)) : old.reject(new Error("stale error")));
    expect(countText()).toBe("99market_news_count_unit");
    expect(screen.queryByText("market_news_hotspot_fallback_hint")).not.toBeInTheDocument();
    expect(document.querySelector(".mn-concept-panel")).toHaveAttribute("aria-busy", "false");
  });

  it("does not let an old success clear the current window's fallback", async () => {
    const old = deferred<ReturnType<typeof hotspots>>();
    api.getMarketEventHotspots.mockImplementation((days: number) => days === 7 ? old.promise : days === 365 ? Promise.reject(new Error("year offline")) : Promise.resolve(hotspots(days)));
    render(<MarketNews />);
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: "market_news_last_7d" }));
    await waitFor(() => expect(api.getMarketEventHotspots).toHaveBeenCalledWith(7));
    fireEvent.click(screen.getByRole("button", { name: "market_news_last_365d" }));
    await screen.findByText("market_news_hotspot_fallback_hint");
    await act(async () => old.resolve(hotspots(7, 88)));
    expect(screen.getByText("market_news_hotspot_fallback_hint")).toBeInTheDocument();
    expect(countText()).not.toBe("88market_news_count_unit");
  });

  it("rejects a response carrying a different days window", async () => {
    api.getMarketEventHotspots.mockResolvedValue(hotspots(7, 88));
    render(<MarketNews />);
    await screen.findByText("market_news_hotspot_fallback_hint");
    expect(countText()).not.toBe("88market_news_count_unit");
  });

  it("keeps the current loading state when an old request finishes first", async () => {
    const old = deferred<ReturnType<typeof hotspots>>();
    const latest = deferred<ReturnType<typeof hotspots>>();
    api.getMarketEventHotspots.mockImplementation((days: number) => days === 7 ? old.promise : days === 365 ? latest.promise : Promise.resolve(hotspots(days)));
    render(<MarketNews />);
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: "market_news_last_7d" }));
    await waitFor(() => expect(api.getMarketEventHotspots).toHaveBeenCalledWith(7));
    fireEvent.click(screen.getByRole("button", { name: "market_news_last_365d" }));
    await waitFor(() => expect(api.getMarketEventHotspots).toHaveBeenCalledWith(365));
    await act(async () => old.resolve(hotspots(7, 88)));
    expect(document.querySelector(".mn-concept-panel")).toHaveAttribute("aria-busy", "true");
    expect(countText()).toBeUndefined();
    await act(async () => latest.resolve(hotspots(365, 99)));
    expect(countText()).toBe("99market_news_count_unit");
    expect(document.querySelector(".mn-concept-panel")).toHaveAttribute("aria-busy", "false");
  });

  it.each(["success", "failure"])("ignores an old list %s after a newer keyword succeeds", async (outcome) => {
    const old = deferred<ReturnType<typeof list>>();
    const latest = deferred<ReturnType<typeof list>>();
    api.getMarketEvents.mockImplementation(({ keyword }: { keyword?: string }) => keyword === "A" ? old.promise : keyword === "B" ? latest.promise : Promise.resolve(list("初始市场消息")));
    render(<MarketNews />);
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: /market_news_filter$/ }));
    const input = screen.getByPlaceholderText("market_news_keyword_placeholder");
    fireEvent.change(input, { target: { value: "A" } });
    await waitFor(() => expect(api.getMarketEvents).toHaveBeenCalledWith(expect.objectContaining({ keyword: "A" })));
    fireEvent.change(input, { target: { value: "B" } });
    await waitFor(() => expect(api.getMarketEvents).toHaveBeenCalledWith(expect.objectContaining({ keyword: "B" })));
    await act(async () => latest.resolve(list("最新关键词B的消息")));
    await screen.findByText("最新关键词B的消息");
    await act(async () => outcome === "success" ? old.resolve(list("过期请求A的消息")) : old.reject(new Error("旧列表错误")));
    expect(screen.getByText("最新关键词B的消息")).toBeInTheDocument();
    expect(screen.queryByText("过期请求A的消息")).not.toBeInTheDocument();
    expect(screen.queryByText("旧列表错误")).not.toBeInTheDocument();
  });

  it("keeps the latest list error when an older request succeeds", async () => {
    const old = deferred<ReturnType<typeof list>>();
    api.getMarketEvents.mockImplementation(({ keyword }: { keyword?: string }) => keyword === "A" ? old.promise : keyword === "B" ? Promise.reject(new Error("当前列表失败")) : Promise.resolve(list("初始市场消息")));
    render(<MarketNews />);
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: /market_news_filter$/ }));
    const input = screen.getByPlaceholderText("market_news_keyword_placeholder");
    fireEvent.change(input, { target: { value: "A" } });
    await waitFor(() => expect(api.getMarketEvents).toHaveBeenCalledWith(expect.objectContaining({ keyword: "A" })));
    fireEvent.change(input, { target: { value: "B" } });
    await screen.findByText("当前列表失败");
    await act(async () => old.resolve(list("不应显示的旧响应")));
    expect(screen.getByText("当前列表失败")).toBeInTheDocument();
    expect(screen.queryByText("不应显示的旧响应")).not.toBeInTheDocument();
  });
});

describe("R01: collection errors are visible to the operator", () => {
  it("does not reload or publish notifications after navigation unmounts the page", async () => {
    const pending = deferred<{ collected: number; errors: string[]; status: string }>();
    api.collectMarketEvents.mockReturnValue(pending.promise);
    const page = render(<MarketNews />);
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: /market_news_collect$/ }));
    await waitFor(() => expect(api.collectMarketEvents).toHaveBeenCalledTimes(1));
    const calls = api.getMarketEvents.mock.calls.length;
    page.unmount();
    await act(async () => pending.resolve({ collected: 5, errors: [], status: "done" }));
    expect(api.getMarketEvents).toHaveBeenCalledTimes(calls);
    expect(showToast).not.toHaveBeenCalled();
  });

  it("does not publish a collect result if navigation occurs during its list refresh", async () => {
    const refresh = deferred<ReturnType<typeof list>>();
    const page = render(<MarketNews />);
    await loaded();
    api.getMarketEvents.mockReturnValue(refresh.promise);
    api.collectMarketEvents.mockResolvedValue({ collected: 5, errors: [], status: "done" });
    fireEvent.click(screen.getByRole("button", { name: /market_news_collect$/ }));
    await waitFor(() => expect(api.getMarketEvents).toHaveBeenCalledTimes(2));
    page.unmount();
    await act(async () => refresh.resolve(list("离开后的刷新结果")));
    expect(showToast).not.toHaveBeenCalled();
  });
  it("shows all-source failure instead of a no-new-events toast", async () => {
    api.collectMarketEvents.mockResolvedValue({ collected: 0, errors: ["caixin: offline"], status: "failed" });
    render(<MarketNews />);
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: /market_news_collect$/ }));
    await waitFor(() => expect(showToast).toHaveBeenCalledWith("error", expect.stringContaining("caixin: offline")));
    expect(showToast).not.toHaveBeenCalledWith("info", "market_news_collect_nothing");
  });

  it("shows partial failure alongside the added count", async () => {
    api.collectMarketEvents.mockResolvedValue({ collected: 5, errors: ["caixin: offline"], status: "partial" });
    render(<MarketNews />);
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: /market_news_collect$/ }));
    await waitFor(() => expect(showToast).toHaveBeenCalledWith("info", expect.stringContaining("market_news_collect_partial")));
    expect(showToast.mock.calls[0][1]).toContain('"n":5');
    expect(showToast.mock.calls[0][1]).toContain("caixin: offline");
  });
});
