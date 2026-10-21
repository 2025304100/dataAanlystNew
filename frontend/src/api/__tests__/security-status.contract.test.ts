import { afterEach, expect, it, vi } from "vitest";
import { api } from "../client";

afterEach(() => vi.unstubAllGlobals());

it("checks a historical file before importing the exact reviewed content", async () => {
  const digest = "a".repeat(64);
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ can_import: true, content_sha256: digest }) });
  vi.stubGlobal("fetch", fetchMock);
  const payload = { csv_text: "symbol,trade_date\n600000,2026-09-01", filename: "history.csv", source_name: "交易所历史文件", source_reference: "history.csv", source_version: "2026-09", source_updated_at: "2026-09-02T00:00:00+08:00" };
  await api.previewSecurityStatusImport(payload);
  expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/security-status/import/preview");
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(payload);
  await api.importSecurityStatus({ ...payload, preview_sha256: digest, confirmed: true });
  expect(fetchMock.mock.calls[1][0]).toBe("/api/v1/security-status/import");
  expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ ...payload, preview_sha256: digest, confirmed: true });
});

it("reads existing historical status and template without starting a download job", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
  vi.stubGlobal("fetch", fetchMock);
  await api.getSecurityStatusCoverage();
  await api.getSecurityStatusTemplate();
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(["/api/v1/security-status/coverage", "/api/v1/security-status/template"]);
  expect(fetchMock.mock.calls.every(([, options]) => !options?.method || options.method === "GET")).toBe(true);
});
