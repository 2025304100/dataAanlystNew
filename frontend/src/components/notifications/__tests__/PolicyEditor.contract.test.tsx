import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("../../../api/client", () => ({ requestJson: request }));
vi.mock("../../../i18n", () => ({ t: (key: string) => key }));
import { PolicyEditor } from "../PolicyEditor";

beforeEach(() => {
  localStorage.clear();
  request.mockReset();
  request.mockImplementation(async (url: string) => {
    if (url.endsWith("capabilities")) return { sources: { system: { supported: true, events: [{ type: "score_drop", supported: true }, { type: "task_complete", supported: false }] } }, delivery_modes: ["instant"], scope_types: ["all", "portfolio", "symbol"] };
    return [];
  });
});
afterEach(cleanup);

it("offers actual alert events and hides unconnected source and delivery options by default", async () => {
  const user = userEvent.setup();
  render(<PolicyEditor />);
  await user.click(await screen.findByText("addPolicy"));
  fireEvent.mouseDown(screen.getByLabelText("policyEditorSourceTypes").closest(".ant-select")!.querySelector(".ant-select-selector")!);
  await waitFor(() => expect(screen.getByText(/alertTypeScoreDrop/, { selector: ".ant-select-item-option-content" })).toBeVisible());
  expect(screen.queryByText(/sourceEventTaskComplete/, { selector: ".ant-select-item-option-content" })).not.toBeInTheDocument();
  await user.keyboard("{Escape}");
  fireEvent.mouseDown(screen.getByLabelText("policyDeliveryMode").closest(".ant-select")!.querySelector(".ant-select-selector")!);
  expect(screen.queryByText("policyDeliveryDigest", { selector: ".ant-select-item-option-content" })).not.toBeInTheDocument();
});

it("preserves the existing weekday-only quiet setting when renaming a policy", async () => {
  const policy = { id: 1, name: "weekday rule", enabled: false, source_types_json: '["score_drop"]', min_severity: "warn", scope_type: "all", scope_ids_json: null, delivery_mode: "instant", channel_ids: [2], cooldown_minutes: 0, dedup_window_minutes: 0, template_id: null, quiet_hours_json: '{"start":"22:00","end":"08:00","timezone":"Asia/Shanghai","trading_days_only":true}' };
  const fallback = request.getMockImplementation()!;
  request.mockImplementation(async (url: string, options?: {method?: string}) => {
    if (options?.method === "PATCH") return policy;
    if (url.endsWith("policies")) return [policy];
    if (url.endsWith("channels")) return [{id:2, name:"inbox",channel_type:"in_app",enabled:true,status:"test_success"}];
    return fallback(url, options);
  });
  const user = userEvent.setup();
  render(<PolicyEditor />);
  await user.click(await screen.findByText("edit", {selector:"button span"}));
  await user.clear(screen.getByLabelText("policyColumnName"));
  await user.type(screen.getByLabelText("policyColumnName"), "renamed");
  await user.click(screen.getByText("save", {selector:"button span"}));
  await waitFor(() => {
    const call = request.mock.calls.find(([, options]) => options?.method === "PATCH");
    expect(call).toBeTruthy();
    expect(JSON.parse(call![1].body).quiet_hours.trading_days_only).toBe(true);
  });
});
