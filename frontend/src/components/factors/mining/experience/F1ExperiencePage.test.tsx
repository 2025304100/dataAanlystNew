import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, waitFor } from "@testing-library/react";

/**
 * B3：F1 经验库页（列表渲染 + 归档）。
 */
vi.mock("../../../../api/factorExperience", () => ({
  factorExperienceApi: {
    list: vi.fn(async () => ({
      items: [
        { experience_id: "exp-1", formula_template: "mean(close,{n1})",
          category: "trend", source: "ai_generated", avg_icir: 0.31,
          status: "normal", is_negative_sample: 0 },
        { experience_id: "exp-2", formula_template: "rank(close,{n1})",
          category: "reversal", source: "enumerated", avg_icir: null,
          status: "normal", is_negative_sample: 1 },
      ],
      total: 2, page: 1, page_size: 50,
    })),
    get: vi.fn(),
    archive: vi.fn(async (id: string) => ({ experience_id: id, status: "archived" })),
  },
}));

import { factorExperienceApi } from "../../../../api/factorExperience";
import F1ExperiencePage from "./F1ExperiencePage";

describe("F1ExperiencePage（B3）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("激活时拉取列表并渲染公式模板/负样本标记", async () => {
    render(<F1ExperiencePage active />);
    await waitFor(() => {
      expect(document.querySelector("[data-f1-exp-table]")).toBeTruthy();
    });
    const rows = document.querySelectorAll("[data-f1-exp-table] tbody tr");
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain("mean(close,{n1})");
    expect(rows[0].textContent).toContain("trend");
    expect(document.querySelectorAll("[data-f1-exp-negative]").length).toBe(1);
  });

  it("未激活不拉取（与批次列表同款门控）", async () => {
    render(<F1ExperiencePage active={false} />);
    expect(factorExperienceApi.list).not.toHaveBeenCalled();
  });

  it("点击归档 → 调 archive 并刷新列表", async () => {
    render(<F1ExperiencePage active />);
    await waitFor(() => {
      expect(document.querySelector("[data-f1-exp-archive='exp-1']")).toBeTruthy();
    });
    fireEvent.click(document.querySelector("[data-f1-exp-archive='exp-1']") as HTMLElement);
    await waitFor(() => {
      expect(factorExperienceApi.archive).toHaveBeenCalledWith("exp-1");
    });
    // 归档后刷新（list 再次被调用）
    await waitFor(() => {
      expect(vi.mocked(factorExperienceApi.list).mock.calls.length)
        .toBeGreaterThanOrEqual(2);
    });
  });
});