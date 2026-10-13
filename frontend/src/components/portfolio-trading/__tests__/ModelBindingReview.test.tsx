import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import ModelBindingReview from "../ModelBindingReview";
import { renderWithApp } from "../../../test/renderWithApp";
import { api } from "../../../api/client";
import type { ModelBindingReviewResponse } from "../../../types";

vi.mock("../../../api/client", () => ({ api: {
  getPortfolioModelBindingReview: vi.fn(), confirmPortfolioModelBinding: vi.fn(),
} }));

const REVIEW: ModelBindingReviewResponse = {
  portfolio_id: 7, snapshot_id: "snapshot-new", model_run_id: "model-new", model_name: "新模型",
  change_summary: "模型与策略已变更，需要本人接受", can_confirm: true,
  requires_confirmation: true, first_confirmation: false, blockers: [],
  before: { snapshot_id: "snapshot-old", model_run_id: "model-old", model_name: "旧模型", snapshot_no: 1, rule_version: 1 },
  after: { snapshot_id: "snapshot-new", model_run_id: "model-new", model_name: "新模型", snapshot_no: 2, rule_version: 1 },
};

function renderReview() {
  const onResolved = vi.fn();
  const onNavigate = vi.fn();
  renderWithApp(<ModelBindingReview portfolioId={7} onResolved={onResolved} onNavigate={onNavigate} />,
    { contextValue: { showToast: vi.fn() } });
  return { onResolved, onNavigate };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getPortfolioModelBindingReview).mockResolvedValue(REVIEW);
});

describe("model binding review", () => {
  it("shows old and new bindings, and requires acknowledgement before confirming the reviewed pair", async () => {
    vi.mocked(api.confirmPortfolioModelBinding).mockResolvedValue({ model_confirmed: true } as never);
    const { onResolved } = renderReview();
    await screen.findByText(/新模型/);
    expect(screen.getByText(/旧模型/)).toBeTruthy();
    expect(screen.getByText(/已应用策略: 版本 1/)).toBeTruthy();
    expect(screen.getByText(/已应用策略: 版本 2/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "确认接受变更" })).toBeDisabled();
    fireEvent.change(screen.getByPlaceholderText("请填写不少于 10 字的审查备注"), { target: { value: "已查看新旧绑定并确认接受本次变更" } });
    expect(screen.getByRole("button", { name: "确认接受变更" })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "确认接受变更" }));
    await waitFor(() => expect(api.confirmPortfolioModelBinding).toHaveBeenCalledWith(7, {
      ack: true, snapshot_id: "snapshot-new", model_run_id: "model-new", review_note: "已查看新旧绑定并确认接受本次变更",
    }));
    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1));
  });

  it("offers strategy repair instead of confirmation when the binding is invalid", async () => {
    vi.mocked(api.getPortfolioModelBindingReview).mockResolvedValue({ ...REVIEW, can_confirm: false, blockers: ["策略尚未应用"] });
    const { onNavigate } = renderReview();
    await screen.findByText("策略尚未应用");
    expect(screen.queryByRole("button", { name: "确认接受变更" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "前往组合策略" }));
    expect(onNavigate).toHaveBeenCalledWith("strategy");
    expect(api.confirmPortfolioModelBinding).not.toHaveBeenCalled();
  });

  it("reloads a changed snapshot and requires a fresh acknowledgement after a conflict", async () => {
    vi.mocked(api.confirmPortfolioModelBinding).mockRejectedValue({ status_code: 409, message: "INTERNAL_ENUM" });
    const { onResolved } = renderReview();
    await screen.findByText(/新模型/);
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.change(screen.getByPlaceholderText("请填写不少于 10 字的审查备注"), { target: { value: "已查看新旧绑定并确认接受本次变更" } });
    vi.mocked(api.getPortfolioModelBindingReview).mockResolvedValue({ ...REVIEW, snapshot_id: "snapshot-later" });
    fireEvent.click(screen.getByRole("button", { name: "确认接受变更" }));
    await screen.findByText("绑定已发生变化，请重新查看摘要后确认");
    await waitFor(() => expect(screen.getByRole("checkbox")).not.toBeChecked());
    expect(screen.getByRole("button", { name: "确认接受变更" })).toBeDisabled();
    expect(onResolved).not.toHaveBeenCalled();
    expect(screen.queryByText("INTERNAL_ENUM")).toBeNull();
  });
});
