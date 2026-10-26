import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    getAIDraft: vi.fn(),
    previewAIDraft: vi.fn(),
    confirmAIDraft: vi.fn(),
    rejectAIDraft: vi.fn(),
    executeAIDraft: vi.fn(),
  },
}));

vi.mock("../../../api/client", () => ({ api: mockApi }));
vi.mock("../../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string) => key,
  enumLabel: (_domain: string, value: string) => value,
}));
vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return {
    ...actual,
    message: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
  };
});

import AIDraftConfirmModal from "../AIDraftConfirmModal";
import ExplanationCard from "../ExplanationCard";
import type { AIDraftDetail, AIResponse } from "../../../types";

function detail(actionType: string, payload: Record<string, unknown>): AIDraftDetail {
  return {
    audit_id: 11,
    message_id: 5,
    action_type: actionType,
    original_suggested_payload: payload,
    current_payload: payload,
    was_modified: false,
    user_confirmed: false,
    confirmed_at: null,
    final_result: null,
    rejected_reason: null,
    created_at: "2026-10-25T10:00:00",
  };
}

const NOTE_PAYLOAD = { watchlist_item_id: 42, note: "估值偏低", tags: ["低估值"], priority: 2 };
const ALERT_PAYLOAD = {
  name: "评分下跌提醒",
  alert_type: "score_drop",
  severity: "warn",
  cooldown_minutes: 60,
  enabled: true,
  config_json: '{"threshold": 40}',
};
const FILTER_PAYLOAD = {
  name: "低估值高股息",
  scope_type: "universe",
  markets: ["cn"],
  boards: [],
  sort_mode: "quality_desc",
  filters_json: "[]",
  description: "稳健筛选",
};

describe("AIDraftConfirmModal（Batch 1：note/alert/filter）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.previewAIDraft.mockResolvedValue({ is_valid: true, errors: [], changes: [] });
    mockApi.confirmAIDraft.mockResolvedValue({ success: true });
    mockApi.executeAIDraft.mockResolvedValue({ success: true, message: "已创建" });
    mockApi.rejectAIDraft.mockResolvedValue({ success: true });
  });

  it("note 草稿渲染观察项字段", async () => {
    mockApi.getAIDraft.mockResolvedValue(detail("draft_note", NOTE_PAYLOAD));
    render(<AIDraftConfirmModal open auditId={11} draftType="draft_note" onClose={() => {}} />);
    expect(await screen.findByTestId("ai-draft-field-watchlist-item")).toBeInTheDocument();
    expect(screen.getByTestId("ai-draft-field-note")).toBeInTheDocument();
    expect(screen.getByTestId("ai-draft-field-tags")).toBeInTheDocument();
    expect(mockApi.getAIDraft).toHaveBeenCalledWith(11);
  });

  it("alert 草稿渲染告警字段", async () => {
    mockApi.getAIDraft.mockResolvedValue(detail("draft_alert", ALERT_PAYLOAD));
    render(<AIDraftConfirmModal open auditId={11} draftType="draft_alert" onClose={() => {}} />);
    expect(await screen.findByTestId("ai-draft-field-alert-name")).toBeInTheDocument();
    expect(screen.getByTestId("ai-draft-field-alert-type")).toBeInTheDocument();
    expect(screen.getByTestId("ai-draft-field-severity")).toBeInTheDocument();
    expect(screen.getByTestId("ai-draft-field-config-json")).toBeInTheDocument();
  });

  it("filter 草稿渲染筛选字段", async () => {
    mockApi.getAIDraft.mockResolvedValue(detail("draft_filter", FILTER_PAYLOAD));
    render(<AIDraftConfirmModal open auditId={11} draftType="draft_filter" onClose={() => {}} />);
    expect(await screen.findByTestId("ai-draft-field-filter-name")).toBeInTheDocument();
    expect(screen.getByTestId("ai-draft-field-scope-type")).toBeInTheDocument();
    expect(screen.getByTestId("ai-draft-field-sort-mode")).toBeInTheDocument();
    expect(screen.getByTestId("ai-draft-field-filters-json")).toBeInTheDocument();
  });

  it("确认执行按 preview → confirm → execute 顺序调用", async () => {
    const onExecuted = vi.fn();
    const onClose = vi.fn();
    mockApi.getAIDraft.mockResolvedValue(detail("draft_alert", ALERT_PAYLOAD));
    const user = userEvent.setup();
    render(
      <AIDraftConfirmModal open auditId={11} draftType="draft_alert" onClose={onClose} onExecuted={onExecuted} />,
    );

    const executeBtn = await screen.findByTestId("ai-draft-execute");
    await user.click(executeBtn);

    await waitFor(() => {
      expect(mockApi.executeAIDraft).toHaveBeenCalledWith(11);
    });
    const previewOrder = mockApi.previewAIDraft.mock.invocationCallOrder[0];
    const confirmOrder = mockApi.confirmAIDraft.mock.invocationCallOrder[0];
    const executeOrder = mockApi.executeAIDraft.mock.invocationCallOrder[0];
    expect(previewOrder).toBeLessThan(confirmOrder);
    expect(confirmOrder).toBeLessThan(executeOrder);
    expect(onExecuted).toHaveBeenCalledWith(expect.objectContaining({ success: true }));
    expect(onClose).toHaveBeenCalled();
  });

  it("预览不通过时阻止执行", async () => {
    const onExecuted = vi.fn();
    mockApi.getAIDraft.mockResolvedValue(detail("draft_note", NOTE_PAYLOAD));
    mockApi.previewAIDraft.mockResolvedValue({
      is_valid: false,
      errors: ["watchlist_item_id 不存在"],
      changes: [],
    });
    const user = userEvent.setup();
    render(
      <AIDraftConfirmModal open auditId={11} draftType="draft_note" onClose={() => {}} onExecuted={onExecuted} />,
    );

    await user.click(await screen.findByTestId("ai-draft-execute"));
    await waitFor(() => {
      expect(mockApi.previewAIDraft).toHaveBeenCalled();
    });
    expect(mockApi.confirmAIDraft).not.toHaveBeenCalled();
    expect(mockApi.executeAIDraft).not.toHaveBeenCalled();
    expect(onExecuted).not.toHaveBeenCalled();
    expect(await screen.findByText("watchlist_item_id 不存在")).toBeInTheDocument();
  });

  it("拒绝走 reject 接口并带原因", async () => {
    mockApi.getAIDraft.mockResolvedValue(detail("draft_filter", FILTER_PAYLOAD));
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<AIDraftConfirmModal open auditId={11} draftType="draft_filter" onClose={onClose} />);

    await user.click(await screen.findByTestId("ai-draft-reject"));
    await waitFor(() => {
      expect(mockApi.rejectAIDraft).toHaveBeenCalledWith(11, "aiDraftRejectedByUser");
    });
    expect(onClose).toHaveBeenCalled();
  });

  it("审计目标类型不匹配时拒绝加载", async () => {
    mockApi.getAIDraft.mockResolvedValue(detail("draft_factor", { code: "x" }));
    render(<AIDraftConfirmModal open auditId={11} draftType="draft_note" onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText("aiDraftLoadFailed")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("ai-draft-execute")).not.toBeInTheDocument();
  });

  it("缺 auditId 时只读展示并隐去执行按钮", async () => {
    render(
      <AIDraftConfirmModal open auditId={null} draftType="draft_note" suggestedPayload={NOTE_PAYLOAD} onClose={() => {}} />,
    );
    expect(await screen.findByText("aiDraftAuditMissing")).toBeInTheDocument();
    expect(screen.queryByTestId("ai-draft-execute")).not.toBeInTheDocument();
    expect(screen.getByTestId("ai-draft-preview")).toBeDisabled();
  });
});

describe("ExplanationCard 草稿入口", () => {
  const baseResponse: AIResponse = {
    answer: "已生成草稿",
    evidence: [],
    warnings: [],
    suggested_actions: [],
    draft: null,
    metadata: {},
  };

  it("note 草稿显示「查看并确认」入口", () => {
    render(
      <ExplanationCard
        response={{
          ...baseResponse,
          draft: { draft_type: "draft_note", audit_id: 11, ...NOTE_PAYLOAD },
        }}
      />,
    );
    expect(screen.getByTestId("ai-draft-open-confirm")).toBeInTheDocument();
  });

  it("未开放类型显示不支持提示（order 延后）", () => {
    render(
      <ExplanationCard
        response={{
          ...baseResponse,
          draft: { draft_type: "draft_order", suggested_payload: {} },
        }}
      />,
    );
    expect(screen.getByText("aiDraftUnsupported")).toBeInTheDocument();
    expect(screen.queryByTestId("ai-draft-open-confirm")).not.toBeInTheDocument();
  });

  it("因子草稿保持原有「应用到草稿」路径", () => {
    render(
      <ExplanationCard
        response={{
          ...baseResponse,
          draft: { draft_type: "draft_factor", suggested_payload: { code: "f1" } },
        }}
      />,
    );
    expect(screen.getByText("aiDraftApplyToEditor")).toBeInTheDocument();
    expect(screen.queryByTestId("ai-draft-open-confirm")).not.toBeInTheDocument();
  });
});
