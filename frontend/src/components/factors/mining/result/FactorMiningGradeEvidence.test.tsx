import { describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@testing-library/react";

/**
 * T37 等级证据抽屉测试（向导 §8.4.1 / §8.4.2 / §8.4.3，任务卡 pitfalls / not_do）。
 *
 * 覆盖 artifacts「等级筛选 + 证据抽屉 3 Tab」与硬约束：
 *   1. 3 Tab（定级证据 / 统计检验证据 / 血缘与来源）且有且仅有 3 个
 *      （not_do：不做 4 Tab）；
 *   2. Tab1：等级大标签 + 一句话理由 + 8 维度明细（当前值/阈值/达标）+ 阈值来源；
 *   3. Tab2：8 项统计检验；**月频时 Bootstrap / 置换两行灰掉并标注样本不足**
 *      （pitfalls 第二条）+ 顶部「月频最高 B 级」提示；
 *   4. Tab3：进化路径 + 经济逻辑（AI 生成必显示）；
 *   5. 人工调整等级：原因必填且 ≥10 字；提交后提示「季度重评不会自动覆盖」
 *      + 「恢复自动」按钮；
 *   6. 季度重评横幅：升级绿 / 降级红 / 移出 FactorSet 追加提示；
 *   7. 评级历史时间轴（等级/时间/触发方式/原因）。
 */
import GradeEvidenceDrawer from "./GradeEvidenceDrawer";
import type { GradeEvidence } from "./gradeEvidenceTypes";

const DAILY: GradeEvidence = {
  candidate_id: "c1",
  formula: "ts_mean(close,5)",
  grade: "B",
  reason_zh: "OOS ICIR=0.18 未达 A 级 0.3 门槛，故评为 B 级",
  thresholds_source: "default",
  frequency: "daily",
  dimensions: [
    { key: "stat_significance", current: 0.021, threshold: 0.001, passed: false, gap: 0.02 },
    { key: "icir", current: 0.24, threshold: 0.3, passed: false, gap: 0.06 },
    { key: "coverage", current: 0.85, threshold: 0.8, passed: true, gap: 0 },
  ],
  stats: {
    t_test_p: 0.003,
    bonferroni_p: 0.021,
    fdr_q: 0.008,
    bootstrap_ci: [0.12, 0.78],
    permutation_p: 0.002,
    dsr_icir: 0.31,
    decay_ratio: 0.67,
    walk_forward: { windows: 4, same_direction: 4 },
    total_trials: 1987,
    degraded: false,
  },
  lineage: {
    generation: 7,
    parent_ids: ["p1", "p2"],
    operation: "crossover",
    economic_logic: "量价背离",
    logic_source: "ai",
  },
  manual_adjusted: false,
  grade_history: [
    { grade: "B", changed_at: "2026-06-30T10:00:00", trigger: "auto", reason: "自动评定" },
  ],
};

const MONTHLY: GradeEvidence = {
  ...DAILY,
  frequency: "monthly",
  stats: { ...DAILY.stats, degraded: true, bootstrap_ci: null, permutation_p: null },
};

const e = (over: Partial<GradeEvidence> = {}): GradeEvidence => ({ ...DAILY, ...over });

describe("GradeEvidenceDrawer Tab 结构", () => {
  it("恰好 3 个 Tab（not_do：不做 4 Tab）", () => {
    render(<GradeEvidenceDrawer evidence={DAILY} />);
    const tabs = document.querySelectorAll("[data-evidence-tab]");
    expect(tabs.length).toBe(3);
  });

  it("默认展示定级证据 Tab", () => {
    render(<GradeEvidenceDrawer evidence={DAILY} />);
    expect(document.querySelector("[data-evidence-panel='grade']")).toBeTruthy();
  });

  it("可切换到统计检验与血缘 Tab", () => {
    render(<GradeEvidenceDrawer evidence={DAILY} />);
    fireEvent.click(document.querySelector('[data-evidence-tab="stats"]') as HTMLElement);
    expect(document.querySelector("[data-evidence-panel='stats']")).toBeTruthy();
    fireEvent.click(document.querySelector('[data-evidence-tab="lineage"]') as HTMLElement);
    expect(document.querySelector("[data-evidence-panel='lineage']")).toBeTruthy();
  });
});

describe("Tab1 定级证据", () => {
  it("显示等级大标签与一句话定级理由", () => {
    render(<GradeEvidenceDrawer evidence={DAILY} />);
    expect(document.querySelector("[data-evidence-grade]")?.textContent).toBe("B");
    expect(document.querySelector("[data-evidence-reason]")?.textContent)
      .toContain("OOS ICIR=0.18");
  });

  it("8 维度明细含当前值/阈值/达标与阈值来源", () => {
    render(<GradeEvidenceDrawer evidence={DAILY} />);
    const rows = document.querySelectorAll("[data-evidence-dimension]");
    expect(rows.length).toBe(3);
    const text = rows[0].textContent ?? "";
    expect(text).toContain("0.021");
    expect(text).toContain("0.001");
    expect(document.querySelector("[data-evidence-threshold-source]")?.textContent)
      .toContain("默认");
  });

  it("自定义阈值来源需如实标注", () => {
    render(<GradeEvidenceDrawer evidence={e({ thresholds_source: "custom" })} />);
    expect(document.querySelector("[data-evidence-threshold-source]")?.textContent)
      .toMatch(/自定义/);
  });
});

describe("Tab2 统计检验证据", () => {
  it("渲染 8 项统计检验", () => {
    render(<GradeEvidenceDrawer evidence={DAILY} />);
    fireEvent.click(document.querySelector('[data-evidence-tab="stats"]') as HTMLElement);
    const rows = document.querySelectorAll("[data-evidence-stat]");
    expect(rows.length).toBe(8);
  });

  it("显示 total_trials 说明（DSR 按此校正）", () => {
    render(<GradeEvidenceDrawer evidence={DAILY} />);
    fireEvent.click(document.querySelector('[data-evidence-tab="stats"]') as HTMLElement);
    const note = document.querySelector("[data-evidence-total-trials]");
    expect(note?.textContent).toContain("1987");
  });

  it("**月频时 Bootstrap / 置换两行灰掉并标注样本不足**（pitfalls）", () => {
    render(<GradeEvidenceDrawer evidence={MONTHLY} />);
    fireEvent.click(document.querySelector('[data-evidence-tab="stats"]') as HTMLElement);
    const boot = document.querySelector('[data-evidence-stat="bootstrap"]');
    const perm = document.querySelector('[data-evidence-stat="permutation"]');
    expect(boot?.getAttribute("data-disabled")).toBe("true");
    expect(perm?.getAttribute("data-disabled")).toBe("true");
    expect(boot?.textContent).toMatch(/样本不足|未计算/);
    // 其余 6 项不灰
    expect(
      document.querySelector('[data-evidence-stat="t_test"]')?.getAttribute("data-disabled"),
    ).not.toBe("true");
  });

  it("月频在抽屉顶部提示「最高 B 级」", () => {
    render(<GradeEvidenceDrawer evidence={MONTHLY} />);
    const tip = document.querySelector("[data-evidence-monthly-tip]");
    expect(tip).toBeTruthy();
    expect(tip?.textContent).toContain("B");
  });

  it("日频不出现月频提示，且 Bootstrap/置换不灰", () => {
    render(<GradeEvidenceDrawer evidence={DAILY} />);
    expect(document.querySelector("[data-evidence-monthly-tip]")).toBeNull();
    fireEvent.click(document.querySelector('[data-evidence-tab="stats"]') as HTMLElement);
    expect(
      document.querySelector('[data-evidence-stat="bootstrap"]')?.getAttribute("data-disabled"),
    ).not.toBe("true");
  });
});

describe("Tab3 血缘与来源", () => {
  it("显示进化路径与经济逻辑", () => {
    render(<GradeEvidenceDrawer evidence={DAILY} />);
    fireEvent.click(document.querySelector('[data-evidence-tab="lineage"]') as HTMLElement);
    const panel = document.querySelector("[data-evidence-panel='lineage']");
    expect(panel?.textContent).toContain("7");            // 代数
    expect(panel?.textContent).toContain("p1");           // 父代
    expect(panel?.textContent).toContain("量价背离");      // 经济逻辑
  });
});

describe("人工调整等级（§8.4.2）", () => {
  it("原因必填且至少 10 字，不足则不可提交", () => {
    const onAdjust = vi.fn();
    render(<GradeEvidenceDrawer evidence={DAILY} onAdjustGrade={onAdjust} />);
    fireEvent.click(document.querySelector("[data-evidence-adjust]") as HTMLElement);
    fireEvent.change(
      document.querySelector("[data-evidence-adjust-reason]") as HTMLTextAreaElement,
      { target: { value: "太短" } },
    );
    const submit = document.querySelector("[data-evidence-adjust-submit]") as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fireEvent.change(
      document.querySelector("[data-evidence-adjust-reason]") as HTMLTextAreaElement,
      { target: { value: "业务上该因子有明确经济含义，人工上调" } },
    );
    expect(
      (document.querySelector("[data-evidence-adjust-submit]") as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it("提交后带上目标等级与原因", () => {
    const onAdjust = vi.fn();
    render(<GradeEvidenceDrawer evidence={DAILY} onAdjustGrade={onAdjust} />);
    fireEvent.click(document.querySelector("[data-evidence-adjust]") as HTMLElement);
    fireEvent.change(
      document.querySelector("[data-evidence-adjust-grade]") as HTMLSelectElement,
      { target: { value: "A" } },
    );
    fireEvent.change(
      document.querySelector("[data-evidence-adjust-reason]") as HTMLTextAreaElement,
      { target: { value: "业务上该因子有明确经济含义，人工上调" } },
    );
    fireEvent.click(document.querySelector("[data-evidence-adjust-submit]") as HTMLElement);
    expect(onAdjust).toHaveBeenCalledWith("A", "业务上该因子有明确经济含义，人工上调");
  });

  it("人工调整后提示季度重评不覆盖，并提供「恢复自动」", () => {
    const onRestore = vi.fn();
    render(
      <GradeEvidenceDrawer
        evidence={e({ manual_adjusted: true })}
        onRestoreAuto={onRestore}
      />,
    );
    const tip = document.querySelector("[data-evidence-manual-tip]");
    expect(tip?.textContent).toMatch(/不会自动覆盖|不覆盖/);
    const btn = document.querySelector("[data-evidence-restore-auto]") as HTMLElement;
    expect(btn).toBeTruthy();
    fireEvent.click(btn);
    expect(onRestore).toHaveBeenCalled();
  });
});

describe("季度重评横幅与评级历史（§8.4.3）", () => {
  it("升级显示绿色横幅", () => {
    render(
      <GradeEvidenceDrawer
        evidence={e({ quarter_change: { from: "B", to: "A", reason: "连续 2 季 ICIR 稳定" } })}
      />,
    );
    const banner = document.querySelector("[data-evidence-quarter-change]");
    expect(banner?.getAttribute("data-direction")).toBe("up");
    expect(banner?.textContent).toMatch(/B/);
    expect(banner?.textContent).toMatch(/A/);
  });

  it("降级显示红色横幅并说明原因", () => {
    render(
      <GradeEvidenceDrawer
        evidence={e({ quarter_change: { from: "A", to: "C", reason: "ICIR 较上季下降 42%" } })}
      />,
    );
    const banner = document.querySelector("[data-evidence-quarter-change]");
    expect(banner?.getAttribute("data-direction")).toBe("down");
    expect(banner?.textContent).toContain("42%");
  });

  it("降级并移出 FactorSet 追加提示", () => {
    render(
      <GradeEvidenceDrawer
        evidence={e({
          quarter_change: { from: "A", to: "C", reason: "下降 42%" },
          removed_from_factor_set: "fs-9",
        })}
      />,
    );
    const note = document.querySelector("[data-evidence-removed-set]");
    expect(note?.textContent).toContain("fs-9");
  });

  it("评级历史时间轴展示等级/时间/触发方式/原因", () => {
    render(
      <GradeEvidenceDrawer
        evidence={e({
          grade_history: [
            { grade: "A", changed_at: "2026-09-18T10:00:00", trigger: "quarterly",
              reason: "连续 2 季稳定" },
            { grade: "B", changed_at: "2026-06-30T10:00:00", trigger: "auto", reason: "自动评定" },
          ],
        })}
      />,
    );
    const items = document.querySelectorAll("[data-evidence-history-item]");
    expect(items.length).toBe(2);
    expect(items[0].textContent).toContain("A");
    expect(items[0].textContent).toMatch(/季度|quarterly/);
  });
});
