import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";

const { mockApi } = vi.hoisted(() => ({ mockApi: {
  scoringListFactorDefinitions: vi.fn(), listFactorVersions: vi.fn(),
  listShadowObservations: vi.fn(), getShadowObservationSummary: vi.fn(), getShadowHealth: vi.fn(),
  getShadowCapabilities: vi.fn(), requestActivation: vi.fn(), approveActivation: vi.fn(),
} }));
vi.mock("../../api/client", () => ({ api: mockApi }));
vi.mock("../../i18n", () => ({ t: (key: string) => key, factorLabel: (_code: string, name?: string) => name || _code }));
vi.mock("echarts-for-react", () => ({ default: () => <div data-testid="shadow-chart" /> }));
import FactorShadowLab from "../factors/FactorShadowLab";

const summary = (id: number) => ({ factor_version_id: id, valid_days: 25, min_required_days: 20, is_complete: true });
const health = { health_status: "healthy", alerts: [], n_total_days: 25, should_quarantine: false };
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mockApi.scoringListFactorDefinitions.mockResolvedValue({ items: [{ id: 7, code: "momentum", name: "动量" }], total: 1 });
  mockApi.listFactorVersions.mockResolvedValue([{ id: 11, factor_id: 7, version: 1, is_latest: true }, { id: 12, factor_id: 7, version: 2 }]);
  mockApi.getShadowCapabilities.mockResolvedValue({ mode: "historical_read_only", activation: { supported: false }, observation_collection: { supported: false } });
  mockApi.listShadowObservations.mockResolvedValue([]);
  mockApi.getShadowObservationSummary.mockImplementation((id: number) => Promise.resolve(summary(id)));
  mockApi.getShadowHealth.mockResolvedValue(health);
});

describe("Shadow historical observation guidance", () => {
  it("does not overwrite the parent's saved selection while version resolution is pending", async () => {
    let finish!: (rows: any[]) => void;
    const onSelectionChange = vi.fn();
    mockApi.listFactorVersions.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    render(<FactorShadowLab initialFactorCode="momentum" initialVersionId={12} onSelectionChange={onSelectionChange} />);
    await waitFor(() => expect(mockApi.listFactorVersions).toHaveBeenCalled());
    expect(onSelectionChange).not.toHaveBeenCalled();
    await act(async () => { finish([{ id: 11, version: 1, is_latest: true }, { id: 12, version: 2 }]); });
    expect(onSelectionChange).toHaveBeenCalledTimes(1);
    expect(onSelectionChange).toHaveBeenCalledWith({ factorCode: "momentum", versionId: 12 });
  });

  it("keeps a manually selected factor and real version after leaving and remounting the workspace", async () => {
    function Workspaces() {
      const [visible, setVisible] = useState(true);
      const [selection, setSelection] = useState<{ factorCode?: string; versionId?: number }>({});
      return <><button onClick={() => setVisible(value => !value)}>切换工作区</button>
        {visible ? <FactorShadowLab initialFactorCode={selection.factorCode} initialVersionId={selection.versionId}
          onSelectionChange={setSelection} /> : <div>其他工作区</div>}</>;
    }
    render(<Workspaces />);
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "evalWorkbenchFactor" }));
    fireEvent.click(await screen.findByText("动量 (momentum)"));
    await waitFor(() => expect(mockApi.listShadowObservations).toHaveBeenCalledWith(11, { limit: 200 }));
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "factorLabVersion" }));
    fireEvent.click(await screen.findByText("v2 · factorValidation_pending"));
    await waitFor(() => expect(mockApi.listShadowObservations).toHaveBeenCalledWith(12, { limit: 200 }));
    fireEvent.click(screen.getByRole("button", { name: "切换工作区" }));
    mockApi.listShadowObservations.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "切换工作区" }));
    await waitFor(() => expect(mockApi.listShadowObservations).toHaveBeenCalledWith(12, { limit: 200 }));
    expect(mockApi.listShadowObservations).not.toHaveBeenCalledWith(11, { limit: 200 });
    expect(mockApi.approveActivation).not.toHaveBeenCalled();
  });

  it("resolves a preselected factor and its actual version and defaults to read-only history", async () => {
    render(<FactorShadowLab initialFactorCode="momentum" initialVersionId={11} />);
    await waitFor(() => expect(mockApi.listShadowObservations).toHaveBeenCalledWith(11, { limit: 200 }));
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    expect(await screen.findByText("factorLabShadowReadOnly")).toBeVisible();
    expect(screen.queryByRole("button", { name: "shadowLabApprove" })).not.toBeInTheDocument();
    expect(mockApi.requestActivation).not.toHaveBeenCalled();
  });

  it("ignores an older version response after the user selects another version", async () => {
    let resolveOld!: (rows: any[]) => void;
    mockApi.listShadowObservations.mockImplementation((id: number) => id === 11
      ? new Promise(resolve => { resolveOld = resolve; })
      : Promise.resolve([{ id: 12, trade_date: "2026-10-12", is_valid_day: true, ic_value: 0.2, coverage: 1, health_status: "healthy" }]));
    render(<FactorShadowLab initialFactorCode="momentum" initialVersionId={11} />);
    await waitFor(() => expect(mockApi.listShadowObservations).toHaveBeenCalledWith(11, { limit: 200 }));
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "factorLabVersion" }));
    fireEvent.click(await screen.findByText("v2 · factorValidation_pending"));
    expect(await screen.findByText("2026-10-12")).toBeVisible();
    await act(async () => { resolveOld([{ id: 11, trade_date: "2026-09-01", is_valid_day: true }]); });
    expect(screen.queryByText("2026-09-01")).not.toBeInTheDocument();
    expect(screen.getByText("2026-10-12")).toBeVisible();
  });
});
