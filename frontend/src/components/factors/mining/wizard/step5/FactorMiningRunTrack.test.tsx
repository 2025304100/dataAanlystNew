import { describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@testing-library/react";

/**
 * T32 Step5 运行跟踪测试（向导 §8.3 / §8.3.1~§8.3.7）。
 *
 * 覆盖：
 *   1. 进度总览（代数 X/Y、收敛状态、多样性百分比）；
 *   2. 多样性 <30% → 早熟警告（§8.3.1）；
 *   3. **收敛参考线来自配置阈值**（§8.3.2 明令不得把 0.08 写成固定门禁）；
 *   4. 连续 3 代提升 < 阈值 → 提示「已收敛，建议停止」，**是否停止由用户确认**；
 *   5. 中断 → 暂停（按钮变「继续」）；
 *   6. 提前停止 → 确认框（提示保留前 X 代 + 对 Top 50 做最终验证）；
 *   7. 最终验证进度单独展示。
 */
import FactorMiningRunTrack from "./FactorMiningRunTrack";
import type { RunProgress } from "./runTypes";

const PROGRESS: RunProgress = {
  run_id: "run-1",
  status: "running",
  generation: 8,
  max_generations: 20,
  best_icir: 0.42,
  avg_icir: 0.31,
  diversity: 0.52,
  convergence_threshold: 0.01,
  converged: false,
  eta_seconds: 360,
  population_size: 100,
  final_validation: null,
  curve: [
    { generation: 1, best_icir: 0.25, avg_icir: 0.18 },
    { generation: 2, best_icir: 0.3, avg_icir: 0.2 },
    { generation: 3, best_icir: 0.35, avg_icir: 0.24 },
    { generation: 4, best_icir: 0.42, avg_icir: 0.31 },
  ],
};

const p = (over: Partial<RunProgress> = {}): RunProgress => ({ ...PROGRESS, ...over });

describe("FactorMiningRunTrack 进度总览", () => {
  it("展示代数、收敛状态与多样性", () => {
    render(<FactorMiningRunTrack progress={PROGRESS} />);
    const overview = document.querySelector("[data-run-overview]");
    expect(overview?.textContent).toContain("8");
    expect(overview?.textContent).toContain("20");
    expect(document.querySelector("[data-run-diversity]")?.textContent).toContain("52");
    expect(document.querySelector("[data-run-converged]")).toBeTruthy();
  });

  it("多样性低于 30% 时提示种群早熟（含增加注入率建议）", () => {
    render(<FactorMiningRunTrack progress={p({ diversity: 0.22 })} />);
    const warn = document.querySelector("[data-run-premature]");
    expect(warn).toBeTruthy();
    expect(warn?.textContent).toMatch(/注入/);
  });

  it("多样性正常时不出现早熟警告", () => {
    render(<FactorMiningRunTrack progress={p({ diversity: 0.52 })} />);
    expect(document.querySelector("[data-run-premature]")).toBeNull();
  });
});

describe("FactorMiningRunTrack 进化曲线与收敛提示", () => {
  it("曲线渲染并**使用配置的收敛阈值**（不是写死的 0.08）", () => {
    render(<FactorMiningRunTrack progress={p({ convergence_threshold: 0.05 })} />);
    const chart = document.querySelector("[data-run-curve]");
    expect(chart).toBeTruthy();
    expect(chart?.textContent).toContain("0.05");
    expect(chart?.textContent).not.toContain("0.08");
  });

  it("已收敛且达阈值 → 提示「建议停止」但不自动停（需用户确认）", () => {
    const onStop = vi.fn();
    render(
      <FactorMiningRunTrack
        progress={p({ converged: true, stall_generations: 3 })}
        onStop={onStop}
      />,
    );
    const tip = document.querySelector("[data-run-converge-tip]");
    expect(tip).toBeTruthy();
    expect(tip?.textContent).toMatch(/建议停止|停止/);
    expect(onStop).not.toHaveBeenCalled();     // 不自动停止
    expect(document.querySelector("[data-run-stop]")).toBeTruthy();
  });
});

describe("FactorMiningRunTrack 中断/停止/放弃（§8.3.5）", () => {
  it("中断后按钮变为「继续」", () => {
    const onPause = vi.fn();
    const { rerender } = render(
      <FactorMiningRunTrack progress={p({ status: "running" })} onPause={onPause} />,
    );
    fireEvent.click(document.querySelector("[data-run-pause]") as HTMLElement);
    expect(onPause).toHaveBeenCalled();
    rerender(
      <FactorMiningRunTrack progress={p({ status: "paused" })} onResume={vi.fn()} />,
    );
    expect(document.querySelector("[data-run-resume]")).toBeTruthy();
    expect(document.querySelector("[data-run-pause]")).toBeNull();
  });

  it("提前停止需确认，确认框说明保留前 X 代与 Top 50 验证", () => {
    const onStop = vi.fn();
    render(
      <FactorMiningRunTrack
        progress={p({ generation: 8 })}
        finalValidationTop={50}
        onStop={onStop}
      />,
    );
    fireEvent.click(document.querySelector("[data-run-stop]") as HTMLElement);
    const confirm = document.querySelector("[data-run-stop-confirm]");
    expect(confirm).toBeTruthy();
    expect(confirm?.textContent).toContain("8");
    expect(confirm?.textContent).toContain("50");
    expect(onStop).not.toHaveBeenCalled();      // 未确认前不停止
  });

  it("完全放弃需二次确认且提示不可恢复", () => {
    const onDiscard = vi.fn();
    render(<FactorMiningRunTrack progress={PROGRESS} onDiscard={onDiscard} />);
    fireEvent.click(document.querySelector("[data-run-discard]") as HTMLElement);
    const confirm = document.querySelector("[data-run-discard-confirm]");
    expect(confirm?.textContent).toMatch(/不可恢复/);
    expect(onDiscard).not.toHaveBeenCalled();
  });
});

describe("FactorMiningRunTrack 最终验证", () => {
  it("最终验证进度单独展示", () => {
    render(
      <FactorMiningRunTrack
        progress={p({
          status: "validating",
          final_validation: { done: 20, total: 50, started_at: "2026-09-18T23:00:00" },
        })}
      />,
    );
    const area = document.querySelector("[data-run-final-validation]");
    expect(area).toBeTruthy();
    expect(area?.textContent).toContain("20");
    expect(area?.textContent).toContain("50");
  });

  it("§8.3.7：最终验证进度细化展示（进度条 + 状态 + 完成提示）", () => {
    render(
      <FactorMiningRunTrack
        progress={p({
          status: "validating",
          final_validation: { done: 50, total: 50 },
        })}
      />,
    );
    const area = document.querySelector("[data-run-final-validation]");
    expect(area?.querySelector("[data-run-final-progress]")).toBeTruthy();
    expect(area?.querySelector("[data-run-final-status]")?.textContent).toContain("已完成");
    expect(area?.querySelector("[data-run-final-overview-hint]")).toBeTruthy();
  });
});

describe("FactorMiningRunTrack 当前种群 Top 因子（F2 §8.3.3）", () => {
  it("有 topCandidates 时渲染 Top 列表（含 ICIR/覆盖率/来源）", () => {
    render(
      <FactorMiningRunTrack
        progress={p({ generation: 8 })}
        topCandidates={[
          { rank: 1, formula: "std(close,5)/mean(std(close,20),60)", icir: 0.42, coverage: 0.95, source: "精英保留" },
          { rank: 2, formula: "delta(roe_ttm,4)/std(delta(roe_ttm,4),8)", icir: 0.39, coverage: 0.88, source: "变异自第6代#3" },
        ]}
      />,
    );
    const list = document.querySelector("[data-run-top-list]");
    expect(list).toBeTruthy();
    expect(list?.textContent).toContain("std(close,5)");
    expect(list?.textContent).toContain("0.42");
    expect(list?.textContent).toContain("精英保留");
    expect(list?.textContent).toContain("第 8 代");
  });

  it("新进 Top10 的条目标亮（换入新公式后 data-run-top-islot=new）", () => {
    const { rerender } = render(
      <FactorMiningRunTrack
        progress={p({ generation: 8 })}
        topCandidates={[
          { rank: 1, formula: "aaa", icir: 0.4 },
          { rank: 2, formula: "bbb", icir: 0.3 },
        ]}
      />,
    );
    // 首轮仅建基线，不闪烁
    expect(document.querySelector('[data-run-top-islot="new"]')).toBeNull();
    // 换代：bbb 掉出，ccc 新进 → ccc 标 new
    rerender(
      <FactorMiningRunTrack
        progress={p({ generation: 9 })}
        topCandidates={[
          { rank: 1, formula: "aaa", icir: 0.4 },
          { rank: 2, formula: "ccc", icir: 0.45 },
        ]}
      />,
    );
    const newRow = document.querySelector('[data-run-top-islot="new"]');
    expect(newRow).toBeTruthy();
    expect(newRow?.textContent).toContain("ccc");
    // aaa 仍在 Top10 → 不标 new
    expect(
      document.querySelector('[data-run-top-row="1"]')?.getAttribute("data-run-top-islot"),
    ).toBe("stable");
  });

  it("无 topCandidates 时不渲染 Top 列表", () => {
    render(<FactorMiningRunTrack progress={PROGRESS} />);
    expect(document.querySelector("[data-run-top-list]")).toBeNull();
  });
});

describe("FactorMiningRunTrack 多样性 health 曲线（双轴）", () => {
  it("有 diversity_curve 时渲染双轴曲线（左 ICIR + 右 health + 0.3 参考线）", () => {
    render(
      <FactorMiningRunTrack
        progress={p({
          diversity_curve: [
            { generation: 1, diversity_health: 0.6 },
            { generation: 2, diversity_health: 0.4 },
            { generation: 3, diversity_health: 0.25 },
          ],
        })}
      />,
    );
    const chart = document.querySelector("[data-run-diversity-curve]");
    expect(chart).toBeTruthy();
    expect(document.querySelectorAll("[data-diversity-icir]").length).toBeGreaterThan(0);
    expect(document.querySelectorAll("[data-diversity-health]").length).toBe(3);
    expect(document.querySelector("[data-diversity-premature-line]")).toBeTruthy();
  });

  it("无 diversity_curve 时不渲染双轴曲线", () => {
    render(<FactorMiningRunTrack progress={PROGRESS} />);
    expect(document.querySelector("[data-run-diversity-curve]")).toBeNull();
  });
});
