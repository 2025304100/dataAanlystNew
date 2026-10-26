import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

const { api } = vi.hoisted(() => ({ api: { getMarketEvents: vi.fn(), getMarketEventCollectRuns: vi.fn(),
  getMarketEventHotspots: vi.fn(), collectMarketEvents: vi.fn(), updateMarketEvent: vi.fn() } }));
vi.mock("../../api/client", () => ({ api }));
vi.mock("../../context/AppContext", () => ({ useApp: () => ({ locale: "zh-CN", showToast: vi.fn() }) }));
vi.mock("../../i18n", () => ({ t: (key: string) => key, template: (key: string) => key }));
vi.mock("../ThemeManagementModal", () => ({ default: () => null }));
import MarketNews from "../MarketNews";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
it("keeps valid custom hex colors and displays an unknown source without an i18n key", async () => {
  api.getMarketEvents.mockResolvedValue({ total: 1, by_scope: {}, by_level: {}, events: [{ id: 1, title: "显示回归消息",
    importance_level: 3, sentiment: "positive", impact_scope: "other", affected_sectors: "custom_review",
    sector_tag_source: "manual", source: "new-source", published_at: "2026-10-26T05:50:00" }] });
  api.getMarketEventCollectRuns.mockResolvedValue([]);
  api.getMarketEventHotspots.mockImplementation(async (days) => ({ days, sectors: [] }));
  render(<MarketNews />);
  fireEvent.click(await screen.findByText("显示回归消息"));
  expect(screen.getByText("new-source", { exact: true })).toBeInTheDocument();
  expect(screen.queryByText("market_news_source_new-source")).not.toBeInTheDocument();
  await waitFor(() => expect(document.querySelector(".mn-keywords .ant-tag")).toHaveStyle({ backgroundColor: "#64748b" }));
});
