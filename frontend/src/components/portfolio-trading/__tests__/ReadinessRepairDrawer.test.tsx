import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import ReadinessRepairDrawer from "../ReadinessRepairDrawer";
import { renderWithApp } from "../../../test/renderWithApp";
import { api } from "../../../api/client";
import type { ActionItem, ActionItemsResponse } from "../../../types";

vi.mock("../../../api/client", () => ({
  api: {
    updatePortfolio: vi.fn(),
    previewPortfolioReconciliation: vi.fn(),
    triggerPortfolioReconcile: vi.fn(),
    confirmPortfolioReconciliation: vi.fn(),
  },
}));

const item = (over: Partial<ActionItem> = {}): ActionItem => ({
  id: "readiness:X",
  source: "readiness",
  code: "X",
  severity: "blocker",
  title: "标题",
  impact: "影响",
  detail: null,
  family: "readiness:X",
  primary_action: null,
  sources: ["readiness"],
  ...over,
});

const response = (items: ActionItem[]): ActionItemsResponse => ({
  items,
  summary: {
    blockers: items.filter((i) => i.severity === "blocker").length,
    warnings: items.filter((i) => i.severity === "warning").length,
    info: items.filter((i) => i.severity === "info").length,
    ready: false,
  },
  checks: {
    enabled: true,
    account_ready: true,
    data_ready: false,
    source_ready: true,
    schedule_ready: true,
  },
  generated_at: "2026-10-13T10:32:00",
});

function renderDrawer(overProps: Record<string, unknown> = {}) {
  const setActiveTab = vi.fn();
  const showToast = vi.fn();
  const onToggleEnabled = vi.fn();
  const onOpenAddCandidate = vi.fn();
  const utils = renderWithApp(
    <ReadinessRepairDrawer
      open
      onClose={() => {}}
      portfolioId={7}
      data={response([
        item({
          id: "data-freshness",
          code: "MARKET_DATA_STALE",
          title: "行情或评分过期",
          impact: "今天不会真实下单",
          family: "data-freshness",
          primary_action: { action_type: "redirect", target: "settings:data-center" },
        }),
      ])}
      loading={false}
      isSimulated
      toggling={false}
      canToggle
      onRefresh={vi.fn()}
      onToggleEnabled={onToggleEnabled}
      onOpenAddCandidate={onOpenAddCandidate}
      {...overProps}
    />,
    { contextValue: { setActiveTab, showToast } as never },
  );
  return { ...utils, setActiveTab, onToggleEnabled, onOpenAddCandidate };
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
});

describe("T7 修复抽屉动作", () => {
  it("交易日卡片打开居中快捷核对，关闭后保留原列表和展开状态", async () => {
    const onClose = vi.fn();
    vi.mocked(api.previewPortfolioReconciliation).mockResolvedValue({ portfolio_id: 7, trade_date: "2026-09-30",
      items: [], zero_sum_check_passed: false, differences_found: true, as_of_at: "2026-10-24T01:00:00Z" });
    renderDrawer({ onClose, data: response([
      item({ id: "day-29", title: "9月29日对账尚未完成", trade_date: "2026-09-29", primary_action: { action_type: "guided", target: "governance:reconciliation" } }),
      item({ id: "day-30", title: "9月30日对账尚未完成", trade_date: "2026-09-30", primary_action: { action_type: "guided", target: "governance:reconciliation" } }),
    ]) });
    fireEvent.click(screen.getByText("其他待处理事项（1）"));
    fireEvent.click(screen.getByRole("button", { name: "9月30日对账尚未完成 快捷核对" }));
    await waitFor(() => expect(api.previewPortfolioReconciliation).toHaveBeenCalledWith(7, "2026-09-30"));
    // rc-util uses the same generated title ID for overlays in test mode.
    const modalRoot = document.querySelector(".pi-quick-check-root") as HTMLElement;
    const dialog = within(modalRoot).getByRole("dialog");
    await waitFor(() => expect(dialog).toBeVisible());
    expect(api.triggerPortfolioReconcile).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: /^关\s*闭$/ }));
    await waitFor(() => expect(document.querySelector(".pi-quick-check-root")).toBeNull());
    expect(screen.getByRole("button", { name: "9月30日对账尚未完成 快捷核对" })).toBeVisible();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("日期不明确的卡片禁止猜测交易日", () => {
    renderDrawer({ data: response([item({ trade_date: "2026-09-31", primary_action: { action_type: "guided", target: "governance:reconciliation" } })]) });
    expect(screen.getByRole("button", { name: "标题 快捷核对" })).toBeDisabled();
    expect(api.previewPortfolioReconciliation).not.toHaveBeenCalled();
  });

  it("先显示可处理的下单限制，其他事项收起，避免先让用户打开开关", () => {
    const r = renderDrawer({ data: response([
      item({ id: "disabled", code: "PORTFOLIO_DISABLED", title: "自动交易没有打开", primary_action: { action_type: "toggle" } }),
      item({ id: "stale", code: "MARKET_DATA_STALE", title: "请先更新行情", primary_action: { action_type: "redirect", target: "settings:data-center" } }),
      item({ id: "missing", severity: "warning", title: "尚未设置通知", primary_action: null }),
    ]) });
    const next = screen.getByRole("region", { name: "先处理这一项" });
    expect(within(next).getByText("请先更新行情")).toBeVisible();
    expect(within(next).getByRole("button", { name: "前往处理" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "开启自动接管" })).not.toBeVisible();
    fireEvent.click(screen.getByText("其他待处理事项（2）"));
    expect(screen.getByRole("button", { name: "开启自动接管" })).toBeVisible();
    expect(r.onToggleEnabled).not.toHaveBeenCalled();
  });

  it("redirect 动作直达设置数据中心", () => {
    renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: "前往处理" }));
    expect(window.localStorage.getItem("settings_active_section")).toBe("data-center");
    expect(screen.getByText("行情或评分过期")).toBeTruthy();
  });

  it("toggle 在健康态原地触发开关回调", () => {
    const r = renderDrawer({
      data: response([
        item({
          id: "readiness:PORTFOLIO_DISABLED",
          code: "PORTFOLIO_DISABLED",
          title: "自动接管未开启",
          primary_action: { action_type: "toggle" },
        }),
      ]),
    });
    fireEvent.click(screen.getByRole("button", { name: "开启自动接管" }));
    expect(r.onToggleEnabled).toHaveBeenCalledTimes(1);
  });

  it("toggle 在 HG1 阻塞时降级为去总览开启，并派发子页事件", () => {
    const listener = vi.fn();
    window.addEventListener("portfolio:subtab-navigate", listener);
    renderDrawer({
      canToggle: false,
      data: response([
        item({
          code: "PORTFOLIO_DISABLED",
          title: "自动接管未开启",
          primary_action: { action_type: "toggle" },
        }),
      ]),
    });
    fireEvent.click(screen.getByRole("button", { name: "去总览开启" }));
    expect((listener.mock.calls[0][0] as CustomEvent<{ tab: string }>).detail.tab).toBe("overview");
    window.removeEventListener("portfolio:subtab-navigate", listener);
  });

  it("open-modal 动作打开添加候选弹窗", () => {
    const r = renderDrawer({
      data: response([
        item({
          severity: "warning",
          code: "NO_CANDIDATES",
          title: "组合还没有标的",
          primary_action: { action_type: "open-modal", target: "add-candidate" },
        }),
      ]),
    });
    fireEvent.click(screen.getByRole("button", { name: "添加候选标的" }));
    expect(r.onOpenAddCandidate).toHaveBeenCalledTimes(1);
  });

  it("折叠的技术信息默认不裸显英文 code", () => {
    renderDrawer();
    // closed <details> 内文本对可查询树不可见
    expect(screen.queryByText("MARKET_DATA_STALE")).toBeNull();
  });

  it("全部就绪时显示绿色空态", () => {
    renderDrawer({
      data: { ...response([]), summary: { blockers: 0, warnings: 0, info: 0, ready: true } },
    });
    expect(screen.getAllByText("已就绪，等待调度")).toHaveLength(2);
  });

  it("不会在仍有对账阻断时信任旧的 ready=true", () => {
    const data = response([item({ code: "RECONCILIATION_BLOCKED", title: "对账差异阻塞" })]);
    data.summary.ready = true;
    renderDrawer({ data });
    expect(screen.getByText("暂不下单，点开查看处理项")).toBeTruthy();
    expect(screen.queryByText("已就绪，等待调度")).toBeNull();
  });

  it("已有候选而暂无信号时显示正常等待，不宣称订单门禁已通过", () => {
    const data = response([item({ code: "NO_BUY_SIGNALS_TODAY", severity: "info", title: "今日无买入信号" })]);
    data.summary.waiting_for_signal = true;
    data.summary.execution_state = "waiting_signal";
    renderDrawer({ data });
    expect(screen.getByText("正常等待买入信号，无需处理")).toBeTruthy();
    expect(screen.queryByText("暂不下单，点开查看处理项")).toBeNull();
    expect(screen.queryByRole("button", { name: "添加候选标的" })).toBeNull();
    expect(screen.queryByRole("region", { name: "先处理这一项" })).not.toBeInTheDocument();
  });
});
