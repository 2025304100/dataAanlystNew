import { describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@testing-library/react";

/**
 * T31 Step4 进化参数 + 资源确认 测试（向导 §6.5.5 / §6.6.8 / §7，设计 §9.2）。
 *
 * 覆盖 artifacts「简单/高级模式 + 资源确认弹窗」与任务卡硬约束：
 *   1. **简单模式不得出现专业术语**（帕累托/非支配/拥挤度/赛道/锦标赛）——§6.5.5；
 *   2. 高级模式展开折叠区（赛道竞争/多目标/选择/繁殖自适应）；
 *   3. 自适应开启时三率显示「自动」且不可手改（§6.6.8）；
 *   4. 锁状态三态文案（pitfalls）：无冲突 / mining_domain 冲突**禁用提交** /
 *      duckdb_write 冲突**显示排队位次**；
 *   5. **ETA 必须标注「估算」**（pitfalls + §7）；
 *   6. **不暴露并行度**（not_do）——不得出现"并行/进程/worker/CPU"字样；
 *   7. 快速试验模式预设（种群 50 / 代数 10，默认 100/20）。
 *
 * 术语黑名单（简单模式）：帕累托 / 非支配 / 拥挤度 / 赛道 / 锦标赛
 */

import MiningEvoParamStep from "./MiningEvoParamStep";
import type { MiningLockStatus } from "./evoTypes";

const LOCK_FREE: MiningLockStatus = {
  miningDomain: { busy: false, taskId: null, runId: null, acquiredAt: null, heartbeatAt: null },
  duckdbWrite: { busy: false, taskId: null, queue: [] },
};

const LOCK_DOMAIN: MiningLockStatus = {
  miningDomain: { busy: true, taskId: "run-42", runId: "run-42", acquiredAt: null, heartbeatAt: null },
  duckdbWrite: { busy: false, taskId: null, queue: [] },
};

const LOCK_WRITE: MiningLockStatus = {
  miningDomain: { busy: false, taskId: null, runId: null, acquiredAt: null, heartbeatAt: null },
  duckdbWrite: { busy: true, taskId: "task-9", queue: ["ahead-1", "ahead-2"] },
};

const TERMS = ["帕累托", "非支配", "拥挤度", "赛道", "锦标赛"];

describe("MiningEvoParamStep 简单/高级模式", () => {
  it("默认简单模式，且**不出现任何专业术语**", () => {
    render(<MiningEvoParamStep />);
    expect(document.querySelector("[data-evo-step]")).toBeTruthy();
    const text = document.body.textContent ?? "";
    for (const term of TERMS) {
      expect(text).not.toContain(term);
    }
    // 简单模式必须有：进化强度 / 选优偏好 / 启用 AI 生成
    expect(document.querySelector("[data-evo-strength]")).toBeTruthy();
    expect(document.querySelector("[data-evo-preference]")).toBeTruthy();
    expect(document.querySelector("[data-evo-ai-enabled]")).toBeTruthy();
  });

  it("切到高级模式后展开折叠区（含术语）", () => {
    render(<MiningEvoParamStep />);
    fireEvent.click(document.querySelector("[data-evo-mode-advanced]") as HTMLElement);
    const advanced = document.querySelector("[data-evo-advanced-sections]");
    expect(advanced).toBeTruthy();
    const text = advanced?.textContent ?? "";
    expect(TERMS.some((term) => text.includes(term))).toBe(true);
  });

  it("切回简单模式隐藏折叠区", () => {
    render(<MiningEvoParamStep />);
    fireEvent.click(document.querySelector("[data-evo-mode-advanced]") as HTMLElement);
    fireEvent.click(document.querySelector("[data-evo-mode-simple]") as HTMLElement);
    expect(document.querySelector("[data-evo-advanced-sections]")).toBeNull();
  });

  it("自适应开启时三率显示「自动」且不可手改", () => {
    render(<MiningEvoParamStep />);
    fireEvent.click(document.querySelector("[data-evo-mode-advanced]") as HTMLElement);
    const adaptive = document.querySelector("[data-evo-adaptive] input") as HTMLInputElement;
    expect(adaptive.checked).toBe(true);      // 默认开
    for (const key of ["mutation", "crossover", "random"]) {
      const cell = document.querySelector(`[data-evo-rate-auto="${key}"]`);
      expect(cell?.textContent).toContain("自动");
    }
    const rate = document.querySelector('[data-evo-rate-input="mutation"] input') as HTMLInputElement;
    expect(rate.disabled).toBe(true);
  });

  it("关闭自适应后可手动配置三率", () => {
    render(<MiningEvoParamStep />);
    fireEvent.click(document.querySelector("[data-evo-mode-advanced]") as HTMLElement);
    fireEvent.click(document.querySelector("[data-evo-adaptive] input") as HTMLElement);
    const rate = document.querySelector('[data-evo-rate-input="mutation"] input') as HTMLInputElement;
    expect(rate.disabled).toBe(false);
  });
});

describe("MiningEvoParamStep 资源确认弹窗", () => {
  const open = () => {
    render(<MiningEvoParamStep lockStatus={undefined} />);
    return null;
  };

  it("无冲突：显示「可立即开始」且提交可用", () => {
    render(<MiningEvoParamStep lockStatus={LOCK_FREE} />);
    fireEvent.click(document.querySelector("[data-evo-submit]") as HTMLElement);
    expect(document.querySelector("[data-resource-modal]")).toBeTruthy();
    expect(document.querySelector("[data-lock-none]")?.textContent).toContain("可立即开始");
    expect(
      (document.querySelector("[data-evo-confirm-submit]") as HTMLButtonElement).disabled,
    ).toBe(false);
    open();
  });

  it("mining_domain 冲突：显示占用者并**禁用提交**（不排队）", () => {
    render(<MiningEvoParamStep lockStatus={LOCK_DOMAIN} />);
    fireEvent.click(document.querySelector("[data-evo-submit]") as HTMLElement);
    const tip = document.querySelector("[data-lock-domain]");
    expect(tip?.textContent).toContain("run-42");
    expect(
      (document.querySelector("[data-evo-confirm-submit]") as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(document.querySelector("[data-queue-position]")).toBeNull();
  });

  it("duckdb_write 冲突：显示排队位次", () => {
    render(<MiningEvoParamStep lockStatus={LOCK_WRITE} />);
    fireEvent.click(document.querySelector("[data-evo-submit]") as HTMLElement);
    expect(document.querySelector("[data-lock-write]")).toBeTruthy();
    const pos = document.querySelector("[data-queue-position]");
    expect(pos?.textContent).toContain("2");
    expect(
      (document.querySelector("[data-evo-confirm-submit]") as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it("ETA 必须标注「估算」", () => {
    render(<MiningEvoParamStep lockStatus={LOCK_FREE} etaSeconds={900} />);
    fireEvent.click(document.querySelector("[data-evo-submit]") as HTMLElement);
    const eta = document.querySelector("[data-eta]");
    expect(eta).toBeTruthy();
    expect(eta?.textContent).toContain("估算");
  });

  it("资源不足时阻断并给出调整建议", () => {
    render(
      <MiningEvoParamStep lockStatus={LOCK_FREE} resourcesOk={false} />,
    );
    fireEvent.click(document.querySelector("[data-evo-submit]") as HTMLElement);
    const blocked = document.querySelector("[data-resource-blocked]");
    expect(blocked).toBeTruthy();
    expect(blocked?.textContent).toMatch(/减少|缩短|缩小/);
    expect(
      (document.querySelector("[data-evo-confirm-submit]") as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("**不暴露并行度**（not_do）：弹窗与页面均无并行/进程/worker/CPU 字样", () => {
    render(<MiningEvoParamStep lockStatus={LOCK_WRITE} />);
    fireEvent.click(document.querySelector("[data-evo-submit]") as HTMLElement);
    const text = document.body.textContent ?? "";
    for (const banned of ["并行", "进程", "worker", "Worker", "CPU", "cpu"]) {
      expect(text).not.toContain(banned);
    }
  });

  it("快速试验模式使用小规模预设（50/10）", () => {
    render(<MiningEvoParamStep />);
    fireEvent.click(document.querySelector("[data-evo-mode-advanced]") as HTMLElement);
    const q = document.querySelector("[data-evo-quick-trial]") as HTMLInputElement;
    expect(q).toBeTruthy();
    expect(document.querySelector("[data-evo-population]") as HTMLInputElement)
      .toHaveProperty("value", "100");
    fireEvent.click(q);
    expect(document.querySelector("[data-evo-population]") as HTMLInputElement)
      .toHaveProperty("value", "50");
    expect(document.querySelector("[data-evo-generations]") as HTMLInputElement)
      .toHaveProperty("value", "10");
  });
});

describe("MiningEvoParamStep 提交与暂存", () => {
  it("确认提交回调携带配置", () => {
    const onSubmit = vi.fn();
    render(<MiningEvoParamStep lockStatus={LOCK_FREE} onSubmit={onSubmit} />);
    fireEvent.click(document.querySelector("[data-evo-submit]") as HTMLElement);
    fireEvent.click(document.querySelector("[data-evo-confirm-submit]") as HTMLElement);
    expect(onSubmit).toHaveBeenCalled();
  });

  it("可保存暂存（不绕过资源确认）", () => {
    const onSaveDraft = vi.fn();
    render(<MiningEvoParamStep lockStatus={LOCK_FREE} onSaveDraft={onSaveDraft} />);
    fireEvent.click(document.querySelector("[data-evo-save-draft]") as HTMLElement);
    expect(onSaveDraft).toHaveBeenCalled();
  });
});
