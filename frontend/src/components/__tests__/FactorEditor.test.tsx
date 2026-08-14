import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

const { mockApi, mockMessage } = vi.hoisted(() => ({
  mockApi: {
    getFactorDefinition: vi.fn(),
    listFactorVersions: vi.fn(),
  },
  mockMessage: { error: vi.fn(), success: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return { ...actual, App: { ...actual.App, useApp: () => ({ message: mockMessage }) } };
});

vi.mock("../../api/client", () => ({ api: mockApi }));
vi.mock("../../context/AppContext", () => ({ useApp: () => ({ locale: "zh-CN" }) }));
vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  factorLabel: (_code: string, value?: string) => value || "-",
  factorCategoryLabel: (value: string) => value,
  factorDirectionLabel: (value: string) => value,
}));
vi.mock("../factors/FactorFormulaEditorModal", () => ({ default: () => null }));
vi.mock("../AiChatDrawer", () => ({ default: () => null }));

import FactorEditor from "../factors/FactorEditor";

describe("FactorEditor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.getFactorDefinition.mockResolvedValue({
      id: 9,
      code: "ep_demo",
      name: "资金跟庄",
      direction: "higher_better",
      formula_expr: null,
      default_missing_policy: "exclude",
      lifecycle_status: "draft",
    });
    mockApi.listFactorVersions.mockResolvedValue([{
      factor_code: "ep_demo",
      version: 1,
      formula_expr: "sum(main_net_inflow, 20) / max(sum(amount, 20), 1)",
      params: {},
      direction: "higher_better",
      change_note: "验证",
      is_latest: true,
    }]);
  });

  it("restores the latest immutable version formula when definition formula is empty", async () => {
    render(<FactorEditor factorCode="ep_demo" onSaved={vi.fn()} onBack={vi.fn()} />);

    await waitFor(() => expect(mockApi.listFactorVersions).toHaveBeenCalledWith("ep_demo"));
    expect(await screen.findByText("sum(main_net_inflow, 20) / max(sum(amount, 20), 1)")).toBeInTheDocument();
  });
});
