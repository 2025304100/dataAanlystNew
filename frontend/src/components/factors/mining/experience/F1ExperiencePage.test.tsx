import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
          use_count: 7, success_count: 3, success_rate: 0.4286,
          status: "normal", is_negative_sample: 0 },
        { experience_id: "exp-2", formula_template: "rank(close,{n1})",
          category: "reversal", source: "enumerated", avg_icir: null,
          use_count: 0, success_count: 0, success_rate: null,
          status: "normal", is_negative_sample: 1 },
        // 用过但全失败：use_count>0 且 rate=0 → 必须显示 0.0%，不能和"从未使用"混为一谈
        { experience_id: "exp-3", formula_template: "std(vol,{n1})",
          category: "volatility", source: "ai_generated", avg_icir: -0.05,
          use_count: 5, success_count: 0, success_rate: 0,
          status: "normal", is_negative_sample: 0 },
      ],
      total: 3, page: 1, page_size: 50,
    })),
    get: vi.fn(),
    archive: vi.fn(async (id: string) => ({ experience_id: id, status: "archived" })),
  },
}));

import { factorExperienceApi } from "../../../../api/factorExperience";
import F1ExperiencePage from "./F1ExperiencePage";
import { setLocale } from "../../../../i18n";

afterEach(() => setLocale("zh-CN"));

describe("F1ExperiencePage（B3）", () => {
  beforeEach(() => {
    setLocale("zh-CN");
    vi.clearAllMocks();
  });

  it("激活时拉取列表并渲染公式模板/负样本标记", async () => {
    render(<F1ExperiencePage active />);
    await waitFor(() => {
      expect(document.querySelector("[data-f1-exp-table]")).toBeTruthy();
    });
    const rows = document.querySelectorAll("[data-f1-exp-table] tbody tr");
    expect(rows.length).toBe(3);
    expect(rows[0].textContent).toContain("mean(close,{n1})");
    expect(rows[0].textContent).toContain("趋势");
    expect(document.querySelectorAll("[data-f1-exp-negative]").length).toBe(1);
  });

  it("切换语言后翻译类别、来源和状态，保留公式与未知枚举", async () => {
    const fixture = await factorExperienceApi.list();
    vi.mocked(factorExperienceApi.list).mockResolvedValueOnce({ ...fixture, items: [
      { ...fixture.items[0], source: "manual" },
      { ...fixture.items[1], category: "custom_category", source: "custom_source", status: "custom_status" },
    ] });
    const page = render(<F1ExperiencePage active />);
    await waitFor(() => expect(page.getByText("人工录入")).toBeTruthy());
    expect(page.getByText("趋势")).toBeTruthy();
    expect(page.getByText("正常")).toBeTruthy();
    setLocale("en-US");
    page.rerender(<F1ExperiencePage active />);
    expect(page.getByText("Manual entry")).toBeTruthy();
    expect(page.getByText("Trend")).toBeTruthy();
    expect(page.getByText("Normal")).toBeTruthy();
    expect(page.getByText("mean(close,{n1})")).toBeTruthy();
    expect(page.getByText("custom_category")).toBeTruthy();
    expect(page.getByText("custom_source")).toBeTruthy();
    expect(page.getByText("custom_status")).toBeTruthy();
    expect(factorExperienceApi.archive).not.toHaveBeenCalled();
  });

  it("渲染使用次数与成功率（含 0 与 null 的区分）", async () => {
    render(<F1ExperiencePage active />);
    await waitFor(() => {
      expect(document.querySelector("[data-f1-exp-table]")).toBeTruthy();
    });
    const rows = document.querySelectorAll("[data-f1-exp-table] tbody tr");
    const cell = (r: Element, sel: string) =>
      r.querySelector(sel)?.textContent?.trim();

    // exp-1：用过且成功率有值 → 百分比保留 1 位
    expect(cell(rows[0], "[data-f1-exp-use-count]")).toBe("7");
    expect(cell(rows[0], "[data-f1-exp-success-rate]")).toBe("42.9%");

    // exp-2：use_count=0 是真实值（不能显示成 "-"）；
    //        从未使用 → 成功率 0/0 无意义 → "-"（不能显示 0.0%）
    expect(cell(rows[1], "[data-f1-exp-use-count]")).toBe("0");
    expect(cell(rows[1], "[data-f1-exp-success-rate]")).toBe("-");

    // exp-3：用过但全失败 → 成功率 0.0% 是真实结论，必须显示出来
    expect(cell(rows[2], "[data-f1-exp-use-count]")).toBe("5");
    expect(cell(rows[2], "[data-f1-exp-success-rate]")).toBe("0.0%");
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

  it("归档失败显示原因，不把原数据误判为空", async () => {
    vi.mocked(factorExperienceApi.archive).mockRejectedValueOnce(new Error("服务繁忙，请重试"));
    const page = render(<F1ExperiencePage active />);
    await waitFor(() => expect(document.querySelector("[data-f1-exp-archive='exp-1']")).toBeTruthy());
    fireEvent.click(document.querySelector("[data-f1-exp-archive='exp-1']") as HTMLElement);
    await waitFor(() => expect(page.getByRole("alert").textContent).toContain("服务繁忙，请重试"));
    expect(document.querySelectorAll("[data-f1-exp-table] tbody tr")).toHaveLength(3);
  });

  it("经验超过50条可切换下一页，显示实际总数", async () => {
    const original = await factorExperienceApi.list();
    vi.mocked(factorExperienceApi.list).mockClear();
    vi.mocked(factorExperienceApi.list).mockResolvedValueOnce({ ...original, total: 51 });
    vi.mocked(factorExperienceApi.list).mockResolvedValueOnce({ ...original, items: [original.items[0]], total: 51, page: 2 });
    const page = render(<F1ExperiencePage active />);
    await waitFor(() => expect(page.getByText(/共 51 条/)).toBeTruthy());
    fireEvent.click(page.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(factorExperienceApi.list).toHaveBeenLastCalledWith({ page: 2, page_size: 50 }));
    await waitFor(() => expect(document.querySelectorAll("[data-f1-exp-table] tbody tr")).toHaveLength(1));
  });
});
