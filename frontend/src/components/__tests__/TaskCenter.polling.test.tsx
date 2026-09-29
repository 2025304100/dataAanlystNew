// VIZ-0929-15：任务中心的"执行中"不能是死文案。
// 后端补了读列表时就地过期，前端这一侧要保证：列表里还有 running/queued 时会自己再拉一次，
// 用户不必手动点刷新就能看到它变成终态；没有活动任务时不轮询（不持续打后端）。
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, waitFor } from "@testing-library/react";

const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    getTaskHistory: vi.fn(),
    getTaskObservability: vi.fn(),
  },
}));

// 路径深度必须是 ../../（本文件在 src/components/__tests__/ 下），
// 写错时 vi.mock 静默失效，症状是"mock 从未被调用"。
vi.mock("../../api/client", () => ({ api: mockApi }));
vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  enumLabel: (_group: string, value: string) => value,
  getLocale: () => "zh-CN",
}));
vi.mock("../ai/ExplainButton", () => ({ default: () => null }));
vi.mock("../../context/AppContext", () => ({
  useApp: () => ({ showToast: vi.fn() }),
}));

import TaskCenter from "../TaskCenter";

const task = (status: string) => ({
  id: `t-${status}`,
  source: "async",
  task_type: "market_data_sync",
  status,
  stage: status,
  percent: 10,
  message: "",
  payload: {},
  result: {},
  errors: [],
  created_at: "2026-09-30T10:00:00",
  updated_at: "2026-09-30T10:00:00",
});

const seed = (statuses: string[]) => {
  mockApi.getTaskHistory.mockResolvedValue({ tasks: statuses.map(task) });
  mockApi.getTaskObservability.mockResolvedValue({
    domains: {},
    waiting_reasons: [],
    throughput: {},
  });
};

beforeEach(() => {
  vi.clearAllMocks();
});

// 断言目标是"有没有挂 10 秒轮询"，而不是首屏发了几次请求。
// 首屏次数在 StrictMode 双挂载下本身不稳定（1 次或 2 次都可能），拿它当基线会偶发翻车；
// 而"僵尸执行中"的真正病灶就是列表页从不自己再拉一次，所以直接盯 setInterval 的注册。
// 不做通用 helper：setInterval 的重载元组 arity 一旦塞进自定义类型就要跟 TS 打太极。
describe("TaskCenter 活动任务轮询", () => {
  it("列表存在 running 时挂 10s 轮询，且轮询是静默的（不闪整表 loading）", async () => {
    const handlers: Array<() => void> = [];
    vi.spyOn(window, "setInterval").mockImplementation(((handler: TimerHandler, delay?: number) => {
      if (delay === 10_000 && typeof handler === "function") handlers.push(handler as () => void);
      return 1;
    }) as unknown as typeof window.setInterval);
    seed(["running"]);
    const { container } = render(<TaskCenter />);
    // 轮询 effect 依赖 tasks 状态，必须等状态落定后再断言注册情况
    await waitFor(() => expect(handlers.length).toBeGreaterThan(0));

    const before = mockApi.getTaskHistory.mock.calls.length;
    await act(async () => { handlers[0](); });
    expect(mockApi.getTaskHistory.mock.calls.length).toBeGreaterThan(before);
    expect(container.querySelector(".ant-spin-spinning")).toBeNull();
    vi.restoreAllMocks();
  });

  it("列表只有终态任务时不挂轮询（不持续打后端）", async () => {
    const spy = vi.spyOn(window, "setInterval");
    seed(["done", "failed"]);
    render(<TaskCenter />);
    await waitFor(() => expect(mockApi.getTaskHistory.mock.calls.length).toBeGreaterThan(0));
    // 给状态落定一个额外回合，避免"还没来得及注册"被误判成"没有注册"
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    expect(spy.mock.calls.some(([, delay]) => delay === 10_000)).toBe(false);
    vi.restoreAllMocks();
  });
});
