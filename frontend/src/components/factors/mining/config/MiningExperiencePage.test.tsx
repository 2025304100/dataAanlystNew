import { describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@testing-library/react";

/**
 * T37 F1 列表页测试（向导 §6.9.7 / §8.4，任务卡 pitfalls）。
 *
 * 核心约束：**D 级默认不进批量操作选择集**（全选也排除 D 级）。
 */
import MiningExperiencePage from "./MiningExperiencePage";
import type { MiningExperienceRow } from "./MiningExperiencePage";

const ROWS: MiningExperienceRow[] = [
  { candidate_id: "c1", formula: "ts_mean(close,5)", grade: "S", icir_adjusted: 0.55, decay_ratio: 0.9 },
  { candidate_id: "c2", formula: "cs_rank(pe_ttm)", grade: "B", icir_adjusted: 0.21, decay_ratio: 0.7 },
  { candidate_id: "c3", formula: "ts_std(volume,20)", grade: "D", icir_adjusted: 0.05, decay_ratio: 0.3,
    grade_changed_recently: true },
];

describe("MiningExperiencePage", () => {
  it("等级 chip 显示各级数量，点击可筛选", () => {
    render(<MiningExperiencePage rows={ROWS} />);
    expect(document.querySelectorAll("[data-exp-grade-chip]").length).toBe(6);
    expect(document.querySelector('[data-exp-grade-chip="D"]')?.textContent).toContain("1");
    fireEvent.click(document.querySelector('[data-exp-grade-chip="S"]') as HTMLElement);
    const rows = document.querySelector("[data-exp-rows]");
    expect(rows?.textContent).toContain("ts_mean(close,5)");
    expect(rows?.textContent).not.toContain("cs_rank(pe_ttm)");
  });

  it("点击公式打开证据抽屉回调", () => {
    const onOpen = vi.fn();
    render(<MiningExperiencePage rows={ROWS} onOpenEvidence={onOpen} />);
    fireEvent.click(document.querySelector('[data-exp-open-evidence="c2"]') as HTMLElement);
    expect(onOpen).toHaveBeenCalledWith("c2");
  });

  it("**全选不含 D 级**（pitfalls：D 级默认不进选择集）", () => {
    render(<MiningExperiencePage rows={ROWS} />);
    fireEvent.click(document.querySelector("[data-exp-select-all-non-d]") as HTMLElement);
    expect(document.querySelector("[data-exp-selected-count]")?.textContent).toContain("2");
    expect(
      (document.querySelector('[data-exp-checkbox="c3"]') as HTMLInputElement).checked,
    ).toBe(false);
    expect(
      (document.querySelector('[data-exp-checkbox="c1"]') as HTMLInputElement).checked,
    ).toBe(true);
  });

  it("D 级可人工单独勾选（未被硬禁）", () => {
    render(<MiningExperiencePage rows={ROWS} />);
    fireEvent.click(document.querySelector('[data-exp-checkbox="c3"]') as HTMLElement);
    expect(document.querySelector("[data-exp-selected-count]")?.textContent).toContain("1");
  });

  it("等级变更 7 天内显示「新」角标", () => {
    render(<MiningExperiencePage rows={ROWS} />);
    const badge = document.querySelector('[data-exp-grade="c3"] [data-exp-new-badge]');
    expect(badge).toBeTruthy();
    expect(document.querySelector('[data-exp-grade="c1"] [data-exp-new-badge]')).toBeNull();
  });
});
