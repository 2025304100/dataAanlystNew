import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("../../context/AppContext", () => ({ useApp: () => ({ locale: "zh-CN", setActiveTab: vi.fn(), setActiveSubTab: vi.fn() }) }));
vi.mock("../../i18n", () => ({
  t: (key: string) => key, template: (key: string) => key,
  factorLabel: (code: string, name?: string) => name || code,
}));
vi.mock("antd", async () => {
  const original = await vi.importActual<typeof import("antd")>("antd");
  return { ...original, App: { ...original.App, useApp: () => ({ message: { error: vi.fn(), warning: vi.fn(), success: vi.fn() } }) } };
});

import FactorEvaluationLab from "../factors/FactorEvaluationLab";

beforeEach(() => localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

it("serializes a real version as a string for preflight and keeps the task's numeric version contract", async () => {
  const preflightBodies: Record<string, unknown>[] = [];
  const taskBodies: Record<string, unknown>[] = [];
  const historyQueries: URLSearchParams[] = [];
  const respond = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
  vi.stubGlobal("fetch", vi.fn(async (input: string | Request, options?: RequestInit) => {
    const url = typeof input === "string" ? input : input.url;
    const path = url.split("?")[0];
    if (path.endsWith("/scoring/factor-definitions")) return respond({ items: [{ id: 7, code: "turnover_z20", name: "换手率", factor_kind: "continuous", direction: "higher_better" }], total: 1 });
    if (path.endsWith("/factors/turnover_z20/versions")) return respond([{ id: 12, factor_id: 7, version: 2, is_latest: true, direction: "higher_better", postprocess: null }]);
    if (path.endsWith("/factor-evaluation/preflight")) {
      const body = JSON.parse(String(options?.body));
      preflightBodies.push(body);
      // The HTTP schema accepts a version string here; task creation accepts an integer.
      if (typeof body.factor_version_id !== "string") return respond({ detail: [{ loc: ["body", "factor_version_id"], msg: "Input should be a valid string", type: "string_type" }] }, 422);
      return respond({ overall: { passed: true, blocking_count: 0 }, items: [] });
    }
    if (path.endsWith("/factor-evaluation/tasks") && options?.method === "POST") {
      const body = JSON.parse(String(options.body));
      taskBodies.push(body);
      return respond({ id: "test-evaluation", status: "completed", percent: 100, stage: "done", result: null, errors: [] });
    }
    if (path.endsWith("/factor-evaluation/runs")) { historyQueries.push(new URLSearchParams(url.split("?")[1])); return respond([]); }
    if (path.endsWith("/factor-evaluation/tasks")) return respond([]);
    throw new Error(`Unexpected mocked HTTP request: ${path}`);
  }));

  render(<FactorEvaluationLab initialFactorCode="turnover_z20" initialVersionId={12} />);
  const submit = screen.getByRole("button", { name: "evalLabSubmit" });
  await waitFor(() => expect(submit).toBeEnabled());
  expect(preflightBodies[0]).toEqual(expect.objectContaining({ factor_code: "turnover_z20", factor_version_id: "12" }));
  expect(taskBodies).toHaveLength(0);
  expect(historyQueries.length).toBeGreaterThan(0);
  expect(historyQueries.every(query => query.get("factor_version_id") === "12")).toBe(true);
  fireEvent.click(submit);
  await waitFor(() => expect(taskBodies).toHaveLength(1));
  expect(taskBodies[0]).toEqual(expect.objectContaining({ factor_code: "turnover_z20", factor_version_id: 12 }));
  await waitFor(() => expect(submit).not.toHaveClass("ant-btn-loading"));
});
