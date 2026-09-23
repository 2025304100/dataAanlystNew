import { describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@testing-library/react";

/**
 * T32 结果总览测试（向导 §8.4，任务卡 pitfalls / not_do）。
 *
 * 覆盖：
 *   1. **结果页顶部必须标注研究声明**（pitfalls 第一条）；
 *   2. **跳转因子模型页必须携带 4 项参数**：factor_set_id / data_cutoff_at /
 *      candidate_pool_snapshot_id / rebalance_frequency（pitfalls 第二条）；
 *   3. **结果页不自动激活因子或模型**（not_do）—— 渲染后不得自动触发激活/跳转；
 *   4. 等级筛选 chip（全部/S/A/B/C/D，含各级数量）；
 *   5. **D 级默认不勾选**；选中集含 D 级时**二次确认**（§8.4 按等级批量处理）；
 *   6. 有效因子列表展示校正后 ICIR、等级标签、衰减率、来源。
 */
import FactorMiningResult from "./FactorMiningResult";
import type { MiningResultRow } from "./resultTypes";

const ROWS: MiningResultRow[] = [
  {
    candidate_id: "c1", formula: "ts_mean(close,5)", grade: "S",
    icir: 0.62, icir_adjusted: 0.55, coverage: 0.95, turnover: 0.18,
    decay_ratio: 0.9, source: "elite", generation: 3,
    complexity: 5, generation_rank: 1,
  },
  {
    candidate_id: "c2", formula: "cs_rank(pe_ttm)", grade: "B",
    icir: 0.24, icir_adjusted: 0.21, coverage: 0.78, turnover: 0.4,
    decay_ratio: 0.7, source: "mutation", generation: 5,
    complexity: 3, generation_rank: 2,
  },
  {
    candidate_id: "c3", formula: "ts_std(volume,20)", grade: "D",
    icir: 0.08, icir_adjusted: 0.05, coverage: 0.55, turnover: 0.8,
    decay_ratio: 0.3, source: "random", generation: 7,
    complexity: 8, generation_rank: 3,
  },
];

const CONTEXT = {
  factor_set_id: "fs-9",
  data_cutoff_at: "2026-09-05",
  candidate_pool_snapshot_id: "snap-3",
  rebalance_frequency: "weekly",
};

describe("FactorMiningResult 研究声明与 not_do", () => {
  it("顶部标注研究声明", () => {
    render(<FactorMiningResult rows={ROWS} context={CONTEXT} />);
    const banner = document.querySelector("[data-result-disclaimer]");
    expect(banner).toBeTruthy();
    expect(banner?.textContent).toMatch(/研究|不构成投资建议/);
    // 必须位于页面顶部（第一个区块）
    const first = document.querySelector("[data-result-page]")?.firstElementChild;
    expect(first?.getAttribute("data-result-disclaimer") !== null ||
      Boolean(first?.querySelector("[data-result-disclaimer]"))).toBe(true);
  });

  it("**不自动激活因子或模型**（渲染后无激活/跳转调用）", () => {
    const onActivate = vi.fn();
    const onGotoModel = vi.fn();
    render(
      <FactorMiningResult rows={ROWS} context={CONTEXT}
        onActivate={onActivate} onGotoFactorModel={onGotoModel} />,
    );
    expect(onActivate).not.toHaveBeenCalled();
    expect(onGotoModel).not.toHaveBeenCalled();
  });
});

describe("FactorMiningResult 跳转因子模型页", () => {
  it("跳转携带 4 项上下文参数", () => {
    const onGoto = vi.fn();
    render(<FactorMiningResult rows={ROWS} context={CONTEXT} onGotoFactorModel={onGoto} />);
    fireEvent.click(document.querySelector("[data-result-goto-model]") as HTMLElement);
    expect(onGoto).toHaveBeenCalledTimes(1);
    const arg = onGoto.mock.calls[0][0] as Record<string, unknown>;
    expect(arg.factor_set_id).toBe("fs-9");
    expect(arg.data_cutoff_at).toBe("2026-09-05");
    expect(arg.candidate_pool_snapshot_id).toBe("snap-3");
    expect(arg.rebalance_frequency).toBe("weekly");
    expect(Object.keys(arg).length).toBe(4);
  });

  it("上下文缺失时不发起跳转（避免带残缺参数）", () => {
    const onGoto = vi.fn();
    render(
      <FactorMiningResult rows={ROWS}
        context={{ ...CONTEXT, factor_set_id: "" }} onGotoFactorModel={onGoto} />,
    );
    expect(
      (document.querySelector("[data-result-goto-model]") as HTMLButtonElement).disabled,
    ).toBe(true);
    fireEvent.click(document.querySelector("[data-result-goto-model]") as HTMLElement);
    expect(onGoto).not.toHaveBeenCalled();
  });
});

describe("FactorMiningResult 等级筛选与批量", () => {
  it("渲染等级 chip 并显示各级数量", () => {
    render(<FactorMiningResult rows={ROWS} context={CONTEXT} />);
    const chips = document.querySelectorAll("[data-grade-chip]");
    expect(chips.length).toBe(6);              // 全部 + S/A/B/C/D
    const text = document.body.textContent ?? "";
    expect(document.querySelector('[data-grade-chip="S"]')?.textContent).toContain("1");
    expect(document.querySelector('[data-grade-chip="D"]')?.textContent).toContain("1");
    expect(text).toContain("ts_mean(close,5)");
  });

  it("点击 chip 过滤列表", () => {
    render(<FactorMiningResult rows={ROWS} context={CONTEXT} />);
    fireEvent.click(document.querySelector('[data-grade-chip="S"]') as HTMLElement);
    const body = document.querySelector("[data-result-rows]");
    expect(body?.textContent).toContain("ts_mean(close,5)");
    expect(body?.textContent).not.toContain("cs_rank(pe_ttm)");
  });

  it("**D 级默认不勾选**，且默认选中集为空", () => {
    render(<FactorMiningResult rows={ROWS} context={CONTEXT} />);
    const dBox = document.querySelector('[data-result-checkbox="c3"]') as HTMLInputElement;
    expect(dBox.checked).toBe(false);
    expect(
      document.querySelector("[data-result-selected-count]")?.textContent,
    ).toContain("0");
  });

  it("选中集含 D 级时批量加入需二次确认", () => {
    const onBatchAdd = vi.fn();
    render(<FactorMiningResult rows={ROWS} context={CONTEXT} onBatchAdd={onBatchAdd} />);
    fireEvent.click(document.querySelector('[data-result-checkbox="c3"]') as HTMLElement);
    fireEvent.click(document.querySelector("[data-result-batch-add]") as HTMLElement);
    const confirm = document.querySelector("[data-result-dgrade-confirm]");
    expect(confirm).toBeTruthy();
    expect(confirm?.textContent).toMatch(/D 级|1/);
    expect(onBatchAdd).not.toHaveBeenCalled();
  });

  it("未选 D 级时批量加入直接执行", () => {
    const onBatchAdd = vi.fn();
    render(<FactorMiningResult rows={ROWS} context={CONTEXT} onBatchAdd={onBatchAdd} />);
    fireEvent.click(document.querySelector('[data-result-checkbox="c1"]') as HTMLElement);
    fireEvent.click(document.querySelector("[data-result-batch-add]") as HTMLElement);
    expect(document.querySelector("[data-result-dgrade-confirm]")).toBeNull();
    expect(onBatchAdd).toHaveBeenCalled();
  });
});

describe("FactorMiningResult 列表字段", () => {
  it("展示校正后 ICIR、等级标签、衰减率与来源", () => {
    render(<FactorMiningResult rows={ROWS} context={CONTEXT} />);
    const body = document.querySelector("[data-result-rows]")?.textContent ?? "";
    expect(body).toContain("0.55");            // 校正后 ICIR
    expect(body).toContain("0.9");             // 衰减率
    expect(body).toMatch(/精英|elite/);         // 来源
    expect(document.querySelector('[data-result-grade="c1"]')?.textContent).toBe("S");
  });
});

describe("FactorMiningResult 帕累托散点（§6.5.6）", () => {
  it("渲染散点：每点一个点、rank=1 前沿高亮描边", () => {
    render(<FactorMiningResult rows={ROWS} context={CONTEXT} />);
    const pareto = document.querySelector("[data-result-pareto]");
    expect(pareto).toBeTruthy();
    expect(document.querySelectorAll("[data-pareto-point]").length).toBe(3);
    expect(document.querySelectorAll("[data-pareto-frontier]").length).toBe(1);
    expect(document.querySelector('[data-pareto-frontier="c1"]')).toBeTruthy();
    const legend = document.querySelector(".mining-result-pareto-legend")?.textContent ?? "";
    expect(legend).toMatch(/换手率|turnover/);
    expect(legend).toMatch(/ICIR/);
  });

  it("缺少换手率或 ICIR 的行不进散点", () => {
    render(
      <FactorMiningResult
        rows={[{ ...ROWS[0], turnover: undefined }]}
        context={CONTEXT}
      />,
    );
    expect(document.querySelector("[data-result-pareto]")).toBeNull();
  });
});
