import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../client";

describe("data-center external input API contracts", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: "task-audit" }) });
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => { vi.unstubAllGlobals(); });

  it.each([
    ["overview", () => api.getExternalDataOverview(), "/api/v1/external-data/overview"],
    ["coverage", () => api.getExternalDataCoverage(), "/api/v1/external-data/coverage"],
    ["capabilities", () => api.getExternalDataSyncCapabilities(), "/api/v1/external-data/sync-capabilities"],
    ["task", () => api.getExternalDataSyncTask("id/a#b"), "/api/v1/external-data/sync-tasks/id%2Fa%23b"],
    ["partitions", () => api.getExternalDataSyncTaskPartitions("id/a#b"), "/api/v1/external-data/sync-tasks/id%2Fa%23b/partitions"],
    ["score task", () => api.scoringGetTask("id/a#b"), "/api/v1/scoring/tasks/id%2Fa%23b"],
    ["watchlists", () => api.getWatchlists(), "/api/v1/watchlists"],
  ] as const)("GET %s uses its public API and encodes task identifiers", async (_, call, expected) => {
    await call();
    expect(fetchMock).toHaveBeenCalledWith(expected, expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(fetchMock.mock.calls[0][1].body).toBeUndefined();
  });

  it.each(["preview", "start"] as const)("%s retains selected scope, watchlist, dates, and northbound choice", async (action) => {
    const payload = { dataset: "capital_flow" as const, source: "watchlist" as const, watchlist_id: 22, include_northbound: false, mode: "backfill" as const, start_date: "2026-07-01", end_date: "2026-08-02", lookback_days: 30, limit: 20 };
    if (action === "preview") await api.previewExternalDataSyncPlan(payload);
    else await api.startExternalDataSync(payload);
    expect(fetchMock.mock.calls[0][0]).toBe(action === "preview" ? "/api/v1/external-data/sync-plans/preview" : "/api/v1/external-data/sync-tasks");
    expect(fetchMock.mock.calls[0][1].method).toBe("POST");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(payload);
  });

  it("scans gaps with date and dataset filters and keeps the two-minute scan timeout", async () => {
    const timer = vi.spyOn(window, "setTimeout");
    try {
      await api.getExternalDataGaps("financial", "2026-07-01", "2026-08-02", 500);
      expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/external-data/gaps?dataset=financial&start_date=2026-07-01&end_date=2026-08-02&limit=500");
      expect(timer).toHaveBeenCalledWith(expect.any(Function), 120000);
    } finally { timer.mockRestore(); }
  });

  it("repairs exactly the gaps returned by diagnosis", async () => {
    const payload = { dataset: "fundamental" as const, start_date: "2026-07-01", end_date: "2026-08-02", gaps: [{ symbol_id: 1, symbol: "600000", trade_date: "2026-07-10" }] };
    await api.repairExternalDataGaps(payload);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/external-data/gaps/repair");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(payload);
  });

  it.each(["cancel", "retry"] as const)("%s uses POST with the exact encoded existing task ID", async (action) => {
    if (action === "cancel") await api.cancelExternalDataSyncTask("id/a#b");
    else await api.retryExternalDataSyncTask("id/a#b");
    expect(fetchMock.mock.calls[0][0]).toBe(`/api/v1/external-data/sync-tasks/id%2Fa%23b/${action}`);
    expect(fetchMock.mock.calls[0][1].method).toBe("POST");
  });

  it("imports the explicit CSV body without background upload", async () => {
    const payload = { csv_text: "symbol,timestamp,open,high,low,close,volume,amount\n600000,2026-08-02 14:59,1,1,1,1,1,1", filename: "tail.csv" };
    await api.importTailProxyMinutes(payload);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/external-data/tail-proxy/import");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(payload);
  });

  it.each([
    { universe_source: "watchlist" as const, watchlist_id: 22 },
    { universe_source: "positions" as const, portfolio_id: 44 },
    { universe_source: "all" as const },
  ])("score refresh retains $universe_source without enabling model training", async (scope) => {
    const payload = { full_refresh: true, start_date: "2026-07-01", end_date: "2026-08-02", train_model: false, materialize_scores: true, ...scope };
    await api.scoringCreateTask(payload);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/scoring/tasks");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(payload);
  });

  it("surfaces a provider conflict with its business message", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 409, statusText: "Conflict", json: async () => ({ detail: "该数据集正在同步" }) });
    await expect(api.startExternalDataSync({ dataset: "fundamental", source: "all" })).rejects.toMatchObject({ message: "该数据集正在同步", status_code: 409 });
  });
});
