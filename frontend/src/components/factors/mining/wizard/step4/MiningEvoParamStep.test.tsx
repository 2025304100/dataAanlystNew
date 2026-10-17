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

describe("MiningEvoParamStep 配置回显与提交一致", () => {
  it("手动繁殖比例合计无效时说明原因且不允许提交，修正后可继续", () => {
    const { getByRole, queryByRole } = render(<MiningEvoParamStep lockStatus={LOCK_FREE}
      initial={{ simple_mode: false, adaptive: false }} />);
    const mutation = document.querySelector('[data-evo-rate-input="mutation"] input') as HTMLInputElement;
    const random = document.querySelector('[data-evo-rate-input="random"] input') as HTMLInputElement;
    fireEvent.change(mutation, { target: { value: "0.60" } });
    fireEvent.blur(mutation);
    expect(getByRole("alert")).toHaveTextContent(/100%/);
    expect(document.querySelector("[data-evo-submit]")).toBeDisabled();
    expect(document.querySelector("[data-evo-save-draft]")).not.toBeDisabled();
    fireEvent.change(random, { target: { value: "0.15" } });
    fireEvent.blur(random);
    expect(queryByRole("alert")).toBeNull();
    expect(document.querySelector("[data-evo-submit]")).not.toBeDisabled();
  });
  it("简单模式未实现的选优偏好不可选择，但强度和AI开关仍可正常修改", () => {
    const onConfig = vi.fn();
    const { getByRole, queryByRole } = render(<MiningEvoParamStep onConfig={onConfig} />);
    const preference = getByRole("combobox");
    expect(preference).toBeDisabled();
    expect(document.querySelector("[data-evo-preference-unavailable-hint]")).toBeTruthy();
    fireEvent.mouseDown(preference);
    expect(queryByRole("option")).toBeNull();
    expect(onConfig).not.toHaveBeenCalled();
    fireEvent.keyDown(getByRole("slider"), { key: "ArrowLeft", keyCode: 37 });
    fireEvent.click(getByRole("switch"));
    expect(onConfig.mock.lastCall?.[0]).toMatchObject({
      preference: "balanced", strength: "conservative", max_generations: 5, ai_enabled: false,
    });
  });

  it("修改高级选择参数后，输入框和上报配置保持同一个值", () => {
    const onConfig = vi.fn();
    render(<MiningEvoParamStep onConfig={onConfig} />);
    fireEvent.click(document.querySelector("[data-evo-mode-advanced]") as HTMLElement);
    const tournament = document.querySelector("[data-evo-tournament-k]") as HTMLInputElement;
    fireEvent.change(tournament, { target: { value: "7" } });
    fireEvent.blur(tournament);
    expect(tournament).toHaveValue("7");
    expect(onConfig.mock.lastCall?.[0]).toMatchObject({ tournament_k: 7 });
  });

  it("重新载入草稿替换旧类别配置，后续编辑保留新草稿的全部类别", () => {
    const onConfig = vi.fn();
    const { rerender } = render(<MiningEvoParamStep onConfig={onConfig} initial={{
      simple_mode: false, category_limits: { quality: 2 },
    }} />);
    rerender(<MiningEvoParamStep onConfig={onConfig} initial={{
      simple_mode: false, category_limits: { trend: 7, reversal: 4 },
    }} />);
    expect(document.querySelector('[data-evo-category-limit="trend"]')).toHaveValue("7");
    expect(document.querySelector('[data-evo-category-limit="reversal"]')).toHaveValue("4");
    expect(document.querySelector('[data-evo-category-limit="quality"]')).toHaveValue("0");
    fireEvent.change(document.querySelector("[data-evo-tournament-k]") as HTMLInputElement, { target: { value: "7" } });
    expect(onConfig.mock.lastCall?.[0]).toMatchObject({ category_limits: { trend: 7, reversal: 4 } });
    expect(onConfig.mock.lastCall?.[0].category_limits).not.toHaveProperty("quality");
  });

  it("载入草稿展示完整高级配置，修改基础参数仍携带草稿高级值", () => {
    const onConfig = vi.fn();
    render(<MiningEvoParamStep onConfig={onConfig} initial={{
      simple_mode: false,
      adaptive: false,
      tournament_k: 6,
      ai_ratio: 0.7,
      random_ratio: 0.3,
      track_enabled: false,
      track_floor_ratio: 0.4,
      weak_generations: 5,
      cross_category_ratio: 0.35,
      mutation_rate: 0.5,
      crossover_rate: 0.35,
      random_rate: 0.15,
      category_limits: { trend: 7, quality: 4 },
      coverage_strategy: "drop",
    }} />);
    for (const [selector, value] of [
      ["[data-evo-tournament-k]", "6"],
      ["[data-evo-ai-ratio]", "0.7"],
      ["[data-evo-random-ratio]", "0.3"],
      ["[data-evo-track-floor]", "0.4"],
      ["[data-evo-weak-generations]", "5"],
      ["[data-evo-cross-category]", "0.35"],
      ['[data-evo-rate-input="mutation"] input', "0.5"],
      ['[data-evo-rate-input="crossover"] input', "0.35"],
      ['[data-evo-rate-input="random"] input', "0.15"],
      ['[data-evo-category-limit="trend"]', "7"],
      ['[data-evo-category-limit="quality"]', "4"],
    ]) expect(Number((document.querySelector(selector) as HTMLInputElement).value)).toBe(Number(value));
    expect(document.querySelector('[data-evo-track-enabled] input')).not.toBeChecked();
    expect(document.querySelector('[data-evo-coverage-strategy] input[value="drop"]')).toBeChecked();
    fireEvent.change(document.querySelector("[data-evo-population]") as HTMLInputElement, { target: { value: "120" } });
    expect(onConfig.mock.lastCall?.[0]).toMatchObject({
      population_size: 120,
      tournament_k: 6,
      ai_ratio: 0.7,
      random_ratio: 0.3,
      track_enabled: false,
      track_floor_ratio: 0.4,
      weak_generations: 5,
      cross_category_ratio: 0.35,
      mutation_rate: 0.5,
      crossover_rate: 0.35,
      random_rate: 0.15,
      category_limits: { trend: 7, quality: 4 },
      coverage_strategy: "drop",
    });
  });

  it("未接入执行的选项只读，自适应关闭后才允许调整跨类别比例", () => {
    const onConfig = vi.fn();
    render(<MiningEvoParamStep onConfig={onConfig} initial={{ simple_mode: false }} />);
    for (const selector of [
      '[data-evo-category-limit="trend"]',
      '[data-evo-coverage-strategy] input[value="drop"]',
      "[data-evo-ai-ratio]", "[data-evo-random-ratio]",
      '[data-evo-explore-preset] input[value="balanced"]',
      "[data-evo-track-enabled] input", "[data-evo-track-floor]",
      "[data-evo-weak-generations]", "[data-evo-cross-category]",
    ]) expect(document.querySelector(selector)).toBeDisabled();
    expect(document.querySelector("[data-evo-unsupported-hint]")).toBeTruthy();
    expect(document.querySelector("[data-evo-cross-category-auto-hint]")).toBeTruthy();
    expect(document.querySelector("[data-evo-tournament-k]")).not.toBeDisabled();
    fireEvent.click(document.querySelector("[data-evo-adaptive] input") as HTMLElement);
    expect(document.querySelector("[data-evo-cross-category]")).not.toBeDisabled();
    fireEvent.change(document.querySelector("[data-evo-cross-category]") as HTMLInputElement, { target: { value: "0.35" } });
    expect(Number((document.querySelector("[data-evo-cross-category]") as HTMLInputElement).value)).toBe(0.35);
    expect(onConfig.mock.lastCall?.[0]).toMatchObject({ adaptive: false, cross_category_ratio: 0.35 });
  });

  it("简单模式调整强度后，高级模式的代数和后续回调都使用映射值", () => {
    const onConfig = vi.fn();
    const { getByRole } = render(<MiningEvoParamStep onConfig={onConfig} />);
    fireEvent.keyDown(getByRole("slider"), { key: "ArrowLeft", keyCode: 37 });
    expect(onConfig.mock.lastCall?.[0]).toMatchObject({ strength: "conservative", max_generations: 5 });
    fireEvent.click(document.querySelector("[data-evo-mode-advanced]") as HTMLElement);
    expect(document.querySelector("[data-evo-generations]")).toHaveValue("5");
    expect(onConfig.mock.lastCall?.[0]).toMatchObject({ strength: "conservative", max_generations: 5 });
  });

  it("快速试验提供明确应用动作，并把页面与回调同时设置为50/10", () => {
    const onConfig = vi.fn();
    render(<MiningEvoParamStep onConfig={onConfig} initial={{ simple_mode: false, tournament_k: 6 }} />);
    const apply = document.querySelector("[data-evo-quick-trial]") as HTMLButtonElement;
    expect(apply.tagName).toBe("BUTTON");
    expect(apply.getAttribute("role")).not.toBe("switch");
    fireEvent.click(apply);
    expect(document.querySelector("[data-evo-population]")).toHaveValue("50");
    expect(document.querySelector("[data-evo-generations]")).toHaveValue("10");
    expect(onConfig.mock.lastCall?.[0]).toMatchObject({ population_size: 50, max_generations: 10, tournament_k: 6 });
    fireEvent.change(document.querySelector("[data-evo-generations]") as HTMLInputElement, { target: { value: "12" } });
    fireEvent.click(apply);
    expect(document.querySelector("[data-evo-generations]")).toHaveValue("10");
    expect(onConfig.mock.lastCall?.[0]).toMatchObject({ population_size: 50, max_generations: 10, tournament_k: 6 });
  });
});
