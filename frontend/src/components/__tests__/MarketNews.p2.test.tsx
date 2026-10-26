import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const { api, showToast } = vi.hoisted(() => ({ api: {
  getMarketEvents: vi.fn(), getMarketEventCollectRuns: vi.fn(), getMarketEventHotspots: vi.fn(),
  collectMarketEvents: vi.fn(), updateMarketEvent: vi.fn(),
}, showToast: vi.fn() }));
vi.mock("../../api/client", () => ({ api }));
vi.mock("../../context/AppContext", () => ({ useApp: () => ({ locale: "zh-CN", showToast }) }));
vi.mock("../../i18n", () => ({ t: (key: string) => key,
  template: (key: string, values: Record<string, unknown>) => `${key}:${JSON.stringify(values)}` }));
vi.mock("../ThemeManagementModal", () => ({ default: () => null }));
import MarketNews from "../MarketNews";

const item = (id: number, changes: Record<string, unknown> = {}) => ({
  id, title: `市场消息${id}`, summary: null, impact_scope: "other", importance_level: 3, sentiment: "positive",
  source: "test", affected_sectors: "solar_pv", sector_tag_source: "auto", published_at: new Date().toISOString(),
  created_at: new Date().toISOString(), expires_at: null, ...changes,
});
const result = (events: ReturnType<typeof item>[], total = events.length) => ({ events, total,
  by_scope: {}, by_level: {}, expired_total: 0, as_of: "2026-10-26T00:00:00", snapshot_max_id: 1000 });
const hotspots = (days: number, custom = false) => ({ days, sectors: [{ sector_id: custom ? "custom_review" : "solar_pv",
  bull: 3, bear: 0, neutral: 0, total: 3, max_level: 3, event_ids: [1] }] });
beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  api.getMarketEvents.mockResolvedValue(result([item(1)]));
  api.getMarketEventCollectRuns.mockResolvedValue([]);
  api.getMarketEventHotspots.mockImplementation(async (days) => hotspots(days));
  api.updateMarketEvent.mockResolvedValue({});
  api.collectMarketEvents.mockResolvedValue({ collected: 0, errors: [], status: "done" });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("R08: recalculates 7d counts and identifies the loaded-page coverage", async () => {
  const ago = (days: number) => new Date(Date.now()-days*86400_000).toISOString();
  api.getMarketEvents.mockResolvedValue(result([item(1, { published_at: ago(2) }),
    ...Array.from({ length: 5 }, (_, i) => item(i+2, { published_at: ago(20) }))]));
  api.getMarketEventHotspots.mockRejectedValue(new Error("offline"));
  render(<MarketNews />);
  await screen.findByText("market_news_hotspot_fallback_hint");
  fireEvent.click(screen.getByRole("button", { name: "market_news_last_7d" }));
  await waitFor(() => expect(document.querySelector(".mn-sector-count")).toHaveTextContent("1market_news_count_unit"));
  expect(screen.getByText(/market_news_hotspot_loaded_hint:/)).toHaveTextContent('"n":6');
  fireEvent.click(screen.getByRole("button", { name: "market_news_last_30d" }));
  await waitFor(() => expect(document.querySelector(".mn-sector-count")).toHaveTextContent("6market_news_count_unit"));
});

it("R09/R10: detail uses the stored custom label even when local keywords disagree", async () => {
  api.getMarketEvents.mockResolvedValue(result([item(1, { title: "光伏组件增长", affected_sectors: "custom_review", sector_tag_source: "manual" })]));
  api.getMarketEventHotspots.mockImplementation(async (days) => hotspots(days, true));
  render(<MarketNews />);
  fireEvent.click(await screen.findByText("光伏组件增长"));
  expect(document.querySelector(".mn-keywords")).toHaveTextContent("custom_review");
  expect(document.querySelector(".mn-keywords")).not.toHaveTextContent("market_news_sector_solar_pv");
  expect(document.querySelector(".mn-sector-name")).toHaveTextContent("custom_review");
  expect(screen.getByText("market_news_tags_manual")).toBeInTheDocument();
});

it("R09: explicit empty stored labels do not fall back to local keywords", async () => {
  api.getMarketEvents.mockResolvedValue(result([item(1, { title: "光伏组件增长", affected_sectors: "", sector_tag_source: "manual" })]));
  render(<MarketNews />);
  fireEvent.click(await screen.findByText("光伏组件增长"));
  expect(document.querySelector(".mn-keywords")).not.toBeInTheDocument();
});

it("R09: a historical label can explicitly return to automatic classification", async () => {
  api.getMarketEvents.mockResolvedValue(result([item(1, { sector_tag_source: "legacy", affected_sectors: "retired_id" })]));
  render(<MarketNews />);
  fireEvent.click(await screen.findByText("市场消息1"));
  expect(screen.getByText("market_news_tags_legacy")).toBeInTheDocument();
  api.getMarketEvents.mockResolvedValue(result([item(1)]));
  fireEvent.click(screen.getByRole("button", { name: "market_news_tags_reset_auto" }));
  await waitFor(() => expect(api.updateMarketEvent).toHaveBeenCalledWith(1, { sector_tag_source: "auto" }));
  await screen.findByText("market_news_tags_auto");
});

it("R13: preserves the real total, reaches the next page and resets on filtering", async () => {
  api.getMarketEvents.mockImplementation(async (query) => query.offset === 50 ? result([item(201)], 51) : result([item(1)], 51));
  render(<MarketNews />);
  await screen.findByText("市场消息1");
  expect(document.querySelector(".mn-footer-stats")).toHaveTextContent("51");
  fireEvent.click(screen.getByTitle("2"));
  await screen.findByText("市场消息201");
  expect(api.getMarketEvents).toHaveBeenLastCalledWith(expect.objectContaining({ limit: 50, offset: 50,
    include_expired: false, as_of: "2026-10-26T00:00:00", snapshot_max_id: 1000 }));
  fireEvent.click(screen.getByRole("button", { name: /market_news_filter$/ }));
  fireEvent.change(screen.getByPlaceholderText("market_news_keyword_placeholder"), { target: { value: "新关键词" } });
  await waitFor(() => expect(api.getMarketEvents).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 0, keyword: "新关键词" })));
});

it("R13: favorites outside the normal page are queried by ID with current filters", async () => {
  localStorage.setItem("mn_favorites", JSON.stringify([201]));
  api.getMarketEvents.mockImplementation(async (query) => result([item(query.event_ids ? 201 : 1)]));
  render(<MarketNews />);
  await screen.findByText("市场消息1");
  fireEvent.click(screen.getByRole("button", { name: /market_news_filter$/ }));
  const selector = screen.getByText("market_news_all_items").closest(".ant-select")!.querySelector(".ant-select-selector")!;
  fireEvent.mouseDown(selector);
  fireEvent.click(await screen.findByText("⭐ market_news_favorite_only"));
  await screen.findByText("市场消息201");
  expect(api.getMarketEvents).toHaveBeenCalledWith(expect.objectContaining({ event_ids: [201], include_expired: false, offset: 0 }));
  expect(screen.queryByText("市场消息1")).not.toBeInTheDocument();
});

function downloadMocks() {
  const create = vi.fn((_blob: Blob) => "blob:market-export");
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: create });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  return create;
}

it("R13: export reads all 201 matches, not just the visible 50 rows", async () => {
  const rows = Array.from({ length: 201 }, (_, i) => item(i+1));
  api.getMarketEvents.mockImplementation(async (query) => result(rows.slice(query.offset, query.offset+query.limit), 201));
  const create = downloadMocks();
  render(<MarketNews />);
  await screen.findByText("市场消息1");
  fireEvent.click(screen.getByRole("button", { name: /market_news_export$/ }));
  await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
  const blob = create.mock.calls[0][0] as Blob;
  const csv = await new Promise<string>((resolve) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.readAsText(blob); });
  expect(csv.split("\r\n")).toHaveLength(202);
  expect(csv).toContain("市场消息201");
  expect(showToast).toHaveBeenCalledWith("success", expect.stringContaining('"n":201'));
});

it("R13: a later-page failure never downloads a partial CSV", async () => {
  const rows = Array.from({ length: 201 }, (_, i) => item(i+1));
  api.getMarketEvents.mockImplementation(async (query) => {
    if (query.offset === 200) throw new Error("export-page-failed");
    return result(rows.slice(query.offset, query.offset+query.limit), 201);
  });
  const create = downloadMocks();
  render(<MarketNews />);
  await screen.findByText("市场消息1");
  fireEvent.click(screen.getByRole("button", { name: /market_news_export$/ }));
  await waitFor(() => expect(showToast).toHaveBeenCalledWith("error", "export-page-failed"));
  expect(create).not.toHaveBeenCalled();
});

it("R13/R07: changing filters during export prevents downloading the old query", async () => {
  let release!: (value: unknown) => void;
  api.getMarketEvents.mockImplementation((query) => query.limit === 200
    ? new Promise((resolve) => { release = resolve; }) : Promise.resolve(result([item(1)])));
  const create = downloadMocks();
  render(<MarketNews />);
  await screen.findByText("市场消息1");
  fireEvent.click(screen.getByRole("button", { name: /market_news_export$/ }));
  await waitFor(() => expect(release).toBeTypeOf("function"));
  fireEvent.click(screen.getByRole("button", { name: /market_news_filter$/ }));
  fireEvent.change(screen.getByPlaceholderText("market_news_keyword_placeholder"), { target: { value: "新条件" } });
  await act(async () => release(result([item(1)])));
  await waitFor(() => expect(showToast).toHaveBeenCalledWith("error", "market_news_request_superseded"));
  expect(create).not.toHaveBeenCalled();
});
