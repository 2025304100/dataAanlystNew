import { afterEach, expect, it, vi } from "vitest";
import { api } from "../client";

afterEach(() => vi.unstubAllGlobals());

it("starts a portfolio task using its trading date and returns the actual queued task", async () => {
  const task = { portfolio_id: 7, trade_date: "2026-10-20", task_id: "task-7", status: "queued", created: true };
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => task });
  vi.stubGlobal("fetch", fetchMock);

  const result = await api.startAutoSimulation(7, { trade_date: "2026-10-20" });

  expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/portfolios/7/auto-simulation/start");
  expect(fetchMock.mock.calls[0][1].method).toBe("POST");
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ trade_date: "2026-10-20" });
  expect(result).toEqual(task);
});
