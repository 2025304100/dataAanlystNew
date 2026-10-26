import { describe, expect, it, vi } from "vitest";
import type { MarketEvent, MarketEventQuery } from "../../types";
import { aggregateMarketSectors, fetchCompleteMarketEvents, marketEventsCsv, marketTime, sortMarketEvents, storedSectorIds } from "../marketNews";

const T = Date.parse("2026-10-26T00:00:00Z");
const item = (id: number, changes: Partial<MarketEvent> = {}): MarketEvent => ({
  id, title: `消息${id}`, summary: null, impact_scope: "other", importance_level: 3, affected_market: "A股",
  affected_sectors: "solar_pv", affected_symbols: null, sentiment: "positive", source: "test", source_url: null,
  is_manual: 0, published_at: new Date(T-2*86400_000).toISOString(), expires_at: null,
  created_at: "2026-10-26T00:00:00", updated_at: "2026-10-26T00:00:00", ...changes,
});
const response = (events: MarketEvent[], total = events.length) => ({
  events, total, by_scope: {}, by_level: {}, expired_total: 0, as_of: "2026-10-26T00:00:00", snapshot_max_id: 10000,
});

describe("R08/R09/R10: window aggregation and authoritative labels", () => {
  const rows = [item(1), ...Array.from({ length: 5 }, (_, i) => item(i+2, { published_at: new Date(T-20*86400_000).toISOString() })),
    item(9, { published_at: new Date(T-60*86400_000).toISOString(), sentiment: "negative", importance_level: 5 })];
  it.each([[7, 1, 0, 3], [30, 6, 0, 3], [90, 6, 1, 5], [365, 6, 1, 5]])(
    "recomputes the %i-day counts", (days, bull, bear, level) => {
      const result = aggregateMarketSectors(rows, days, () => ["solar_pv"], T);
      expect(result).toEqual([{ id: "solar_pv", bull, bear, neutral: 0, maxLevel: level }]);
    });
  it("counts the exact boundary, excludes invalid/null dates, and deduplicates per event", () => {
    const result = aggregateMarketSectors([item(1, { published_at: "2026-10-19T00:00:00" }),
      item(2, { published_at: null }), item(3, { published_at: "bad" }),
      item(4, { published_at: "2026-10-18T23:59:59Z" })], 7, () => ["custom", "custom"], T);
    expect(result).toEqual([{ id: "custom", bull: 1, bear: 0, neutral: 0, maxLevel: 3 }]);
    expect(marketTime("2026-10-26T08:00:00+08:00")).toBe(T);
  });
  it.each(["custom_review,custom_review,retired_id", ""])("preserves explicit stored labels %s", (labels) => {
    const match = vi.fn(() => ["solar_pv"]);
    expect(storedSectorIds(item(1, { affected_sectors: labels }), match)).toEqual(labels ? ["custom_review", "retired_id"] : []);
    expect(match).not.toHaveBeenCalled();
  });
  it("uses legacy matching only for NULL labels", () => {
    expect(storedSectorIds(item(1, { affected_sectors: null }), () => ["custom"])).toEqual(["custom"]);
  });
});

describe("R13: complete reads and deterministic CSV", () => {
  it("reads all 451 rows and reuses the first response snapshot", async () => {
    const rows = Array.from({ length: 451 }, (_, index) => item(index+1));
    const fetch = vi.fn(async (query: MarketEventQuery) => response(rows.slice(query.offset!, query.offset!+query.limit!), 451));
    const result = await fetchCompleteMarketEvents(fetch, { keyword: "消息", include_expired: false });
    expect(result.total).toBe(451);
    expect(fetch.mock.calls.map(([query]) => query.offset)).toEqual([0, 200, 400]);
    expect(fetch.mock.calls[1][0]).toMatchObject({ as_of: "2026-10-26T00:00:00", snapshot_max_id: 10000, keyword: "消息", include_expired: false });
    expect(new Set(result.events.map((e) => e.id)).size).toBe(451);
  });
  it("queries every favorite ID in bounded batches and applies a global order", async () => {
    const ids = Array.from({ length: 205 }, (_, i) => i+1);
    const fetch = vi.fn(async (query: MarketEventQuery) => response(query.event_ids!.filter((id) => id !== 204).map((id) => item(id))));
    const result = await fetchCompleteMarketEvents(fetch, { sort_by: "published_at", include_expired: false }, [...ids, 1, 0, -1]);
    expect(fetch.mock.calls.map(([q]) => q.event_ids!.length)).toEqual([100, 100, 5]);
    expect(result.total).toBe(204);
    expect(result.events[0].id).toBe(205);
    expect(result.events[result.events.length - 1]?.id).toBe(1);
  });
  it("does not query all news for an empty favorites collection", async () => {
    const fetch = vi.fn();
    const result = await fetchCompleteMarketEvents(fetch, {}, []);
    expect(result.total).toBe(0);
    expect(fetch).not.toHaveBeenCalled();
  });
  it.each(["duplicate", "empty", "changed-total"])("rejects %s pages instead of truncating", async (fault) => {
    const fetch = vi.fn(async ({ offset }: MarketEventQuery) => offset === 0 ? response([item(1)], 2)
      : fault === "duplicate" ? response([item(1)], 2) : fault === "empty" ? response([], 2) : response([item(2)], 3));
    await expect(fetchCompleteMarketEvents(fetch, {})).rejects.toThrow("market_news_data_changed");
  });
  it("propagates a page failure without returning a partial export", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response([item(1)], 2)).mockRejectedValueOnce(new Error("offline"));
    await expect(fetchCompleteMarketEvents(fetch, {})).rejects.toThrow("offline");
  });
  it("cancels a superseded query after its in-flight request returns", async () => {
    let current = true;
    const fetch = vi.fn(async () => { current = false; return response([item(1)]); });
    await expect(fetchCompleteMarketEvents(fetch, {}, undefined, () => current)).rejects.toThrow("market_news_request_superseded");
  });
  it("preserves importance, credibility, date and ID tie breakers", () => {
    const rows = [item(1, { source: "cctv" }), item(3, { source: "cctv" }), item(9), item(2, { importance_level: 5 })];
    expect(sortMarketEvents(rows, "importance_level").map((e) => e.id)).toEqual([2, 3, 1, 9]);
  });
  it("exports BOM, complete records, quotes and formula-safe external strings", () => {
    const rows = Array.from({ length: 201 }, (_, i) => item(i+1));
    rows[0] = item(1, { title: '=SUM(1,2)"中文"', summary: "首行\n次行" });
    const csv = marketEventsCsv(rows);
    expect(csv.startsWith("\uFEFFpublished_at,")).toBe(true);
    expect(csv.split("\r\n")).toHaveLength(202);
    expect(csv).toContain('"\'=SUM(1,2)""中文"""');
    expect(csv).toContain("首行 次行");
    expect(csv).toContain('"消息201"');
  });
});
