// WP5.1：SymbolRelationshipBar 集成测试
//
// 覆盖：
// - symbolId 为 null 时不渲染 OpportunityStatusBadges（包装层降级）
// - symbolId 有效时渲染 OpportunityStatusBadges 并透传 props
// - 点击徽标回调 onOpenDetail 透传
//
// 约束：
// - 不调用真实后端，所有 API 通过 vi.fn mock
// - 复用 OpportunityStatusBadges 已有的 hook 链（useSymbolRelationships）
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

// 使用 vi.hoisted 提升 mock
const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    getSymbolRelationships: vi.fn(async (_id: number) => null as any),
  },
}));

// Mock i18n：t 返回 key
vi.mock("../../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, params: Record<string, string | number> = {}) =>
    key.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? "")),
  DOT: " | ",
  stageLabel: (v: string | null | undefined) => v ?? "-",
  actionLabel: (v: string | null | undefined) => v ?? "-",
  assetTypeLabel: (v: string | null | undefined) => v ?? "unknown",
  regionShortLabel: (v: string | null | undefined) => v ?? "-",
  getLocale: () => "zh-CN",
  setLocale: () => {},
}));

// Mock antd：保留组件库，仅覆盖 message
vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return {
    ...actual,
    message: {
      ...actual.message,
      success: vi.fn(),
      error: vi.fn(),
      warning: vi.fn(),
      info: vi.fn(),
    },
  };
});

// Mock api/client：暴露 getSymbolRelationships
vi.mock("../../../api/client", () => ({ api: mockApi }));

import SymbolRelationshipBar from "../SymbolRelationshipBar";
import type { SymbolRelationships } from "../../../types/symbolRelationships";

/** 构造一份完整的 SymbolRelationships 数据，支持部分覆盖 */
function makeRelationships(over: Partial<SymbolRelationships> = {}): SymbolRelationships {
  return {
    symbol_id: 1,
    symbol: "600000",
    fetched_at: "2025-01-01T00:00:00Z",
    degraded: false,
    degraded_reason: null,
    candidate: {
      has_candidate: false,
      candidate_id: null,
      scope: null,
      stage: null,
      action: null,
      priority_score: null,
      quality_score: null,
      timing_score: null,
      data_credibility: null,
      generated_at: null,
      scan_run_id: null,
      snapshot_id: null,
      snapshot_generated_at: null,
    },
    observation: {
      has_observation: false,
      watchlist_item_id: null,
      watchlist_id: null,
      watchlist_name: null,
      origin_type: null,
      status: null,
      priority: null,
      tags: [],
      target_portfolio_id: null,
      added_at: null,
    },
    portfolio_member: {
      has_portfolio_membership: false,
      portfolio_id: null,
      portfolio_name: null,
      member_id: null,
      member_status: null,
      execution_mode: null,
      source_type: null,
      effective_from: null,
      note: null,
    },
    position: {
      has_position: false,
      portfolio_id: null,
      portfolio_name: null,
      position_id: null,
      quantity: null,
      cost_price: null,
      latest_price: null,
      market_value: null,
      opened_at: null,
    },
    alert: {
      has_active_alert: false,
      alert_rule_ids: [],
      active_alert_events: 0,
      latest_alert_severity: null,
      latest_alert_at: null,
    },
    ...over,
  };
}

describe("SymbolRelationshipBar 集成测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.getSymbolRelationships.mockResolvedValue(null);
  });

  // 1. symbolId 为 null 时不发请求、不渲染内部徽标
  it("symbolId 为 null 时不应调用 getSymbolRelationships", () => {
    render(<SymbolRelationshipBar symbolId={null} />);
    expect(mockApi.getSymbolRelationships).not.toHaveBeenCalled();
  });

  // 2. symbolId 有效时调用 getSymbolRelationships 并渲染包装层
  it("symbolId 有效时应调用 getSymbolRelationships", async () => {
    render(<SymbolRelationshipBar symbolId={1} />);
    await waitFor(() => {
      expect(mockApi.getSymbolRelationships).toHaveBeenCalledWith(1);
    });
  });

  // 3. 五段状态全部激活时渲染对应徽标（验证与 OpportunityStatusBadges 集成）
  it("五段状态全部激活时应渲染所有徽标", async () => {
    const rel = makeRelationships({
      candidate: {
        has_candidate: true,
        candidate_id: 1,
        scope: "cn-stock",
        stage: "start",
        action: "open",
        priority_score: 80,
        quality_score: 70,
        timing_score: 75,
        data_credibility: "high",
        generated_at: "2025-01-01",
        scan_run_id: 1,
        snapshot_id: 1,
        snapshot_generated_at: "2025-01-01",
      },
      observation: {
        has_observation: true,
        watchlist_item_id: 1,
        watchlist_id: 1,
        watchlist_name: "主观察池",
        origin_type: "manual",
        status: "watching",
        priority: 50,
        tags: [],
        target_portfolio_id: null,
        added_at: "2025-01-01",
      },
      portfolio_member: {
        has_portfolio_membership: true,
        portfolio_id: 1,
        portfolio_name: "默认组合",
        member_id: 1,
        member_status: "active",
        execution_mode: "manual",
        source_type: "manual",
        effective_from: "2025-01-01",
        note: null,
      },
      position: {
        has_position: true,
        portfolio_id: 1,
        portfolio_name: "默认组合",
        position_id: 1,
        quantity: 100,
        cost_price: 10.5,
        latest_price: 11.2,
        market_value: 1120,
        opened_at: "2025-01-01",
      },
      alert: {
        has_active_alert: true,
        alert_rule_ids: [1, 2],
        active_alert_events: 2,
        latest_alert_severity: "warning",
        latest_alert_at: "2025-01-01",
      },
    });
    mockApi.getSymbolRelationships.mockResolvedValue(rel);

    const { container } = render(<SymbolRelationshipBar symbolId={1} />);

    // 等待 hook 完成数据加载
    await waitFor(() => {
      expect(container.querySelector(".symbol-research-relationship-bar")).toBeInTheDocument();
    });

    // 验证包装层渲染（OpportunityStatusBadges 的徽标会渲染为 antd Tag）
    const tags = container.querySelectorAll(".ant-tag");
    expect(tags.length).toBeGreaterThan(0);
  });

  // 4. className 透传到包装层
  it("className 应透传到包装层", () => {
    const { container } = render(
      <SymbolRelationshipBar symbolId={null} className="custom-class" />,
    );
    const wrapper = container.querySelector(".symbol-research-relationship-bar");
    expect(wrapper).toBeInTheDocument();
    expect(wrapper?.classList.contains("custom-class")).toBe(true);
  });

  // ── WP5 验收清单：单段状态徽标覆盖 ──────────────────────────────

  // 5. (L183) 候选状态显示：只有 has_candidate=true 时渲染候选徽标
  it("只有 candidate 激活时应渲染候选徽标且不渲染其他状态徽标", async () => {
    const base = makeRelationships();
    mockApi.getSymbolRelationships.mockResolvedValue(
      makeRelationships({
        candidate: {
          ...base.candidate,
          has_candidate: true,
          scope: "cn-stock",
        },
      }),
    );

    render(<SymbolRelationshipBar symbolId={1} />);
    await waitFor(() => {
      expect(screen.getByText("opportunityBadgeCandidate")).toBeInTheDocument();
    });

    // 默认 showInactive=false，未激活的徽标不应出现
    expect(screen.queryByText("opportunityBadgeObservation")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgePortfolioMember")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgePosition")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgeAlert")).not.toBeInTheDocument();
  });

  // 6. (L183) 观察状态显示：只有 has_observation=true 时渲染观察徽标
  it("只有 observation 激活时应渲染观察徽标且不渲染其他状态徽标", async () => {
    const base = makeRelationships();
    mockApi.getSymbolRelationships.mockResolvedValue(
      makeRelationships({
        observation: {
          ...base.observation,
          has_observation: true,
          watchlist_name: "主观察池",
        },
      }),
    );

    render(<SymbolRelationshipBar symbolId={1} />);
    await waitFor(() => {
      expect(screen.getByText("opportunityBadgeObservation")).toBeInTheDocument();
    });

    expect(screen.queryByText("opportunityBadgeCandidate")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgePosition")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgeAlert")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgePortfolioMember")).not.toBeInTheDocument();
  });

  // 7. (L183) 组合成员状态显示：只有 has_portfolio_membership=true 时渲染组合成员徽标
  it("只有 portfolio_member 激活时应渲染组合成员徽标且不渲染其他状态徽标", async () => {
    const base = makeRelationships();
    mockApi.getSymbolRelationships.mockResolvedValue(
      makeRelationships({
        portfolio_member: {
          ...base.portfolio_member,
          has_portfolio_membership: true,
          portfolio_name: "默认组合",
        },
      }),
    );

    render(<SymbolRelationshipBar symbolId={1} />);
    await waitFor(() => {
      expect(screen.getByText("opportunityBadgePortfolioMember")).toBeInTheDocument();
    });

    expect(screen.queryByText("opportunityBadgeCandidate")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgeObservation")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgePosition")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgeAlert")).not.toBeInTheDocument();
  });

  // 8. (L183) 持仓状态显示：只有 has_position=true 时渲染持仓徽标
  it("只有 position 激活时应渲染持仓徽标且不渲染其他状态徽标", async () => {
    const base = makeRelationships();
    mockApi.getSymbolRelationships.mockResolvedValue(
      makeRelationships({
        position: {
          ...base.position,
          has_position: true,
          quantity: 100,
        },
      }),
    );

    render(<SymbolRelationshipBar symbolId={1} />);
    await waitFor(() => {
      expect(screen.getByText("opportunityBadgePosition")).toBeInTheDocument();
    });

    expect(screen.queryByText("opportunityBadgeCandidate")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgeObservation")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgePortfolioMember")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgeAlert")).not.toBeInTheDocument();
  });

  // 9. (L183) 告警状态显示：只有 has_active_alert=true 时渲染告警徽标
  it("只有 alert 激活时应渲染告警徽标且不渲染其他状态徽标", async () => {
    const base = makeRelationships();
    mockApi.getSymbolRelationships.mockResolvedValue(
      makeRelationships({
        alert: {
          ...base.alert,
          has_active_alert: true,
          active_alert_events: 2,
        },
      }),
    );

    render(<SymbolRelationshipBar symbolId={1} />);
    await waitFor(() => {
      expect(screen.getByText("opportunityBadgeAlert")).toBeInTheDocument();
    });

    expect(screen.queryByText("opportunityBadgeCandidate")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgeObservation")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgePortfolioMember")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgePosition")).not.toBeInTheDocument();
  });

  // 10. (L183) 接口失败降级为"状态未知"：不误报"未加入"
  it("接口失败时应降级显示'状态未知'且不渲染任何激活徽标", async () => {
    mockApi.getSymbolRelationships.mockRejectedValue(new Error("network error"));

    const { container } = render(<SymbolRelationshipBar symbolId={1} />);

    await waitFor(() => {
      expect(screen.getByText("opportunityBadgeUnknown")).toBeInTheDocument();
    });

    // 关键约束：不误报"未加入"——任何激活徽标都不应出现
    expect(screen.queryByText("opportunityBadgeCandidate")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgeObservation")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgePortfolioMember")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgePosition")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgeAlert")).not.toBeInTheDocument();

    // 验证包装层仍有 data-state="unknown" 标记
    const unknownSpan = container.querySelector('[data-state="unknown"]');
    expect(unknownSpan).not.toBeNull();
  });

  // 11. (L183) 点击徽标打开详情弹窗：onOpenDetail 回调透传到 OpportunityStatusBadges
  it("点击徽标应调用 onOpenDetail 回调并携带 symbolId", async () => {
    const base = makeRelationships();
    mockApi.getSymbolRelationships.mockResolvedValue(
      makeRelationships({
        candidate: {
          ...base.candidate,
          has_candidate: true,
          scope: "cn-stock",
        },
      }),
    );
    const onOpenDetail = vi.fn();

    render(<SymbolRelationshipBar symbolId={1} onOpenDetail={onOpenDetail} />);

    // 等待候选徽标渲染
    const badge = await waitFor(() => screen.getByText("opportunityBadgeCandidate"));
    expect(badge).toBeInTheDocument();

    // 点击徽标
    fireEvent.click(badge.closest(".ant-tag")!);

    // 验证 onOpenDetail 被调用，参数为 symbolId=1
    expect(onOpenDetail).toHaveBeenCalledWith(1);
  });
});
