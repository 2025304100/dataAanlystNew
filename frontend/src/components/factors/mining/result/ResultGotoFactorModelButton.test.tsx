import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@testing-library/react";

/**
 * C1：结果页 → 因子模型页跳转按钮（4 上下文门控 + settings:navigate 复用）。
 */
const navigateMock = vi.hoisted(() => vi.fn());
vi.mock("../../../../utils/navigate", () => ({
  navigate: navigateMock,
}));

import ResultGotoFactorModelButton from "./ResultGotoFactorModelButton";

const FULL_CTX = {
  factor_set_id: "fs-1",
  data_cutoff_at: "2026-09-19T00:00:00",
  candidate_pool_snapshot_id: "snap-1",
  rebalance_frequency: "weekly",
};

describe("ResultGotoFactorModelButton（C1）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("4 上下文齐备 → 可点击并跳转因子模型页", () => {
    render(<ResultGotoFactorModelButton context={FULL_CTX} />);
    const btn = document.querySelector("[data-result-goto-model]") as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
    fireEvent.click(btn);
    const url = new URL(navigateMock.mock.calls[0][0], "http://localhost");
    expect(url.pathname).toBe("/factors");
    expect(url.searchParams.get("set_id")).toBe("fs-1");
    expect(url.searchParams.get("data_cutoff_at")).toBe(FULL_CTX.data_cutoff_at);
    expect(url.searchParams.get("candidate_pool_snapshot_id")).toBe("snap-1");
    expect(url.searchParams.get("rebalance_frequency")).toBe("weekly");
  });

  it("缺任一上下文（如 factor_set_id）→ 禁用且不跳转", () => {
    render(<ResultGotoFactorModelButton context={{ ...FULL_CTX, factor_set_id: null }} />);
    const btn = document.querySelector("[data-result-goto-model]") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    fireEvent.click(btn);
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("无上下文 → 禁用", () => {
    render(<ResultGotoFactorModelButton />);
    expect(
      (document.querySelector("[data-result-goto-model]") as HTMLButtonElement).disabled,
    ).toBe(true);
  });
});
