import React from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import FactorPipelineWizardNav, { type FactorPipelineWizardNavProps } from "../FactorPipelineWizardNav";

const steps: FactorPipelineWizardNavProps["steps"] = [
  { id: "prepare", title: "准备数据", state: "complete" },
  { id: "train", title: "训练模型", state: "current" },
  { id: "review", title: "验证与激活", state: "blocked" },
  { id: "score", title: "生成评分", state: "waiting" },
];

describe("FactorPipelineWizardNav", () => {
  it("marks the displayed page as current independently of business readiness", () => {
    render(<FactorPipelineWizardNav step="review" steps={steps} onSelect={vi.fn()} />);

    const nav = screen.getByRole("navigation", { name: "因子流水线步骤" });
    const currentButtons = within(nav).getAllByRole("button", { current: "step" });
    expect(currentButtons).toHaveLength(1);
    expect(currentButtons[0]).toHaveAccessibleName("第 3 步：验证与激活");
    expect(screen.getByRole("button", { name: "第 2 步：训练模型" })).not.toHaveAttribute("aria-current");
    expect(nav).toHaveTextContent("第 3 步 / 共 4 步");
    expect(currentButtons[0]).toHaveTextContent("需处理");
  });

  it("lets users inspect a completed or waiting step while a task is busy", () => {
    const onSelect = vi.fn();
    render(<FactorPipelineWizardNav step="train" steps={steps} onSelect={onSelect} busy />);

    fireEvent.click(screen.getByRole("button", { name: "第 1 步：准备数据" }));
    fireEvent.click(screen.getByRole("button", { name: "第 4 步：生成评分" }));

    expect(onSelect.mock.calls).toEqual([["prepare"], ["score"]]);
    expect(screen.getByRole("navigation", { name: "因子流水线步骤" })).toHaveAttribute("aria-busy", "true");
  });

  it("supports selecting a step through standard keyboard button interaction", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<FactorPipelineWizardNav step="train" steps={steps} onSelect={onSelect} />);

    await user.tab();
    expect(screen.getByRole("button", { name: "第 1 步：准备数据" })).toHaveFocus();
    await user.keyboard("{Enter}");

    expect(onSelect).toHaveBeenCalledOnce();
    expect(onSelect).toHaveBeenCalledWith("prepare");
  });
});
