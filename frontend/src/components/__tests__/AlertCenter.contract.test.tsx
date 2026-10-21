import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
const { api, toast } = vi.hoisted(() => ({ api: { getActiveAlerts: vi.fn(), getAlertRules: vi.fn(), updateAlertRule: vi.fn(), getScheduledTasks: vi.fn(), evaluateAlerts: vi.fn() }, toast: vi.fn() }));
vi.mock("../../api/client", () => ({ api }));
vi.mock("../../context/AppContext", () => ({ useApp: () => ({ showToast: toast }) }));
vi.mock("../../i18n", () => ({ t: (key: string) => key, template: (key: string) => key }));
import AlertCenter from "../AlertCenter";

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  api.getActiveAlerts.mockResolvedValue({ events: [] });
  api.getAlertRules.mockResolvedValue([{ id: 4, name: "legacy unavailable alert", alert_type: "indicator_trigger", severity: "warn", enabled: 1, cooldown_minutes: 60 }]);
  api.getScheduledTasks.mockResolvedValue([]);
});
afterEach(cleanup);

it("does not call unread alerts empty and retries the failed read in place", async () => {
  const user = userEvent.setup();
  api.getActiveAlerts.mockRejectedValueOnce(new Error("network down"));
  render(<AlertCenter />);
  expect(await screen.findByText("暂时无法读取告警，不能确认当前是否有风险。请重试。")).toBeVisible();
  expect(screen.queryByText("alertNoEvents")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "重新读取告警" }));
  expect(await screen.findByText("alertNoEvents")).toBeVisible();
  expect(screen.queryByText("暂时无法读取告警，不能确认当前是否有风险。请重试。")).not.toBeInTheDocument();
});

it("preserves previous alerts when a refresh fails and distinguishes a completed evaluation from unread results", async () => {
  const user = userEvent.setup();
  api.getActiveAlerts.mockResolvedValueOnce({ events: [{ id: 2, title: "行情已过期", message: "上次结果", alert_type: "data_stale", severity: "warn", resolved: false }] });
  api.evaluateAlerts.mockResolvedValue({ new_events: 1 });
  render(<AlertCenter />);
  expect(await screen.findByText("行情已过期")).toBeVisible();
  api.getActiveAlerts.mockRejectedValueOnce(new Error("network down"));
  await user.click(screen.getByRole("button", { name: /alertEvaluate/ }));
  expect(await screen.findByText("下面保留上次读取的告警，尚未更新。")).toBeVisible();
  expect(screen.getByText("行情已过期")).toBeVisible();
  expect(toast).toHaveBeenCalledWith("info", "评估已完成，但暂时读不到结果。请重新读取告警。");
  expect(toast).not.toHaveBeenCalledWith("success", expect.anything());
  await user.click(screen.getByRole("button", { name: "重新读取告警" }));
  expect(await screen.findByText("alertNoEvents")).toBeVisible();
});

it("shows a separate rules read failure and lets the user recover without changing the rules", async () => {
  const user = userEvent.setup();
  api.getAlertRules.mockRejectedValueOnce(new Error("rules unavailable"));
  render(<AlertCenter />);
  await screen.findByText("alertNoEvents");
  await user.click(screen.getByText("alertRules"));
  expect(await screen.findByText("暂时无法读取告警规则，请重试。")).toBeVisible();
  expect(screen.queryByText("alertNoRules")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "重新读取规则" }));
  expect(await screen.findByText("legacy unavailable alert")).toBeVisible();
  expect(api.updateAlertRule).not.toHaveBeenCalled();
  await user.click(screen.getByText("alertEvents"));
  expect(screen.getByText("alertNoEvents")).toBeVisible();
});

it("keeps an unavailable saved alert visible with its reason and allows only disabling", async () => {
  const user = userEvent.setup();
  render(<AlertCenter />);
  await user.click(screen.getByText("alertRules"));
  expect(await screen.findByText("legacy unavailable alert")).toBeVisible();
  expect(screen.getByText("settingsUnsupportedReason")).toBeVisible();
  expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  api.updateAlertRule.mockResolvedValue({});
  await user.click(screen.getByRole("button", { name: "disable" }));
  expect(api.updateAlertRule).toHaveBeenCalledWith(4, { enabled: false });
});

const _schedule = (overrides: Record<string, unknown>) => ({
  id: 15,
  name: "每日告警规则评估",
  task_type: "alert_evaluation",
  frequency: "daily",
  time_of_day: "21:30",
  weekdays: [],
  interval_minutes: null,
  timezone: "Asia/Shanghai",
  payload: {},
  enabled: true,
  next_run_at: null,
  last_run_at: null,
  last_status: null,
  last_task_id: null,
  last_task_status: null,
  last_message: null,
  last_error: null,
  created_at: "",
  updated_at: "",
  ...overrides,
});

it("claims scheduled evaluation only when the alert evaluation schedule is enabled", async () => {
  api.getScheduledTasks.mockResolvedValue([_schedule({ enabled: true })]);
  render(<AlertCenter />);
  expect(await screen.findByText("settingsAlertAutoMode")).toBeVisible();
  expect(screen.queryByText("settingsAlertScheduleOffMode")).not.toBeInTheDocument();
});

it("says evaluation is off when the schedule exists but is disabled", async () => {
  api.getScheduledTasks.mockResolvedValue([_schedule({ enabled: false })]);
  render(<AlertCenter />);
  expect(await screen.findByText("settingsAlertScheduleOffMode")).toBeVisible();
});

it("says evaluation is off when no alert evaluation schedule exists", async () => {
  api.getScheduledTasks.mockResolvedValue([_schedule({ task_type: "factor_pipeline" })]);
  render(<AlertCenter />);
  expect(await screen.findByText("settingsAlertScheduleOffMode")).toBeVisible();
});

it("claims nothing while the schedule list cannot be read", async () => {
  const user = userEvent.setup();
  api.getScheduledTasks.mockRejectedValue(new Error("network down"));
  render(<AlertCenter />);
  await user.click(screen.getByText("alertRules"));
  // 规则列表已加载完 = 同一批 mount 请求都已 settle，此时仍不能出现任何模式断言
  await screen.findByText("legacy unavailable alert");
  // i18n 在本测试里按 key 原样回显，所以任何"模式断言"文案都不该出现
  expect(screen.queryByText(/^settingsAlert/)).not.toBeInTheDocument();
});
