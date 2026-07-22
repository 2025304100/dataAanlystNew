// WP1.7：OpportunityStatusBadges 组件测试
//
// 覆盖：
// - 加载中状态
// - 五段状态徽标（candidate/observation/portfolio_member/position/alert）激活/未激活
// - 接口失败降级"状态未知"，不误报"未加入"
// - degraded=true 显示"状态未知"
// - tooltip 内容正确（基于 scope）
// - symbolId 变化时重新请求
//
// 约束：
// - 不调用真实后端，所有请求通过 vi.fn mock
// - i18n mock：t(key) 返回 key，便于按 key 断言
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    // 默认返回 null（不发请求），单测内通过 mockImplementation 控制行为
    getSymbolRelationships: vi.fn(async (_id: number) => null as any),
  },
}));

// Mock i18n：t 返回 key，template 返回拼接后的字符串
vi.mock("../../i18n", () => ({
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

// Mock antd：保留组件库，仅覆盖 message（本组件未使用 message，但保留以防未来扩展）
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

// Mock api/client：只暴露组件依赖的 getSymbolRelationships
vi.mock("../../api/client", () => ({ api: mockApi }));

import OpportunityStatusBadges from "../opportunity/OpportunityStatusBadges";
import type { SymbolRelationships } from "../../types/symbolRelationships";

/** 构造一份完整的 SymbolRelationships 数据，支持部分覆盖 */
function makeRelationships(over: Partial<SymbolRelationships> = {}): SymbolRelationships {
  return {
    symbol_id: 1,
    symbol: "600000",
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
      watchlist_id: null,
      watchlist_name: null,
      watchlist_item_id: null,
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
    fetched_at: "2026-07-04T10:00:00Z",
    degraded: false,
    degraded_reason: null,
    ...over,
  } as SymbolRelationships;
}

describe("OpportunityStatusBadges 组件测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 默认实现：返回空关联（所有 has_*=false）
    mockApi.getSymbolRelationships.mockImplementation(async (_id: number) => makeRelationships());
  });

  // 1. 加载中显示加载提示
  it("加载中应显示 opportunityBadgeLoading 文本", async () => {
    // 让 fetch 永不 resolve，保持 loading 状态
    mockApi.getSymbolRelationships.mockImplementation(
      () => new Promise(() => {}),
    );
    render(<OpportunityStatusBadges symbolId={1} />);
    // 初始 loading 状态：显示 opportunityBadgeLoading
    expect(screen.getByText("opportunityBadgeLoading")).toBeInTheDocument();
  });

  // 2. 所有状态都未关联时（showInactive=true）显示 5 个 inactive 徽标
  it("showInactive=true 且所有状态未关联时应显示 5 个徽标", async () => {
    mockApi.getSymbolRelationships.mockImplementation(async () => makeRelationships());
    render(<OpportunityStatusBadges symbolId={1} showInactive={true} />);
    // 等待数据加载完成
    await waitFor(() => {
      expect(mockApi.getSymbolRelationships).toHaveBeenCalledWith(1);
    });
    // 五段徽标都应出现：候选 / 已观察 / 组合成员 / 持仓 / 告警
    expect(screen.getByText("opportunityBadgeCandidate")).toBeInTheDocument();
    expect(screen.getByText("opportunityBadgeObservation")).toBeInTheDocument();
    expect(screen.getByText("opportunityBadgePortfolioMember")).toBeInTheDocument();
    expect(screen.getByText("opportunityBadgePosition")).toBeInTheDocument();
    expect(screen.getByText("opportunityBadgeAlert")).toBeInTheDocument();
  });

  // 3. 只有候选时显示候选 active
  it("只有 has_candidate=true 时应显示候选徽标", async () => {
    mockApi.getSymbolRelationships.mockImplementation(async () =>
      makeRelationships({
        candidate: {
          ...makeRelationships().candidate,
          has_candidate: true,
          scope: "cn-stock",
        },
      }),
    );
    render(<OpportunityStatusBadges symbolId={1} />);
    await waitFor(() => {
      expect(screen.getByText("opportunityBadgeCandidate")).toBeInTheDocument();
    });
    // 默认 showInactive=false，只显示 active 徽标，因此其他徽标不应出现
    expect(screen.queryByText("opportunityBadgeObservation")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgePosition")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgeAlert")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgePortfolioMember")).not.toBeInTheDocument();
  });

  // 4. 只有观察时显示观察 active
  it("只有 has_observation=true 时应显示观察徽标", async () => {
    mockApi.getSymbolRelationships.mockImplementation(async () =>
      makeRelationships({
        observation: {
          ...makeRelationships().observation,
          has_observation: true,
          watchlist_name: "主观察池",
        },
      }),
    );
    render(<OpportunityStatusBadges symbolId={1} />);
    await waitFor(() => {
      expect(screen.getByText("opportunityBadgeObservation")).toBeInTheDocument();
    });
    expect(screen.queryByText("opportunityBadgeCandidate")).not.toBeInTheDocument();
  });

  // 5. 只有持仓时显示持仓 active
  it("只有 has_position=true 时应显示持仓徽标", async () => {
    mockApi.getSymbolRelationships.mockImplementation(async () =>
      makeRelationships({
        position: {
          ...makeRelationships().position,
          has_position: true,
          quantity: 100,
        },
      }),
    );
    render(<OpportunityStatusBadges symbolId={1} />);
    await waitFor(() => {
      expect(screen.getByText("opportunityBadgePosition")).toBeInTheDocument();
    });
    expect(screen.queryByText("opportunityBadgeCandidate")).not.toBeInTheDocument();
  });

  // 6. 只有告警时显示告警 active
  it("只有 has_active_alert=true 时应显示告警徽标", async () => {
    mockApi.getSymbolRelationships.mockImplementation(async () =>
      makeRelationships({
        alert: {
          ...makeRelationships().alert,
          has_active_alert: true,
          active_alert_events: 2,
        },
      }),
    );
    render(<OpportunityStatusBadges symbolId={1} />);
    await waitFor(() => {
      expect(screen.getByText("opportunityBadgeAlert")).toBeInTheDocument();
    });
    expect(screen.queryByText("opportunityBadgeCandidate")).not.toBeInTheDocument();
  });

  // 7. 全部 active 时所有徽标 active
  it("全部 has_*=true 时应显示全部 5 个徽标", async () => {
    const base = makeRelationships();
    mockApi.getSymbolRelationships.mockImplementation(async () =>
      makeRelationships({
        candidate: { ...base.candidate, has_candidate: true, scope: "cn-stock" },
        observation: { ...base.observation, has_observation: true, watchlist_name: "主观察池" },
        portfolio_member: { ...base.portfolio_member, has_portfolio_membership: true, portfolio_name: "默认组合" },
        position: { ...base.position, has_position: true, quantity: 100 },
        alert: { ...base.alert, has_active_alert: true, active_alert_events: 1 },
      }),
    );
    render(<OpportunityStatusBadges symbolId={1} />);
    await waitFor(() => {
      expect(screen.getByText("opportunityBadgeCandidate")).toBeInTheDocument();
    });
    expect(screen.getByText("opportunityBadgeObservation")).toBeInTheDocument();
    expect(screen.getByText("opportunityBadgePortfolioMember")).toBeInTheDocument();
    expect(screen.getByText("opportunityBadgePosition")).toBeInTheDocument();
    expect(screen.getByText("opportunityBadgeAlert")).toBeInTheDocument();
  });

  // 8. 接口失败显示"状态未知"，不显示"未加入"
  it("接口失败时应显示 opportunityBadgeUnknown，不误报未加入", async () => {
    mockApi.getSymbolRelationships.mockRejectedValue(new Error("network error"));
    render(<OpportunityStatusBadges symbolId={1} />);
    await waitFor(() => {
      expect(screen.getByText("opportunityBadgeUnknown")).toBeInTheDocument();
    });
    // 关键约束：不误报"未加入"——任何激活徽标都不应出现
    expect(screen.queryByText("opportunityBadgeCandidate")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgeObservation")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgePosition")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgeAlert")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgePortfolioMember")).not.toBeInTheDocument();
    // 应有 data-state="unknown" 标记
    const unknownSpan = document.querySelector('[data-state="unknown"]');
    expect(unknownSpan).not.toBeNull();
  });

  // 9. degraded=true 显示"部分未知"徽标
  it("degraded=true 时应显示 opportunityBadgeUnknown（降级提示）", async () => {
    mockApi.getSymbolRelationships.mockImplementation(async () =>
      makeRelationships({ degraded: true, degraded_reason: "子查询失败" }),
    );
    render(<OpportunityStatusBadges symbolId={1} />);
    await waitFor(() => {
      expect(screen.getByText("opportunityBadgeUnknown")).toBeInTheDocument();
    });
    // 降级时不显示具体激活徽标（避免误导）
    expect(screen.queryByText("opportunityBadgeCandidate")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityBadgePosition")).not.toBeInTheDocument();
  });

  // 10. tooltip 内容正确（基于 scope，不是 priority_score）
  it("候选徽标 tooltip 应包含扫描范围信息", async () => {
    mockApi.getSymbolRelationships.mockImplementation(async () =>
      makeRelationships({
        candidate: {
          ...makeRelationships().candidate,
          has_candidate: true,
          scope: "cn-stock",
          priority_score: 80,
        },
      }),
    );
    const { container } = render(<OpportunityStatusBadges symbolId={1} />);
    await waitFor(() => {
      expect(screen.getByText("opportunityBadgeCandidate")).toBeInTheDocument();
    });
    // antd Tooltip 在 hover 时才显示 title；通过触发 mouseenter 显示
    const badge = screen.getByText("opportunityBadgeCandidate").closest(".ant-tag") as HTMLElement;
    expect(badge).not.toBeNull();
    fireEvent.mouseEnter(badge);
    // tooltip 出现在 body 末尾的 .ant-tooltip-inner
    await waitFor(() => {
      const tooltip = document.querySelector(".ant-tooltip-inner");
      expect(tooltip).not.toBeNull();
      // tooltip 应包含 opportunityBadgeCandidateTip（"扫描范围" key）和 scope 值
      expect(tooltip?.textContent).toContain("opportunityBadgeCandidateTip");
      expect(tooltip?.textContent).toContain("cn-stock");
    });
  });

  // 11. symbolId 变化时重新请求
  it("symbolId 变化时应重新调用 getSymbolRelationships", async () => {
    mockApi.getSymbolRelationships.mockImplementation(async (id: number) =>
      makeRelationships({ symbol_id: id }),
    );
    const { rerender } = render(<OpportunityStatusBadges symbolId={1} />);
    await waitFor(() => {
      expect(mockApi.getSymbolRelationships).toHaveBeenCalledWith(1);
    });
    expect(mockApi.getSymbolRelationships).toHaveBeenCalledTimes(1);

    // 切换到 symbolId=2
    rerender(<OpportunityStatusBadges symbolId={2} />);
    await waitFor(() => {
      expect(mockApi.getSymbolRelationships).toHaveBeenCalledWith(2);
    });
    expect(mockApi.getSymbolRelationships).toHaveBeenCalledTimes(2);
  });

  // 12. symbolId 为 null/undefined 时不发请求
  it("symbolId 为 null 时不发请求且不渲染徽标", () => {
    const { container } = render(<OpportunityStatusBadges symbolId={null} />);
    expect(mockApi.getSymbolRelationships).not.toHaveBeenCalled();
    // 不应渲染任何徽标
    expect(container.querySelector(".opportunity-status-badges")).toBeNull();
  });

  // 13. compact 模式：所有状态未激活时显示 opportunityBadgeNoStatus
  it("compact 模式且无激活状态时显示 opportunityBadgeNoStatus", async () => {
    mockApi.getSymbolRelationships.mockImplementation(async () => makeRelationships());
    render(<OpportunityStatusBadges symbolId={1} compact={true} />);
    await waitFor(() => {
      expect(screen.getByText("opportunityBadgeNoStatus")).toBeInTheDocument();
    });
  });

  // 14. compact 模式：有激活状态时显示汇总徽标
  it("compact 模式且有激活状态时显示 opportunityBadgeMixed 汇总", async () => {
    const base = makeRelationships();
    mockApi.getSymbolRelationships.mockImplementation(async () =>
      makeRelationships({
        candidate: { ...base.candidate, has_candidate: true },
        observation: { ...base.observation, has_observation: true },
      }),
    );
    render(<OpportunityStatusBadges symbolId={1} compact={true} />);
    await waitFor(() => {
      // template("opportunityBadgeMixed", { count: "2" }) => "opportunityBadgeMixed"（mock 返回 key）
      // 实际 mock 的 template 会保留 key，仅替换 {count}，结果为 "opportunityBadgeMixed"
      expect(screen.getByText("opportunityBadgeMixed")).toBeInTheDocument();
    });
  });
});
