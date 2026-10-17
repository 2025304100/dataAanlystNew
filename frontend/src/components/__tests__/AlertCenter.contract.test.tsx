import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
const { api, toast } = vi.hoisted(() => ({ api: { getActiveAlerts: vi.fn(), getAlertRules: vi.fn(), updateAlertRule: vi.fn() }, toast: vi.fn() }));
vi.mock("../../api/client", () => ({ api }));
vi.mock("../../context/AppContext", () => ({ useApp: () => ({ showToast: toast }) }));
vi.mock("../../i18n", () => ({ t: (key: string) => key, template: (key: string) => key }));
import AlertCenter from "../AlertCenter";

beforeEach(() => {
  localStorage.clear();
  api.getActiveAlerts.mockResolvedValue({ events: [] });
  api.getAlertRules.mockResolvedValue([{ id: 4, name: "legacy unavailable alert", alert_type: "indicator_trigger", severity: "warn", enabled: 1, cooldown_minutes: 60 }]);
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
