import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

const { mockApi, mockMessage } = vi.hoisted(() => ({
  mockApi: { listFactorDefinitions: vi.fn() },
  mockMessage: { error: vi.fn(), success: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return {
    ...actual,
    App: { ...actual.App, useApp: () => ({ message: mockMessage }) },
  };
});

vi.mock("../../context/AppContext", () => ({
  useApp: () => ({ locale: "zh-CN" }),
}));

vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, values: Record<string, unknown>) => `${key}:${Object.values(values).join(":")}`,
  factorLabel: (_code: string, name?: string) => name || "-",
  factorCategoryLabel: (value: string) => value,
  factorDirectionLabel: (value: string) => value,
}));

vi.mock("../../api/client", () => ({ api: mockApi }));

import FactorLibrary from "../factors/FactorLibrary";

const factors = [
  { id: 1, code: "draft", name: "Draft", lifecycle_status: "draft", updated_at: null },
  { id: 2, code: "candidate", name: "Candidate", lifecycle_status: "candidate", updated_at: null },
  { id: 3, code: "shadow", name: "Shadow", lifecycle_status: "shadow", updated_at: null },
  { id: 4, code: "active", name: "Active", lifecycle_status: "active", updated_at: null },
];

describe("FactorLibrary", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.listFactorDefinitions.mockImplementation((params: { page?: number; page_size?: number }) => {
      if ((params.page_size ?? 20) > 100) {
        return Promise.reject(new Error("page_size must be <= 100"));
      }
      return Promise.resolve({ items: factors, total: factors.length, page: params.page ?? 1, page_size: params.page_size ?? 20 });
    });
  });

  it("loads factor statistics within the API page-size contract", async () => {
    render(<FactorLibrary onViewDetail={vi.fn()} onEditFactor={vi.fn()} onNewFactor={vi.fn()} />);

    await waitFor(() => {
      expect(mockApi.listFactorDefinitions).toHaveBeenCalledWith(expect.objectContaining({ page_size: 100 }));
    });

    expect(await screen.findByText("4")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getAllByText("1").length).toBeGreaterThanOrEqual(2);
  });
});
