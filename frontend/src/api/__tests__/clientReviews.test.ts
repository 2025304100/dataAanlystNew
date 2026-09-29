// 复盘记录接口的请求体契约（PT-DEF-26）。
//
// 后端 ReviewCreate 里 start_date / end_date 是**必填**，快照字段叫 report_snapshot。
// 旧 client 只发 { note, attribution_snapshot }：既缺必填字段又用错字段名，
// 所以「创建复盘」在任何界面下都只会拿到 422 —— 而旧界面本身还没挂载，
// 这个坏契约就一直没被发现。这里把请求体钉住，防止再次漂移。
import { describe, expect, it, vi } from "vitest";

import { api } from "../client";

function mockedFetch(payload: unknown = { id: 1 }) {
  const response = { ok: true, json: async () => payload } as Response;
  return vi.fn().mockResolvedValue(response);
}

describe("api.createReview 请求体契约", () => {
  it("带上后端必填的时间范围，并且不再发送已废弃的 attribution_snapshot 字段", async () => {
    const fetchMock = mockedFetch();
    vi.stubGlobal("fetch", fetchMock);
    try {
      await api.createReview(7, {
        start_date: "2026-07-01",
        end_date: "2026-07-31",
        note: "本月归因复盘",
        title: "7月复盘",
      });
    } finally {
      vi.unstubAllGlobals();
    }

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toContain("/portfolios/7/reviews");
    expect(init.method).toBe("POST");

    const body = JSON.parse(String(init.body));
    expect(body).toMatchObject({
      start_date: "2026-07-01",
      end_date: "2026-07-31",
      note: "本月归因复盘",
      title: "7月复盘",
    });
    // 后端没有这个字段名；带快照的正确字段是 report_snapshot
    expect(body.attribution_snapshot).toBeUndefined();
  });

  it("不传 report_snapshot 时由服务端实时计算归因（请求体保持精简）", async () => {
    const fetchMock = mockedFetch();
    vi.stubGlobal("fetch", fetchMock);
    try {
      await api.createReview(7, { start_date: "2026-01-01", end_date: "2026-01-31" });
    } finally {
      vi.unstubAllGlobals();
    }
    const body = JSON.parse(String((fetchMock.mock.calls[0] as [string, RequestInit])[1].body));
    expect(body.report_snapshot).toBeUndefined();
    expect(body.start_date).toBe("2026-01-01");
  });

  it("显式给出 report_snapshot 时原样透传，避免服务端重复计算", async () => {
    const fetchMock = mockedFetch();
    vi.stubGlobal("fetch", fetchMock);
    try {
      await api.createReview(7, {
        start_date: "2026-01-01",
        end_date: "2026-01-31",
        report_snapshot: { by_member: { "600000": 12.5 } },
      });
    } finally {
      vi.unstubAllGlobals();
    }
    const body = JSON.parse(String((fetchMock.mock.calls[0] as [string, RequestInit])[1].body));
    expect(body.report_snapshot).toEqual({ by_member: { "600000": 12.5 } });
  });
});
