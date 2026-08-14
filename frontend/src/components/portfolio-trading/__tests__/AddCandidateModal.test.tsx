import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import AddCandidateModal from "../AddCandidateModal";

const { mockApi, mockRequestJson, mockShowToast } = vi.hoisted(() => ({
  mockApi: {
    getDiscoveryCandidatePools: vi.fn(),
    getPortfolioCandidates: vi.fn(),
  },
  mockRequestJson: vi.fn(),
  mockShowToast: vi.fn(),
}));

vi.mock("../../../api/client", () => ({
  api: mockApi,
  requestJson: mockRequestJson,
}));

vi.mock("../../../context/AppContext", () => ({
  useApp: () => ({ showToast: mockShowToast }),
}));

vi.mock("../../../i18n", () => ({
  t: (key: string) => key,
}));

const candidate = (symbolId: number, symbol: string, name: string) => ({
  symbol_id: symbolId,
  candidate_id: symbolId + 100,
  scan_run_id: 9,
  symbol,
  name,
  region: "cn",
  asset_type: "stock",
  market: "cn",
  quality_score: 80,
  timing_score: 75,
  priority_score: 82,
  stage: "accel",
  action: "buy",
  recommended_position_pct: 0.1,
  pool_memberships: ["factor", "technical"],
  factor: { preset_name: "quality-momentum" },
  reason_tags: ["趋势加速"],
});

describe("AddCandidateModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.getDiscoveryCandidatePools.mockResolvedValue({
      counts: { all: 2, factor: 2, technical: 2, theme: 0 },
      items: [
      candidate(1, "600519", "贵州茅台"),
      candidate(2, "000001", "平安银行"),
      candidate(1, "600519", "贵州茅台"),
    ] });
    mockApi.getPortfolioCandidates.mockResolvedValue([{ symbol_id: 2 }]);
    mockRequestJson.mockResolvedValue({ id: 99 });
  });

  it("loads the shared source list and excludes candidates already in this portfolio", async () => {
    render(
      <AddCandidateModal
        open
        onClose={vi.fn()}
        portfolioId={7}
      />,
    );

    expect(await screen.findByText("贵州茅台")).toBeInTheDocument();
    expect(screen.queryByText("平安银行")).not.toBeInTheDocument();
    expect(screen.getAllByText("贵州茅台")).toHaveLength(1);
    expect(mockApi.getDiscoveryCandidatePools).toHaveBeenCalledWith("all", 0, 200);
    expect(mockApi.getPortfolioCandidates).toHaveBeenCalledWith(7);
  });

  it("adds a selected candidate and notifies the parent to refresh", async () => {
    const onClose = vi.fn();
    const onSuccess = vi.fn();
    render(
      <AddCandidateModal
        open
        onClose={onClose}
        onSuccess={onSuccess}
        portfolioId={7}
      />,
    );

    fireEvent.click(await screen.findByText("贵州茅台"));
    fireEvent.click(screen.getByRole("button", { name: "确认添加" }));

    await waitFor(() => expect(mockRequestJson).toHaveBeenCalledTimes(1));
    expect(mockRequestJson).toHaveBeenCalledWith(
      "/api/v1/portfolios/7/candidates",
      expect.objectContaining({ method: "POST" }),
    );
    const request = mockRequestJson.mock.calls[0][1];
    expect(JSON.parse(request.body)).toMatchObject({
      symbol_id: 1,
      source_candidate_id: 101,
      source_type: "factor",
      pool_memberships: ["factor", "technical"],
      priority_score: 82,
    });
    expect(onSuccess).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
