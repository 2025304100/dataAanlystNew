import { describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@testing-library/react";

/**
 * G5 验收测试：「5 步向导可点通到结果页」（2026-09-19 接线）。
 *
 * 覆盖：
 *   1. 默认落在第 1 步（候选池），且 5 步步骤条齐全；
 *   2. 「下一步」逐级推进，依次挂载 step2~step5 各自的面板；
 *   3. 步骤条第 5 步之后「下一步」禁用；第 1 步「上一步」禁用；
 *   4. 步骤条可**点击跳转**；
 *   5. 第 5 步无运行数据时显示空态占位（**不臆造进度**）；有数据时渲染进化跟踪。
 */
vi.mock("../../../../api/factorMining", () => ({
  factorMiningApi: {
    listRuns: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
  },
}));

vi.mock("../../../../api/client", () => ({
  api: { scoringGetOverviewAsFactor: vi.fn(async () => ({})) },
  requestJson: vi.fn(async () => ({})),
}));

vi.mock("./step1/poolApi", () => ({
  MIN_POOL_SIZE: 50,
  previewPool: vi.fn(async () => ({
    matched: 300, total: 5000, excluded: 4700, by_rule: [], warnings: [], blocked: null,
  })),
  createPoolFromFilter: vi.fn(),
  createSnapshot: vi.fn(),
  deleteLatestSnapshot: vi.fn(),
  fetchMembers: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
  fetchFilterPresets: vi.fn(async () => []),
  fetchFilterFields: vi.fn(async () => []),
  fetchLatestSnapshot: vi.fn(async () => null),
  downloadImportTemplate: vi.fn(),
  previewImport: vi.fn(),
  exportImportErrors: vi.fn(),
  importMembers: vi.fn(),
}));

import MiningShell from "../MiningShell";

const PANELS = ["pool", "time-target", "field", "evolution", "run"];

describe("G5：5 步向导可点通", () => {
  it("默认第 1 步，步骤条含全部 5 步", () => {
    render(<MiningShell />);
    expect(document.querySelector("[data-mining-step-bar]")).toBeTruthy();
    expect(document.querySelectorAll("[data-mining-step]").length).toBe(5);
    expect(document.querySelector('[data-mining-step-panel="pool"]')).toBeTruthy();
  });

  it("连续点「下一步」可依次到达 5 个步骤面板", () => {
    render(<MiningShell />);
    const next = () => document.querySelector("[data-mining-next]") as HTMLElement;
    for (let i = 0; i < PANELS.length; i += 1) {
      expect(
        document.querySelector(`[data-mining-step-panel="${PANELS[i]}"]`),
      ).toBeTruthy();
      if (i < PANELS.length - 1) fireEvent.click(next());
    }
    // 到达第 5 步后「下一步」禁用
    expect((next() as HTMLButtonElement).disabled).toBe(true);
  });

  it("第 1 步「上一步」禁用", () => {
    render(<MiningShell />);
    expect(
      (document.querySelector("[data-mining-prev]") as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("步骤条可点击跳转", () => {
    render(<MiningShell />);
    fireEvent.click(document.querySelector('[data-mining-step-jump="field"]') as HTMLElement);
    expect(document.querySelector('[data-mining-step-panel="field"]')).toBeTruthy();
    expect(document.querySelector('[data-mining-step-panel="pool"]')).toBeNull();
  });

  it("第 5 步无运行数据时显示空态（不臆造进度）", () => {
    render(<MiningShell />);
    fireEvent.click(document.querySelector('[data-mining-step-jump="run"]') as HTMLElement);
    expect(document.querySelector("[data-mining-run-empty]")).toBeTruthy();
    expect(document.querySelector("[data-run-track]")).toBeNull();
  });

  it("第 5 步有运行数据时渲染进化跟踪", () => {
    render(
      <MiningShell
        runProgress={{
          run_id: "run-1", status: "running", generation: 3, max_generations: 20,
          convergence_threshold: 0.01, converged: false, diversity: 0.5,
          final_validation: null, curve: [],
        }}
      />,
    );
    fireEvent.click(document.querySelector('[data-mining-step-jump="run"]') as HTMLElement);
    expect(document.querySelector("[data-run-track]")).toBeTruthy();
    expect(document.querySelector("[data-mining-run-empty]")).toBeNull();
  });
});
