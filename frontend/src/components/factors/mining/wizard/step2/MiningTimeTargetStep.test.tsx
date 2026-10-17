import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import MiningTimeTargetStep from "./MiningTimeTargetStep";

describe("MiningTimeTargetStep 执行能力", () => {
  it("仅支持简单收益目标，未接入执行的目标明确禁用", () => {
    const onChange = vi.fn();
    render(<MiningTimeTargetStep onChange={onChange} />);
    fireEvent.mouseDown(document.querySelector("[data-time-target-type] .ant-select-selector") as HTMLElement);
    for (const label of ["对数收益", "基准超额收益", "截面排名目标"]) {
      const option = screen.getByTitle(label).closest(".ant-select-item-option");
      expect(option).toBeTruthy();
      fireEvent.click(option as HTMLElement);
      expect(onChange).not.toHaveBeenCalled();
    }
    expect(document.querySelector("[data-time-target-supported-only]")).toBeTruthy();
  });

  it("本人点击后使用已有行情范围并按数据截止日截断，草稿不会自动被覆盖", () => {
    const onChange = vi.fn();
    render(<MiningTimeTargetStep
      mirroredFrom="2025-01-13"
      mirroredTo="2026-10-14"
      dataCutoffAt="2026-10-12T15:00:00+08:00"
      initial={{ start_date: "2025-06-01", end_date: "2026-06-30", rebalance_frequency: "weekly", target_horizon: 10 }}
      onChange={onChange}
    />);
    const start = document.querySelector("[data-time-start]") as HTMLInputElement;
    const end = document.querySelector("[data-time-end]") as HTMLInputElement;
    expect(start.value).toBe("2025-06-01");
    expect(end.value).toBe("2026-06-30");
    expect(onChange).not.toHaveBeenCalled();
    const useRange = document.querySelector("[data-time-use-mirror-range]") as HTMLButtonElement;
    expect(useRange).toBeTruthy();
    fireEvent.click(useRange);
    expect(start.value).toBe("2025-01-13");
    expect(end.value).toBe("2026-10-12");
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ start_date: "2025-01-13", end_date: "2026-10-12", rebalance_frequency: "weekly", target_horizon: 10 }));
  });

  it("镜像结束早于数据截止日时保留镜像结束日期", () => {
    const onChange = vi.fn();
    render(<MiningTimeTargetStep mirroredFrom="2025-01-13" mirroredTo="2026-10-14" dataCutoffAt="2026-10-15" onChange={onChange} />);
    fireEvent.click(document.querySelector("[data-time-use-mirror-range]") as HTMLElement);
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ start_date: "2025-01-13", end_date: "2026-10-14" }));
  });

  it.each([
    [null, "2026-10-14", "2026-10-14"],
    ["2025-01-13", null, "2026-10-14"],
    ["2025-01-13", "2026-10-14", "2025-01-12"],
    ["2026-10-15", "2026-10-14", "2026-10-15"],
    ["2025-02-30", "2026-10-14", "2026-10-14"],
    ["2025-01-13", "2026-10-14", "invalid"],
  ])("镜像范围不完整或无效时按钮禁用：%s→%s，截止%s", (mirroredFrom, mirroredTo, cutoff) => {
    const onChange = vi.fn();
    render(<MiningTimeTargetStep mirroredFrom={mirroredFrom} mirroredTo={mirroredTo} dataCutoffAt={cutoff} onChange={onChange} />);
    const button = document.querySelector("[data-time-use-mirror-range]") as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    expect(onChange).not.toHaveBeenCalled();
  });
});
