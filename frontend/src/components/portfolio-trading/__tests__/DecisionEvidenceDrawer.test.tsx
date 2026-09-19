import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntApp } from "antd";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { mockApi, stablePortfolios } = vi.hoisted(() => ({
  mockApi: {
    getDecisionRun: vi.fn(),
    listDecisionRunEvidence: vi.fn(),
  },
  stablePortfolios: [] as any[],
}));

vi.mock("../../../api/client", () => ({
  api: mockApi,
}));

vi.mock("../../../context/AppContext", () => ({
  useApp: () => ({ portfolios: stablePortfolios }),
}));

import DecisionEvidenceDrawer from "../DecisionEvidenceDrawer";

const run = {
  id: "decision-run-copy-test",
  strategy_snapshot_id: "snapshot-1",
  portfolio_id: 7,
  run_type: "backtest",
  trade_date: "2026-08-20",
  decision_at: "2026-08-20T01:00:00Z",
  data_cutoff_at: "2026-08-19T23:59:59Z",
  execution_at: "2026-08-21T01:00:00Z",
  run_mode: "research",
  pit_mode: "strict_pit_safe",
  universe_count: 1,
  member_count: 1,
  score_count_expected: 1,
  score_count_actual: 1,
  score_coverage_pct: 100,
  score_max_age_days: 0,
  blocking_status: "READY",
  blocking_reasons_json: {},
  versions_json: {},
  idempotency_key: null,
  started_at: null,
  finished_at: null,
  duration_ms: 10,
  is_result_production_eligible: false,
  created_at: "2026-08-20T01:00:00Z",
} as any;

const evidence = {
  id: "evidence-selected-1",
  decision_run_id: run.id,
  strategy_snapshot_id: run.strategy_snapshot_id,
  portfolio_id: run.portfolio_id,
  symbol_id: 101,
  trade_date: run.trade_date,
  decision_at: run.decision_at,
  data_cutoff_at: run.data_cutoff_at,
  execution_at: run.execution_at,
  action: "BUY",
  action_subtype: null,
  target_position_pct: 0.1,
  min_lot_size: 100,
  target_quantity: 100,
  intended_price: 10,
  executed_price: null,
  slippage_bps: null,
  rejection_reason: null,
  rejection_detail: null,
  score_id: null,
  score_value: 0.8,
  score_rank: 1,
  score_published_at: null,
  pit_safe_flag: "PIT_SAFE",
  constraints_json: {},
  versions_json: {},
  reason_codes_json: {},
  factor_contributions_json: {},
  legacy_fallback_flag: false,
  stop_loss_verified_price_source: null,
  stop_loss_triggered: false,
  match_mode: "NEXT_OPEN",
  content_hash: "hash-1",
} as any;

const nextEvidence = {
  ...evidence,
  id: "evidence-selected-2",
  symbol_id: 102,
} as any;

function renderDrawer(
  props: Partial<React.ComponentProps<typeof DecisionEvidenceDrawer>> = {},
) {
  const onClose = props.onClose ?? vi.fn();
  const view = render(
    <AntApp>
      <DecisionEvidenceDrawer
        open
        onClose={onClose}
        portfolioId={7}
        initialRun={run}
        initialEvidence={[evidence]}
        {...props}
      />
    </AntApp>,
  );
  return { onClose, ...view };
}

describe("DecisionEvidenceDrawer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.getDecisionRun.mockResolvedValue(run);
    mockApi.listDecisionRunEvidence.mockResolvedValue({ total: 1, items: [evidence] });
  });

  it("closes when Escape is pressed while open", () => {
    const { onClose } = renderDrawer();

    fireEvent.keyDown(document, { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("keeps the closed drawer out of visibility and accessibility hit testing", () => {
    const view = renderDrawer({ open: false });
    const mask = view.container.querySelector(".pt-drawer-mask") as HTMLElement;

    expect(mask).toHaveAttribute("aria-hidden", "true");
    expect(mask.style.visibility).toBe("hidden");
    expect(mask.style.pointerEvents).toBe("none");
  });

  it("copies the selected DecisionEvidence ID from the header", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    renderDrawer({ selectedEvidenceId: evidence.id });

    fireEvent.click(screen.getByRole("button", { name: "复制当前证据 ID" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(evidence.id));
  });

  it("explicitly falls back to the DecisionRun ID when no evidence is selected", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    renderDrawer();

    fireEvent.click(
      screen.getByRole("button", { name: "复制当前 DecisionRun ID（未选择证据）" }),
    );

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(run.id));
  });

  it("shows a visible error when copying the selected evidence ID fails", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("clipboard unavailable"));
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    renderDrawer({ selectedEvidenceId: evidence.id });

    fireEvent.click(screen.getByRole("button", { name: "复制当前证据 ID" }));

    expect(await screen.findByTestId("decision-run-copy-error")).toHaveTextContent(
      "复制当前证据 ID 失败",
    );
  });

  it("marks the selected evidence row for locating a specific record", () => {
    renderDrawer({ selectedEvidenceId: evidence.id });

    const row = screen.getByTestId(`decision-evidence-row-${evidence.id}`);
    expect(row).toHaveAttribute("aria-current", "true");
  });

  it("shows the complete partial-fill execution state for the selected evidence", () => {
    renderDrawer({
      initialEvidence: [{
        ...evidence,
        versions_json: {
          order_plan_status: "PARTIAL_FILL",
          order_plan_requested_quantity: 1000,
          order_plan_filled_quantity: 600,
          order_plan_remaining_quantity: 400,
          order_plan_unfilled_reason: "VOLUME_LIMIT",
        },
      }],
      selectedEvidenceId: evidence.id,
    });

    const execution = screen.getByTestId(`decision-evidence-order-plan-${evidence.id}`);
    expect(execution).toHaveTextContent("计划 1,000");
    expect(execution).toHaveTextContent("成交 600");
    expect(execution).toHaveTextContent("剩余 400");
    expect(execution).toHaveTextContent("VOLUME_LIMIT");
  });

  it("expands persisted rule comparisons and factor contribution evidence", async () => {
    renderDrawer({
      initialEvidence: [{
        ...evidence,
        versions_json: {
          rule_comparisons: [{
            rule_code: "quality_score_floor",
            actual_value: 0.8,
            operator: ">=",
            threshold: 0.7,
            passed: true,
          }],
        },
        factor_contributions_json: [{
          factor: "momentum_20d",
          raw_value: 1.2,
          normalized_value: 0.9,
          weight: 0.4,
          contribution: 0.36,
        }],
      }],
    });

    fireEvent.click(screen.getByTestId(`decision-evidence-expand-${evidence.id}`));

    const comparisons = await screen.findByTestId(`decision-evidence-rule-comparisons-${evidence.id}`);
    expect(comparisons).toHaveTextContent("quality_score_floor");
    expect(comparisons).toHaveTextContent("0.8");
    expect(comparisons).toHaveTextContent(">=");
    expect(comparisons).toHaveTextContent("0.7");
    expect(comparisons).toHaveTextContent("通过");
    expect(screen.getByTestId(`decision-evidence-expanded-${evidence.id}`)).toHaveTextContent("momentum_20d");
    expect(screen.getByTestId(`decision-evidence-expanded-${evidence.id}`)).toHaveTextContent("normalized_value");
    expect(screen.getByTestId(`decision-evidence-expanded-${evidence.id}`)).toHaveTextContent("contribution");
  });

  it("moves the selected evidence with previous/next controls", () => {
    renderDrawer({
      initialEvidence: [evidence, nextEvidence],
      selectedEvidenceId: evidence.id,
    });

    fireEvent.click(screen.getByRole("button", { name: "下一条证据" }));

    expect(screen.getByTestId(`decision-evidence-row-${nextEvidence.id}`)).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(screen.getByTestId(`decision-evidence-row-${evidence.id}`)).not.toHaveAttribute(
      "aria-current",
      "true",
    );
  });

  it("restores focus to the element that opened the drawer", async () => {
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.textContent = "open evidence";
    document.body.appendChild(trigger);
    trigger.focus();

    const onClose = vi.fn();
    const view = render(
      <AntApp>
        <DecisionEvidenceDrawer
          open
          onClose={onClose}
          portfolioId={7}
          initialRun={run}
          initialEvidence={[evidence]}
        />
      </AntApp>,
    );
    await waitFor(() => expect(document.activeElement).toHaveAttribute("aria-label", "关闭抽屉"));
    view.rerender(
      <AntApp>
        <DecisionEvidenceDrawer
          open={false}
          onClose={onClose}
          portfolioId={7}
          initialRun={run}
          initialEvidence={[evidence]}
        />
      </AntApp>,
    );

    await waitFor(() => expect(document.activeElement).toBe(trigger));
    trigger.remove();
  });
});
