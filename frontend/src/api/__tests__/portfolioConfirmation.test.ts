import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../client";

afterEach(() => vi.unstubAllGlobals());

describe("portfolio confirmation request contract", () => {
  it("sends the note and the reconciled trading date, including across a holiday", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);
    await api.confirmPortfolioReconciliation(7, {
      ack: true, review_note: "  已核对所有差异并确认修复完成  ", trade_date: "2026-09-30",
    });
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.trade_date).toBe("2026-09-30");
    expect(body.review_note).toBe("已核对所有差异并确认修复完成");
    expect(body.acknowledge_all_diffs_cleared).toBe(true);
    expect(body.force_skip_re_reconcile).toBe(false);
  });

  it("leaves an unspecified date to the backend's trading calendar", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);
    await api.confirmPortfolioReconciliation(7, { ack: true, review_note: "已核对所有差异并确认修复完成", force_skip: false });
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body).not.toHaveProperty("trade_date");
    expect(body.force_skip_re_reconcile).toBe(false);
  });

  it("confirms only the snapshot and model the user reviewed", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);
    await api.confirmPortfolioModelBinding(7, {
      snapshot_id: "snapshot-7", model_run_id: "model-3", ack: true, review_note: "  已查看本次绑定变更并接受  ",
    });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/portfolios/7/confirm-model-binding");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      snapshot_id: "snapshot-7", model_run_id: "model-3", ack: true, review_note: "已查看本次绑定变更并接受",
    });
  });
});
