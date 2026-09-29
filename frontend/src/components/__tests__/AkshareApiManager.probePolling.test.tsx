// PT-DEF-18 的核心行为：探测"慢的上游"不能被误判成失败，"心跳断了"不能无限转圈。
// 这两条就是本次改造的目的 —— 旧实现靠一个 30s 的 HTTP 超时赌上游快慢，慢成功的
// 接口必然被记成失败。
//
// 注：这里刻意不用 fake timers。antd Table 的行渲染依赖定时器/观察器，在假时钟下
// 整个表格一行都不出来（实测会把断言变成"找不到按钮"），反而掩盖真实行为。
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { message } from "antd";

const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    listAkshareApis: vi.fn(),
    listAkshareStrategies: vi.fn(),
    submitApiProbe: vi.fn(),
    getApiProbeTask: vi.fn(),
    updateAkshareApiConfig: vi.fn(),
  },
}));

// 注意路径深度：本文件在 src/components/__tests__/ 下，被测模块在 src/ 下，
// 所以必须写 ../../（写成 ../ 时 vi.mock 会静默 mock 到一个不存在的模块，
// 组件用的仍是真 api —— 症状是"mock 从未被调用"，很容易被误判成组件逻辑坏了）。
vi.mock("../../api/client", () => ({ api: mockApi }));
vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, vars: Record<string, unknown>) => `${key}:${JSON.stringify(vars)}`,
  getLocale: () => "zh-CN",
}));

import AkshareApiManager from "../AkshareApiManager";

const API_KEY = "slow_quote_api";

const ROW = {
  key: API_KEY,
  name: "慢接口",
  category: "realtime",
  module: "akshare",
  description: "慢但可用的接口",
  default_strategy: "standard",
  enabled: true,
  anti_risk_strategy: "standard",
  delay_min_ms: 200,
  delay_max_ms: 500,
  last_probe_at: null,
  last_probe_success: null,
  last_probe_latency_ms: null,
  last_probe_error: null,
  last_call_at: null,
  last_call_success: null,
  last_call_error: null,
  total_calls: 0,
  total_failures: 0,
};

const taskStatus = (status: string, extra: Record<string, unknown> = {}) => ({
  task_id: `task-${API_KEY}`,
  api_key: API_KEY,
  status,
  stage: status === "done" ? "done" : "calling",
  percent: status === "done" ? 100 : 50,
  message: status === "done" ? "done" : "waiting upstream 37s",
  heartbeat_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  seconds_since_heartbeat: 1,
  heartbeat_stale: false,
  result: null,
  ...extra,
});

const okResult = (latency: number) => ({
  key: API_KEY, success: true, latency_ms: latency, error: null,
});

/* eslint-disable @typescript-eslint/no-explicit-any */
let successSpy: any;
let errorSpy: any;

beforeEach(() => {
  vi.clearAllMocks();
  mockApi.listAkshareApis.mockResolvedValue([ROW]);
  // 必须给出档位列表：单元格按 anti_risk_strategy 查档位名，空数组会让整张表抛错卸载
  // （表现是"找不到探测按钮"，很容易被误判成轮询逻辑坏了）
  mockApi.listAkshareStrategies.mockResolvedValue([
    { key: "fast", name: "快速", delay_min_ms: 100, delay_max_ms: 200, max_retries: 1, desc: "快速档" },
    { key: "standard", name: "标准", delay_min_ms: 200, delay_max_ms: 500, max_retries: 3, desc: "标准档" },
    { key: "conservative", name: "保守", delay_min_ms: 1000, delay_max_ms: 2000, max_retries: 3, desc: "保守档" },
    { key: "extreme", name: "极保守", delay_min_ms: 3000, delay_max_ms: 5000, max_retries: 5, desc: "极保守档" },
    { key: "custom", name: "自定义", delay_min_ms: null, delay_max_ms: null, max_retries: 3, desc: "自定义档" },
  ]);
  mockApi.updateAkshareApiConfig.mockResolvedValue(ROW);
  mockApi.submitApiProbe.mockResolvedValue({
    task_id: `task-${API_KEY}`, api_key: API_KEY, status: "queued", reused: false,
  });
  successSpy = vi.spyOn(message, "success").mockImplementation(() => undefined as any);
  errorSpy = vi.spyOn(message, "error").mockImplementation(() => undefined as any);
});

const clickProbe = async () => {
  await waitFor(() => expect(screen.getAllByText("apiMgmtProbe").length).toBeGreaterThan(0));
  fireEvent.click(screen.getAllByText("apiMgmtProbe")[0]);
};

describe("AkshareApiManager 探测轮询", () => {
  it(
    "上游很慢（多轮 running 后才终态）：最终报成功，中途不误判为失败",
    async () => {
      let poll = 0;
      mockApi.getApiProbeTask.mockImplementation(async () => {
        poll += 1;
        if (poll < 3) return taskStatus("running");
        return taskStatus("done", { result: okResult(41000) });
      });

      render(<AkshareApiManager />);
      await clickProbe();

      await waitFor(() => expect(successSpy).toHaveBeenCalled(), { timeout: 12000 });
      expect(poll).toBeGreaterThanOrEqual(3);
      expect(mockApi.submitApiProbe).toHaveBeenCalledTimes(1);
      // 关键：41 秒才回来的探测必须记成功 —— 旧实现在这里必然已经超时判失败
      expect(errorSpy).not.toHaveBeenCalled();
      expect(String(successSpy.mock.calls[0]?.[0])).toContain("41000");
    },
    20000,
  );

  it(
    "心跳断了：明确报错并停止轮询，绝不无限转圈",
    async () => {
      let poll = 0;
      mockApi.getApiProbeTask.mockImplementation(async () => {
        poll += 1;
        if (poll < 2) return taskStatus("running");
        return taskStatus("running", { heartbeat_stale: true, seconds_since_heartbeat: 900 });
      });

      render(<AkshareApiManager />);
      await clickProbe();

      await waitFor(() => expect(errorSpy).toHaveBeenCalled(), { timeout: 12000 });
      expect(String(errorSpy.mock.calls[0]?.[0])).toContain("apiProbeStale");
      const pollsAfterStop = poll;
      await new Promise((r) => setTimeout(r, 300));
      expect(poll).toBe(pollsAfterStop); // 停了，不再无限轮
      expect(successSpy).not.toHaveBeenCalled();
    },
    20000,
  );

  it("任务终态 failed 但没有 result：把状态说清楚，不能当作成功", async () => {
    mockApi.getApiProbeTask.mockResolvedValue(
      taskStatus("failed", { message: "probe exceeded hard limit 300s" }),
    );

    render(<AkshareApiManager />);
    await clickProbe();

    await waitFor(() => expect(errorSpy).toHaveBeenCalled(), { timeout: 8000 });
    expect(successSpy).not.toHaveBeenCalled();
    expect(String(errorSpy.mock.calls[0]?.[0])).toContain("probe exceeded hard limit");
  });

  it("成功回执里带上真实延迟，而不是只报一句泛泛的成功", async () => {
    mockApi.getApiProbeTask.mockResolvedValue(taskStatus("done", { result: okResult(1234) }));

    render(<AkshareApiManager />);
    await clickProbe();

    await waitFor(() => expect(successSpy).toHaveBeenCalled(), { timeout: 8000 });
    expect(String(successSpy.mock.calls[0]?.[0])).toContain("1234");
  });

  it("提交失败（后端 404/500）也要报错，不能静默", async () => {
    mockApi.submitApiProbe.mockRejectedValue(new Error("Unknown api_key"));

    render(<AkshareApiManager />);
    await clickProbe();

    await waitFor(() => expect(errorSpy).toHaveBeenCalled(), { timeout: 8000 });
    expect(String(errorSpy.mock.calls[0]?.[0])).toContain("Unknown api_key");
    expect(mockApi.getApiProbeTask).not.toHaveBeenCalled();
  });
});
