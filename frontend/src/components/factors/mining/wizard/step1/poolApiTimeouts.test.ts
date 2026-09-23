/**
 * 挖掘重接口必须带专用超时（2026-09-22 实测）。

 * 背景：`client.requestJson` 默认超时 **20 秒**。而 `preview`（全市场 ≈5,500 只、
 * 20 日窗口的分位阈值与逐条排除都在服务端算）实测会超过 20s —— 用户看到
 * 「请求超时」，可底部统计随后就出来了（**假失败**）。同类重活还有
 * from-filter（物化成员）、snapshot（冻结+分析）、import 系、filter-presets（分位）。
 *
 * 本哨兵：这些入口必须显式传 `timeoutMs`，且不得低于 60s。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const requestJson = vi.fn<(url: unknown, options?: unknown) => Promise<unknown>>(
  async () => ({}),
);

vi.mock("../../../../../api/client", () => ({
  requestJson: (url: unknown, options?: unknown) => requestJson(url, options),
}));

import {
  createPoolFromFilter,
  createSnapshot,
  fetchFilterPresets,
  fetchMembers,
  importPoolMembers,
  previewImport,
  previewPool,
} from "./poolApi";

const MIN_MS = 60_000;

function lastOpts(): Record<string, unknown> {
  const calls = requestJson.mock.calls;
  const call = calls.length > 0 ? calls[calls.length - 1] : undefined;
  return (call?.[1] ?? {}) as Record<string, unknown>;
}

function expectTimeoutAtLeast(doc: string): void {
  const ms = lastOpts().timeoutMs;
  expect(typeof ms, `${doc} 必须显式传 timeoutMs`).toBe("number");
  expect(ms as number, `${doc} 的超时不得低于 ${MIN_MS}ms`).toBeGreaterThanOrEqual(MIN_MS);
}

describe("挖掘重接口专用超时", () => {
  beforeEach(() => {
    requestJson.mockClear();
  });

  it("previewPool（全市场分位计算）", async () => {
    await previewPool({ valuation: { total_market_cap: { max: 1e9 } } });
    expectTimeoutAtLeast("previewPool");
  });

  it("createPoolFromFilter（成员物化）", async () => {
    await createPoolFromFilter({ name: "t", filter_config: {} });
    expectTimeoutAtLeast("createPoolFromFilter");
  });

  it("createSnapshot（冻结 + 全池分析）", async () => {
    await createSnapshot("pool-1", { analyze: true });
    expectTimeoutAtLeast("createSnapshot");
  });

  it("fetchMembers（成员分页）", async () => {
    await fetchMembers("pool-1", { page: 1, page_size: 20 });
    expectTimeoutAtLeast("fetchMembers");
  });

  it("fetchFilterPresets（服务端分位阈值）", async () => {
    await fetchFilterPresets();
    expectTimeoutAtLeast("fetchFilterPresets");
  });

  it("previewImport（上传解析预览）", async () => {
    await previewImport(new File(["symbol\n000001\n"], "a.csv", { type: "text/csv" }));
    expectTimeoutAtLeast("previewImport");
  });

  it("importPoolMembers（上传入池）", async () => {
    await importPoolMembers("pool-1", new File(["symbol\n000001\n"], "a.csv", { type: "text/csv" }));
    expectTimeoutAtLeast("importPoolMembers");
  });
});
