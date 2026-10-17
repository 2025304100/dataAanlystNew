import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@testing-library/react";
import { setLocale } from "../../../../../i18n";

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
 *   6. 测试样本不足（statistically_degraded，不限频率）→ 显示「最高 B 级」提示；
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

afterEach(() => setLocale("zh-CN"));

describe("MiningSplitPanel 三段比例", () => {
  /** antd InputNumber 会把 data-* 转发到**内层 input** 上（根是包装 div） */
  const ratioInput = (id: string) =>
    document.querySelector(`[data-split-ratio="${id}"]`) as HTMLInputElement;
  /** 触发 antd InputNumber 变更（写内部 input） */
  const changeRatio = (id: string, value: string) => {
    fireEvent.change(ratioInput(id), { target: { value } });
  };

  it("默认 60/20/20 且合计 100", () => {
    render(<MiningSplitPanel />);
    expect(ratioInput("train").value).toBe("60");
    expect(ratioInput("val").value).toBe("20");
    expect(ratioInput("test").value).toBe("20");
    expect(document.querySelector("[data-split-sum]")?.textContent).toContain("100");
  });

  it("测试比例只读并随训练与验证比例联动，显示值与回调保持一致", () => {
    const onChange = vi.fn();
    render(<MiningSplitPanel onChange={onChange} />);
    expect(ratioInput("test").readOnly).toBe(true);
    changeRatio("train", "50");
    expect(ratioInput("test").value).toBe("30");
    expect(onChange).toHaveBeenLastCalledWith({ train: 50, val: 20, test: 30 });
    expect(document.querySelector("[data-split-error]")).toBeNull();
  });

  it("训练与验证挤占全部样本时阻断，留出测试样本后恢复", () => {
    render(<MiningSplitPanel />);
    changeRatio("train", "80");
    expect(ratioInput("test").value).toBe("0");
    expect(document.querySelector("[data-split-error]")).toBeTruthy();
    changeRatio("train", "60");
    expect(document.querySelector("[data-split-error]")).toBeNull();
    changeRatio("val", "0");
    expect(document.querySelector("[data-split-error]")).toBeTruthy();
  });

  it("比例变化回调上抛（由后端重算，前端不算切分）", () => {
    const onChange = vi.fn();
    render(<MiningSplitPanel onChange={onChange} />);
    changeRatio("train", "50");
    expect(onChange).toHaveBeenCalled();
  });
});

describe("MiningSplitPanel 自定义边界", () => {
  it("未接入执行的自定义日期边界禁用并说明，不展示无效输入", () => {
    render(<MiningSplitPanel />);
    const custom = document.querySelector("[data-split-mode-custom]") as HTMLButtonElement;
    expect(custom.disabled).toBe(true);
    expect(document.querySelector("[data-split-custom-train-end]")).toBeNull();
    fireEvent.click(custom);
    expect(document.querySelector("[data-split-custom-train-end]")).toBeNull();
    expect(document.querySelector("[data-split-custom-unavailable]")).toBeTruthy();
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

  it.each([
    { locale: "zh-CN" as const, insufficient: "测试段样本不足", daily: "日频", monthly: "月频", lower: "降低频率" },
    { locale: "en-US" as const, insufficient: "Insufficient test points", daily: "daily", monthly: "Monthly", lower: "lower frequency" },
  ])("$locale：周频测试样本不足按实际原因解释，并建议增加调仓点", ({ locale, insufficient, daily, monthly, lower }) => {
    setLocale(locale);
    render(<MiningSplitPanel budget={bp({ frequency: "weekly", test_points: 11, statistically_degraded: true, meets_floor: false, frequency_floor: 104 })} />);
    const degraded = document.querySelector("[data-split-degraded]")?.textContent ?? "";
    expect(degraded).toContain(insufficient);
    expect(degraded).toContain("B");
    expect(degraded).not.toContain(monthly);
    const floor = document.querySelector("[data-split-floor-fail]")?.textContent ?? "";
    expect(floor).toContain("104");
    expect(floor).toContain(daily);
    expect(floor).not.toContain(lower);
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
    const input = document.querySelector(
      '[data-split-ratio="train"]',
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "50" } });
    rerender(<MiningSplitPanel budget={BUDGET} />);
    // 边界未随比例变化（切分由后端算，前端只收不发）
    expect(document.querySelector("[data-split-boundary]")?.textContent).toBe(before);
  });
});
