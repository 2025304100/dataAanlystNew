import { describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@testing-library/react";

/**
 * T29 Step2 切分面板测试（向导 §4 / §4.1，设计 §7.2，任务卡 pitfalls）。
 *
 * 覆盖：
 *   1. 三段比例默认 60/20/20 且**和必须为 100%**；
 *   2. 比例和不为 100% → 阻断提示（不能进入下一步）；
 *   3. 比例模式 / 自定义日期边界模式切换；
 *   4. **SplitBudget 展示**（后端下发）：总点数与各段点数；
 *   5. **purge 必须同时展示「调仓点数」与「折算交易日数」**
 *      —— 否则用户会把 purge=5 误读成 5 个交易日（任务卡 pitfalls 第一条）；
 *   6. 月频（statistically_degraded）→ 显示「最高 B 级」降级提示；
 *   7. meets_floor=false → 显示最低样本量阻断；
 *   8. **not_do：不在前端计算切分** —— 无 budget 时不得臆造段边界/点数。
 */
import MiningSplitPanel from "./MiningSplitPanel";
import type { SplitBudget } from "./splitTypes";

const BUDGET: SplitBudget = {
  frequency: "daily",
  total_points: 1200,
  train_points: 720,
  val_points: 240,
  test_points: 240,
  purge_points: 5,
  embargo_points: 5,
  purge_trading_days: 7,
  embargo_trading_days: 7,
  tail_loss: 12,
  frequency_floor: 252,
  meets_floor: true,
  statistically_degraded: false,
  // 段边界**由后端下发**（not_do：前端不计算切分）
  train_start: "2021-01-04",
  train_end: "2024-01-03",
  val_end: "2024-12-31",
};

function bp(over: Partial<SplitBudget> = {}) {
  return { ...BUDGET, ...over };
}

describe("MiningSplitPanel 三段比例", () => {
  it("默认 60/20/20 且合计 100", () => {
    render(<MiningSplitPanel />);
    const val = (id: string) =>
      (document.querySelector(`[data-split-ratio="${id}"]`) as HTMLInputElement).value;
    expect(val("train")).toBe("60");
    expect(val("val")).toBe("20");
    expect(val("test")).toBe("20");
    expect(document.querySelector("[data-split-sum]")?.textContent).toContain("100");
  });

  it("比例和不为 100 → 显示阻断提示", () => {
    render(<MiningSplitPanel />);
    fireEvent.change(
      document.querySelector('[data-split-ratio="test"]') as HTMLInputElement,
      { target: { value: "5" } },
    );
    expect(document.querySelector("[data-split-error]")).toBeTruthy();
    expect(document.querySelector("[data-split-sum]")?.textContent).toContain("85");
  });

  it("比例改回 100 后错误消失", () => {
    render(<MiningSplitPanel />);
    const test = document.querySelector('[data-split-ratio="test"]') as HTMLInputElement;
    fireEvent.change(test, { target: { value: "5" } });
    expect(document.querySelector("[data-split-error]")).toBeTruthy();
    fireEvent.change(test, { target: { value: "20" } });
    expect(document.querySelector("[data-split-error]")).toBeNull();
  });

  it("比例变化回调上抛（由后端重算，前端不算切分）", () => {
    const onChange = vi.fn();
    render(<MiningSplitPanel onChange={onChange} />);
    fireEvent.change(
      document.querySelector('[data-split-ratio="train"]') as HTMLInputElement,
      { target: { value: "50" } },
    );
    expect(onChange).toHaveBeenCalled();
  });
});

describe("MiningSplitPanel 自定义边界", () => {
  it("默认比例模式，切到自定义后出现日期边界输入", () => {
    render(<MiningSplitPanel />);
    expect(document.querySelector("[data-split-custom-train-end]")).toBeNull();
    fireEvent.click(document.querySelector("[data-split-mode-custom]") as HTMLElement);
    expect(document.querySelector("[data-split-custom-train-end]")).toBeTruthy();
    expect(document.querySelector("[data-split-custom-val-end]")).toBeTruthy();
  });

  it("切回比例模式隐藏日期输入", () => {
    render(<MiningSplitPanel />);
    fireEvent.click(document.querySelector("[data-split-mode-custom]") as HTMLElement);
    fireEvent.click(document.querySelector("[data-split-mode-ratio]") as HTMLElement);
    expect(document.querySelector("[data-split-custom-train-end]")).toBeNull();
  });
});

describe("MiningSplitPanel SplitBudget 展示", () => {
  it("展示总点数与三段点数", () => {
    render(<MiningSplitPanel budget={BUDGET} />);
    const text = document.querySelector("[data-split-budget]")?.textContent ?? "";
    expect(text).toContain("1200");
    expect(text).toContain("720");
    expect(text).toContain("240");
  });

  it("purge 同时展示调仓点数与折算交易日数（防误读）", () => {
    render(<MiningSplitPanel budget={bp({ purge_points: 5, purge_trading_days: 7 })} />);
    const panel = document.querySelector("[data-split-budget]");
    expect(panel?.querySelector("[data-split-purge-points]")?.textContent).toContain("5");
    expect(panel?.querySelector("[data-split-purge-days]")?.textContent).toContain("7");
    // 两处必须**同时**出现，缺一会让人把 5 个点当成 5 天
    expect(panel?.querySelector("[data-split-purge-points]")).toBeTruthy();
    expect(panel?.querySelector("[data-split-purge-days]")).toBeTruthy();
  });

  it("embargo 同样双单位展示", () => {
    render(<MiningSplitPanel budget={bp({ embargo_points: 3, embargo_trading_days: 9 })} />);
    const panel = document.querySelector("[data-split-budget]");
    expect(panel?.querySelector("[data-split-embargo-points]")?.textContent).toContain("3");
    expect(panel?.querySelector("[data-split-embargo-days]")?.textContent).toContain("9");
  });

  it("月频降级（statistically_degraded）→ 显示最高 B 级提示", () => {
    render(
      <MiningSplitPanel
        budget={bp({ frequency: "monthly", statistically_degraded: true })}
      />,
    );
    const tip = document.querySelector("[data-split-degraded]");
    expect(tip).toBeTruthy();
    expect(tip?.textContent).toContain("B");
  });

  it("未达最低样本量 → 显示阻断提示并给出门槛值", () => {
    render(<MiningSplitPanel budget={bp({ meets_floor: false, frequency_floor: 252 })} />);
    const tip = document.querySelector("[data-split-floor-fail]");
    expect(tip).toBeTruthy();
    expect(tip?.textContent).toContain("252");
  });
});

describe("MiningSplitPanel not_do 守卫", () => {
  it("无 budget 时不臆造切分结果（不渲染预算区与段边界）", () => {
    render(<MiningSplitPanel />);
    expect(document.querySelector("[data-split-budget]")).toBeNull();
    expect(document.querySelector("[data-split-boundary]")).toBeNull();
  });

  it("段边界只来自后端 budget，不随前端比例变化而变", () => {
    const { rerender } = render(<MiningSplitPanel budget={BUDGET} />);
    const before = document.querySelector("[data-split-boundary]")?.textContent;
    expect(before).toBeTruthy();
    fireEvent.change(
      document.querySelector('[data-split-ratio="train"]') as HTMLInputElement,
      { target: { value: "50" } },
    );
    rerender(<MiningSplitPanel budget={BUDGET} />);
    // 边界未随比例变化（切分由后端算，前端只收不发）
    expect(document.querySelector("[data-split-boundary]")?.textContent).toBe(before);
  });
});
