import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
const { api, toast } = vi.hoisted(() => ({ api: { getActiveAlerts: vi.fn(), getAlertRules: vi.fn(), updateAlertRule: vi.fn(), getScheduledTasks: vi.fn() }, toast: vi.fn() }));
vi.mock("../../api/client", () => ({ api }));
vi.mock("../../context/AppContext", () => ({ useApp: () => ({ showToast: toast }) }));
vi.mock("../../i18n", () => ({ t: (key: string) => key, template: (key: string) => key }));
import AlertCenter from "../AlertCenter";

beforeEach(() => {
  localStorage.clear();
  api.getActiveAlerts.mockResolvedValue({ events: [] });
  api.getAlertRules.mockResolvedValue([{ id: 4, name: "legacy unavailable alert", alert_type: "indicator_trigger", severity: "warn", enabled: 1, cooldown_minutes: 60 }]);
  api.getScheduledTasks.mockResolvedValue([]);
});
afterEach(cleanup);

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
