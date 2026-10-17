import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
const { api } = vi.hoisted(() => ({ api: {
  scoringListFactorDefinitions: vi.fn(), listFactorVersions: vi.fn(), getShadowCapabilities: vi.fn(),
  listShadowObservations: vi.fn(), getShadowObservationSummary: vi.fn(), getShadowHealth: vi.fn(),
} }));
vi.mock("../../api/client", () => ({ api }));
vi.mock("../../i18n", () => ({ t: (key: string) => key, factorLabel: (code: string, name?: string) => name || code }));
vi.mock("echarts-for-react", () => ({ default: () => <div data-testid="mock-chart" /> }));
import FactorShadowLab from "../factors/FactorShadowLab";

const observation = { id: 1, trade_date: "2026-08-01", is_valid_day: true, ic_value: 0.05,
  coverage: 0.92, turnover: 0.35, completeness_ratio: 0.96, health_status: "healthy" };
const summary = { factor_version_id: 1, valid_days: 25, min_required_days: 20, is_complete: true, reason: null };
const health = { factor_version_id: 1, health_status: "healthy", alerts: [], n_valid_days: 25, n_total_days: 25, should_quarantine: false };
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  api.scoringListFactorDefinitions.mockResolvedValue({ items: [{ id: 1, code: "momentum", name: "动量" }], total: 1 });
  api.listFactorVersions.mockResolvedValue([{ id: 1, factor_id: 1, version: 1, is_latest: true }]);
  api.getShadowCapabilities.mockResolvedValue({ mode: "historical_read_only" });
  api.listShadowObservations.mockResolvedValue([observation]);
  api.getShadowObservationSummary.mockResolvedValue(summary);
  api.getShadowHealth.mockResolvedValue(health);
});

function selected() { return render(<FactorShadowLab initialFactorCode="momentum" initialVersionId={1} />); }

describe("FactorShadowLab history", () => {
  it("prompts for a factor and version without fetching unselected observations", () => {
    render(<FactorShadowLab />);
    expect(screen.getByText("shadowLabInputHint")).toBeVisible();
    expect(api.listShadowObservations).not.toHaveBeenCalled();
  });
  it("shows actual observation metrics, completed day target, and chart", async () => {
    selected();
    expect(await screen.findByText("2026-08-01")).toBeVisible();
    expect(screen.getByText("factorLabObservationComplete")).toBeVisible();
    expect(screen.getByText("92.00%")).toBeVisible();
    expect(screen.getByText("0.0500")).toBeVisible();
    expect(screen.getByTestId("mock-chart")).toBeVisible();
  });
  it("keeps incomplete observation progress static and explains the missing days", async () => {
    api.getShadowObservationSummary.mockResolvedValue({ ...summary, is_complete: false, valid_days: 10, reason: "需要 20 个有效交易日" });
    selected();
    expect(await screen.findByText("shadowLabGateIncomplete")).toBeVisible();
    expect(screen.getByText("需要 20 个有效交易日")).toBeVisible();
    expect(document.querySelector(".ant-progress-status-active")).not.toBeInTheDocument();
  });
  it("shows historical diagnostic alerts and an honest risk follow-up", async () => {
    api.getShadowHealth.mockResolvedValue({ ...health, health_status: "blocked", should_quarantine: true,
      alerts: [{ severity: "critical", message: "因子常数化" }] });
    selected();
    expect(await screen.findByText("因子常数化")).toBeVisible();
    expect(screen.getByText("factorLabShadowRiskHint")).toBeVisible();
  });
  it("shows a recoverable loading error without false zero-day results", async () => {
    api.listShadowObservations.mockRejectedValueOnce(new Error("network error"));
    selected();
    expect(await screen.findByText("network error")).toBeVisible();
    expect(screen.queryByText("shadowLabObservationSummary")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "factorLabRetryCheck" }));
    expect(await screen.findByText("2026-08-01")).toBeVisible();
  });
  it("reveals unsupported approval actions only as disabled explanations", async () => {
    selected();
    expect(await screen.findByText("2026-08-01")).toBeVisible();
    expect(screen.queryByText("shadowLabApprove")).not.toBeInTheDocument();
    await act(async () => { localStorage.setItem("settings.show_unsupported", "true"); window.dispatchEvent(new Event("settings:unsupported-visibility")); });
    for (const name of ["shadowLabRequestActivation", "shadowLabApprove", "shadowLabReject", "shadowLabQuarantine"]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
    expect(screen.getByText("factorLabShadowWritesUnavailable")).toBeVisible();
  });
  it("rejects a preselected version belonging to another factor", async () => {
    render(<FactorShadowLab initialFactorCode="momentum" initialVersionId={999} />);
    expect(await screen.findByText("factorLabVersionMismatch")).toBeVisible();
    expect(api.listShadowObservations).not.toHaveBeenCalled();
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "factorLabVersion" }));
    fireEvent.click(await screen.findByText("v1 · factorValidation_pending"));
    expect(await screen.findByText("2026-08-01")).toBeVisible();
    expect(screen.queryByText("factorLabVersionMismatch")).not.toBeInTheDocument();
  });
  it("keeps zero-valued metrics and reports a failed capability read conservatively", async () => {
    api.getShadowCapabilities.mockRejectedValue(new Error("unavailable"));
    api.listShadowObservations.mockResolvedValue([{ ...observation, ic_value: 0, coverage: 0 }]);
    selected();
    expect(await screen.findByText("0.0000")).toBeVisible();
    expect(screen.getByText("0.00%")).toBeVisible();
    expect(screen.getByText("factorLabCapabilityFailed")).toBeVisible();
    expect(screen.queryByRole("button", { name: "shadowLabApprove" })).not.toBeInTheDocument();
  });
});
