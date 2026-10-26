import { afterEach, expect, it, vi } from "vitest";
import { api } from "../client";

afterEach(() => vi.unstubAllGlobals());
it("serializes active/favorite/snapshot filters as a read-only GET", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ events: [], total: 0 }) });
  vi.stubGlobal("fetch", fetch);
  await api.getMarketEvents({ include_expired: false, event_ids: [201, 300], as_of: "2026-10-26T00:00:00", snapshot_max_id: 900, offset: 50 });
  const url = new URL(fetch.mock.calls[0][0], "http://localhost");
  expect(url.searchParams.getAll("event_ids")).toEqual(["201", "300"]);
  expect(url.searchParams.get("include_expired")).toBe("false");
  expect(url.searchParams.get("snapshot_max_id")).toBe("900");
  expect(url.searchParams.get("offset")).toBe("50");
  expect(fetch.mock.calls[0][1].method ?? "GET").toBe("GET");
});
it("an explicitly empty ID filter cannot become an unfiltered request", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ events: [], total: 0 }) });
  vi.stubGlobal("fetch", fetch);
  await api.getMarketEvents({ event_ids: [] });
  expect(new URL(fetch.mock.calls[0][0], "http://localhost").searchParams.getAll("event_ids")).toEqual(["0"]);
});
