import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const { backend } = vi.hoisted(() => ({ backend: {
  listAkshareApis: vi.fn(), listAkshareStrategies: vi.fn(), submitApiProbe: vi.fn(), getApiProbeTask: vi.fn(),
  updateAkshareApiConfig: vi.fn(), publishApiPolicy: vi.fn(), getApiConfigurationHistory: vi.fn(),
} }));
vi.mock("../../api/client", () => ({ api: backend }));
vi.mock("../../i18n", () => ({ t: (k: string) => k, template: (k: string) => k, getLocale: () => "zh-CN" }));
import AkshareApiManager from "../AkshareApiManager";

const policy = { enabled: true, min_interval_s: .5, max_attempts: 2, timeout_s: 10, failure_threshold: 3,
  cooldown_s: 60, max_cooldown_s: 900, probe_lease_s: 30, window_s: 60,
  total_limit: null, incremental_limit: null, backfill_limit: null, probe_limit: null };
const base = { category: "行情", module: "sdk", description: "test", default_strategy: "standard", enabled: true,
  anti_risk_strategy: "shared", delay_min_ms: 500, delay_max_ms: 500, last_probe_at: null, last_probe_success: true,
  last_probe_latency_ms: null, last_probe_error: null, last_call_at: null, last_call_success: null, last_call_error: null,
  total_calls: 2, total_failures: 0, data_contracts: [] };
const sharedRow = { ...base, key: "stock_zh_a_hist", name: "东财日线", governance: {
  access_status: "available_now", in_current_release: true, implementation_status: "implemented", auth_required: false,
  auth_status: "not_required", admission_status: "not_reviewed", production_request_allowed: false,
  supplier_quota_status: "unknown", supplier_quota_remaining: null, network_metrics_applicable: true,
  contract_status: "unit_definition_unverified", profile_ids: ["daily_bars/core"], unit_fields: [], can_edit: true, can_probe: true,
  control_mode: "shared", config_version: { endpoint: 1, provider: 1 }, health_status: "observed_success",
  control: { provider_id: "eastmoney", endpoint_policy: policy, provider_policy: policy, transport: "requests" },
  statistics: { by_kind: { call: { logical_calls: 2, attempts: 3, retry_count: 1 }, probe: { logical_calls: 1 } }, latest: {} },
  legacy_statistics: { available: true, total_calls: 99, total_failures: 50 }, block_reasons: [],
} };
const plannedRow = { ...base, key: "PLAN:Fuyao", name: "同花顺 Fuyao REST", enabled: false, governance: {
  ...sharedRow.governance, access_status: "permission_unverified", in_current_release: false, implementation_status: "not_implemented",
  auth_required: true, auth_status: "unconfigured", can_edit: false, can_probe: false, control: null, config_version: null,
} };
beforeEach(() => {
  vi.clearAllMocks(); backend.listAkshareApis.mockResolvedValue([sharedRow, plannedRow]);
  backend.listAkshareStrategies.mockResolvedValue([{ key: "standard", name: "标准", max_retries: 3 }]);
  backend.getApiConfigurationHistory.mockResolvedValue([]);
  backend.submitApiProbe.mockResolvedValue({ task_id: "p1" });
  backend.getApiProbeTask.mockResolvedValue({ status: "done", result: { success: true, latency_ms: 1 } });
  backend.publishApiPolicy.mockResolvedValue(sharedRow);
});

describe("interface governance", () => {
  it("shows admission separately from successful connectivity and blocks planned actions", async () => {
    const { container } = render(<AkshareApiManager />);
    await screen.findByText("同花顺 Fuyao REST");
    expect(screen.getAllByText("未准入").length).toBeGreaterThan(0);
    expect(screen.getByText("无需账号")).toBeInTheDocument();
    expect(screen.getByText("凭据未配置")).toBeInTheDocument();
    const row = container.querySelector('tr[data-row-key="PLAN:Fuyao"]') as HTMLElement;
    expect(within(row).getByRole("switch")).toBeDisabled();
    expect(within(row).getByRole("button", { name: /apiMgmtProbe/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /apiMgmtProbeAll/ }));
    await waitFor(() => expect(backend.submitApiProbe).toHaveBeenCalledTimes(1));
    expect(backend.submitApiProbe).toHaveBeenCalledWith("stock_zh_a_hist");
  });

  it("publishes the endpoint switch with both expected versions", async () => {
    const { container } = render(<AkshareApiManager />);
    await screen.findByText("东财日线");
    const row = container.querySelector('tr[data-row-key="stock_zh_a_hist"]') as HTMLElement;
    fireEvent.click(within(row).getByRole("switch"));
    await waitFor(() => expect(backend.publishApiPolicy).toHaveBeenCalledWith("stock_zh_a_hist", {
      scope: "endpoint", expected_version: { endpoint: 1, provider: 1 }, policy: { enabled: false },
    }, "zh-CN"));
    expect(backend.updateAkshareApiConfig).not.toHaveBeenCalled();
    await waitFor(() => expect(backend.listAkshareApis).toHaveBeenCalledTimes(2));
  });

  it("shows profiles and separate legacy statistics and publishes provider limits", async () => {
    const { container } = render(<AkshareApiManager />);
    await screen.findByText("东财日线");
    fireEvent.click(container.querySelector('tr[data-row-key="stock_zh_a_hist"] .ant-table-row-expand-icon')!);
    expect(screen.getByText(/daily_bars\/core/)).toBeInTheDocument();
    expect(screen.getByText(/旧累计（单独保留，不与共享统计相加）/)).toHaveTextContent("99");
    const editor = screen.getByRole("region", { name: "来源共享策略" });
    fireEvent.change(within(editor).getByRole("spinbutton", { name: "来源共享策略 最大尝试次数" }), { target: { value: "4" } });
    fireEvent.click(within(editor).getByRole("button", { name: "发布新版本" }));
    await waitFor(() => expect(backend.publishApiPolicy).toHaveBeenCalledWith("stock_zh_a_hist", {
      scope: "provider", expected_version: { endpoint: 1, provider: 1 }, policy: { max_attempts: 4 },
    }, "zh-CN"));
  });

  it("refreshes after a version conflict and does not silently retry", async () => {
    backend.publishApiPolicy.mockRejectedValueOnce(new Error("CONFIG_VERSION_CONFLICT"));
    const { container } = render(<AkshareApiManager />);
    await screen.findByText("东财日线");
    fireEvent.click(within(container.querySelector('tr[data-row-key="stock_zh_a_hist"]') as HTMLElement).getByRole("switch"));
    await screen.findByText("配置已变更，已刷新当前版本，请重新检查后提交。");
    expect(backend.publishApiPolicy).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(backend.listAkshareApis).toHaveBeenCalledTimes(2));
  });

  it("restores a history version as a new publication", async () => {
    backend.getApiConfigurationHistory.mockResolvedValueOnce([{ version: 1, origin: "publish", changed_at: 1000,
      configuration: { policy, version: 1 } }]);
    const { container } = render(<AkshareApiManager />);
    await screen.findByText("东财日线");
    fireEvent.click(container.querySelector('tr[data-row-key="stock_zh_a_hist"] .ant-table-row-expand-icon')!);
    const editor = screen.getByRole("region", { name: "本接口策略" });
    fireEvent.click(within(editor).getByRole("button", { name: "查看版本历史" }));
    const combo = await within(editor).findByRole("combobox", { name: "本接口策略 历史版本" });
    fireEvent.mouseDown(combo);
    fireEvent.click(await screen.findByText("v1", { selector: ".ant-select-item-option-content" }));
    fireEvent.click(within(editor).getByRole("button", { name: "恢复为新版本" }));
    await waitFor(() => expect(backend.publishApiPolicy).toHaveBeenCalledWith("stock_zh_a_hist", {
      scope: "endpoint", expected_version: { endpoint: 1, provider: 1 }, restore_version: 1,
    }, "zh-CN"));
  });
});
