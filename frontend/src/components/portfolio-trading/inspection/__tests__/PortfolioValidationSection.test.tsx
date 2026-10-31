// 组合校验区（原型 06 · 组合状态 / 权限矩阵）的渲染契约。
//
// 这里不 mock i18n：状态中文含义与触发条件全部来自 portfolioStatus.* 字典，
// 用假 t() 就验不到「界面上真的有人话」。同时锁住三条产品口径：
//   1. 未知权限只能是「未知」，不能退化成允许；
//   2. 状态码是服务端枚举原值，中文含义另起一列；
//   3. 当前行只能命中一行。
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import type { InspectionJob, InspectionReconciliationQueue, InspectionRuntime } from "../../../../types/portfolioInspection";
import type { ActionItem } from "../../../../types";
import PortfolioValidationSection from "../PortfolioValidationSection";

vi.mock("../AcceptanceControls", () => ({
  AcceptanceButton: ({ children, mutation, loading, icon, ...props }: any) => <button type="button" {...props}>{children}</button>,
  AcceptanceModal: () => null,
}));

import { setLocale } from "../../../../i18n";

function runtime(overrides: Partial<InspectionRuntime> = {}): InspectionRuntime {
  return {
    portfolio_id: 1,
    generated_at: "2026-10-30T01:32:00Z",
    current_state: "RECONCILIATION_BLOCKED",
    permissions: { allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: true },
    capability_reasons: {
      allow_new_buys: ["缺少本日执行证据"],
      allow_risk_exits: ["当前状态门禁允许风险退出"],
      allow_auto_recovery: ["异常恢复仍需本人审核"],
    },
    recovery_summary: { can_confirm: false, blockers: ["先完成证据核对"], checks: [], review_required: true },
    dimensions: {},
    ...overrides,
  };
}

const baseProps = {
  portfolioId: 1,
  title: "新买入已暂停",
  description: "1 类运行条件待处理。",
  kind: "blocked" as const,
  loading: false,
  status: { portfolio_id: 1, current_state: "RECONCILIATION_BLOCKED", allowed_transitions: [], is_auto_simulation_eligible: false },
  primaryLabel: "开始恢复引导",
  onPrimary: () => {},
  queue: null as InspectionReconciliationQueue | null,
  job: null as InspectionJob | null,
  actionItems: [] as ActionItem[],
  actionItemsGeneratedAt: "2026-10-31T01:20:00Z",
  onOpenAction: () => {},
  onOpenHistory: () => {},
  onInspectDay: () => {},
  onReloadQueue: () => {},
  onOpenReconciliation: () => {},
};

beforeEach(() => setLocale("zh-CN"));

describe("组合校验区", () => {
  it("横幅讲结论与四类权限，状态矩阵逐行给出中文含义与门禁口径", () => {
    render(<PortfolioValidationSection {...baseProps} runtime={runtime()} />);

    expect(screen.getByRole("heading", { level: 1, name: "新买入已暂停" })).toBeVisible();
    expect(screen.getByLabelText("新买入权限")).toHaveTextContent("不允许");
    expect(screen.getByLabelText("风险退出权限")).toHaveTextContent("允许");
    expect(screen.getByLabelText("自动恢复权限")).toHaveTextContent("不允许");
    expect(screen.getByLabelText("人工确认权限")).toHaveTextContent("需要");

    const table = within(screen.getByTestId("pi-validation-state")).getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(11);
    // 状态码是原值，中文含义是名词态，触发条件才是完整口径
    expect(within(table).getByText("RECONCILIATION_BLOCKED")).toBeVisible();
    expect(within(table).getByText("对账阻断")).toBeVisible();
    // 触发行必须带可读的条件说明，而不是空占位
    expect(within(table).getByText(/订单、成交、持仓、现金或证据存在差异/)).toBeVisible();
    // 同一时刻只命中一行
    expect(table.querySelectorAll("tbody tr.is-current")).toHaveLength(1);
    expect(table.querySelector("tbody tr.is-current")).toHaveAttribute("data-state", "RECONCILIATION_BLOCKED");
    expect(within(table).getByLabelText("当前状态")).toHaveTextContent("当前");
  });

  it("分段导航可切到权限矩阵，并复述真实限制条件而不是通用模板", () => {
    render(<PortfolioValidationSection {...baseProps} runtime={runtime()} />);
    expect(screen.getByTestId("pi-validation-permissions")).not.toBeVisible();

    fireEvent.click(screen.getByRole("tab", { name: "权限矩阵" }));
    const panel = screen.getByTestId("pi-validation-permissions");
    expect(panel).toBeVisible();
    expect(screen.getByRole("tab", { name: "权限矩阵" })).toHaveAttribute("aria-selected", "true");
    expect(within(panel).getByText("新建买入单")).toBeVisible();
    expect(within(panel).getByText("缺少本日执行证据")).toBeVisible();
    expect(within(panel).getByText("先完成证据核对")).toBeVisible();
    expect(within(panel).getByText("本次审核")).toBeVisible();

    // 方向键切换是原型的分段交互，不能只有鼠标能用
    fireEvent.keyDown(screen.getByRole("tab", { name: "权限矩阵" }), { key: "ArrowLeft" });
    expect(screen.getByTestId("pi-validation-state")).toBeVisible();
  });

  it("运行检查读不到时权限一律显示未知，且不把未知画成绿灯", () => {
    // 关键：即便持久化 /status 还留着旧状态，运行检查读不通时也不能在矩阵里钉「当前」
    const staleStatus = { portfolio_id: 1, current_state: "READY", allowed_transitions: [], is_auto_simulation_eligible: false };
    render(<PortfolioValidationSection {...baseProps} kind="unknown" title="当前交易状态待核实" runtime={null} status={staleStatus} />);

    expect(screen.getByRole("heading", { level: 1, name: "当前交易状态待核实" })).toBeVisible();
    for (const label of ["新买入权限", "风险退出权限", "自动恢复权限", "人工确认权限"]) {
      expect(screen.getByLabelText(label)).toHaveTextContent("未知");
      expect(screen.getByLabelText(label)).not.toHaveClass("is-pass");
    }
    const table = within(screen.getByTestId("pi-validation-state")).getByRole("table");
    expect(table.querySelectorAll("tbody tr.is-current")).toHaveLength(0);
    expect(screen.getByText("状态待核实")).toBeVisible();
  });

  it("对账连续性只复述队列事实：逐日结论、重试痕迹与调度，不推算差异金额", () => {
    const queue: InspectionReconciliationQueue = {
      portfolio_id: 1,
      coverage_start: "2026-10-20",
      verified_through: "2026-10-27",
      target_trade_date: "2026-10-28",
      calendar_status: "KNOWN",
      days: [
        { trade_date: "2026-10-28", status: "WAITING_EXECUTION", reason: "等待合法执行结果", work_state: "waiting_execution" },
        { trade_date: "2026-10-27", status: "PASSED", reason: "本日已完成当前代核对", report_id: "rep-27", report_version: "3", work_state: "done" },
        { trade_date: "2026-10-26", status: "NOT_CHECKED", reason: "本日尚待基于已接受账户基线核对", work_state: "needs_review" },
      ],
      limitations: [],
    };
    const job: InspectionJob = {
      portfolio_id: 1, enabled: true, status: "running", last_run_at: "2026-10-31T01:05:00Z",
      next_run_at: "2026-10-31T01:10:00Z", task_id: "task-1", reason: "",
      check_summary: { passed_count: 12, waiting_count: 1, difference_count: 0, error_count: 1 },
      queue,
      work_items: [{ trade_date: "2026-10-26", work_state: "needs_review", requested_evidence_version: 4,
        processed_evidence_version: 3, attempt_count: 3, last_error_code: "RECONCILIATION_TRANSIENT_READ_ERROR", retry_exhausted: true }],
    };
    render(<PortfolioValidationSection {...baseProps} runtime={runtime()} queue={queue} job={job} />);
    fireEvent.click(screen.getByRole("tab", { name: "对账连续性" }));
    const panel = screen.getByTestId("pi-validation-continuity");
    expect(panel).toBeVisible();

    // 概览：已核对到哪、最早待核对、缺口、作业是否在跑
    const facts = panel.querySelector(".pi-verify-facts") as HTMLElement;
    expect(within(facts).getByText("2026-10-27")).toBeVisible();
    expect(within(facts).getByText("2026-10-28")).toBeVisible();
    expect(within(facts).getByText("2 个")).toBeVisible();
    expect(facts.textContent).toContain("运行中");
    // 逐日结论用中文徽章，不印后端枚举原值
    expect(within(panel).getByText("等待执行证据")).toBeVisible();
    expect(within(panel).getByText("已对平")).toBeVisible();
    expect(panel.textContent).not.toContain("WAITING_EXECUTION");
    // 重试到上限必须明说已转人工，而不是继续显示"排队中"
    expect(panel.textContent).toContain("第 3 次");
    expect(panel.textContent).toContain("已转人工核查");
    expect(within(panel).getByText("需要人工核查")).toBeVisible();
    // 原型里的持仓/现金差异列需要逐日报告，队列本身没有这些数，不许前端编
    expect(panel.textContent).not.toContain("持仓差异");
    expect(panel.textContent).not.toContain("差异金额");
  });

  it("逐日表每一行直接进当日核对，队列缺失时就在面板里重读", () => {
    const inspect = vi.fn();
    const reload = vi.fn();
    const queue: InspectionReconciliationQueue = {
      portfolio_id: 1, coverage_start: "2026-10-20", verified_through: "2026-10-27",
      target_trade_date: "2026-10-28", calendar_status: "KNOWN", limitations: [],
      days: [
        { trade_date: "2026-10-28", status: "WAITING_EXECUTION", reason: "等待合法执行结果", work_state: "waiting_execution" },
        { trade_date: "2026-10-27", status: "PASSED", reason: "本日已完成当前代核对" },
      ],
    };
    const { rerender } = render(
      <PortfolioValidationSection {...baseProps} runtime={runtime()} queue={queue}
        onInspectDay={inspect} onReloadQueue={reload} />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "对账连续性" }));
    const panel = screen.getByTestId("pi-validation-continuity");
    // 待核对优先：目标日排第一，且唯一标记 is-target（否则"每日对账"表里找不到能动手的那天）
    const firstRow = panel.querySelector("tbody tr") as HTMLElement;
    expect(firstRow).toHaveAttribute("data-status", "WAITING_EXECUTION");
    expect(firstRow).toHaveClass("is-target");
    expect(panel.querySelectorAll("tbody tr.is-target")).toHaveLength(1);
    // 行内按钮的无障碍名带日期，避免与「执行与对账」里的「每日对账」重名
    fireEvent.click(within(panel).getByRole("button", { name: "2026-10-28 每日对账" }));
    expect(inspect).toHaveBeenCalledWith("2026-10-28");
    expect(inspect).toHaveBeenCalledTimes(1);

    // 队列读不到：面板内直接给重读按钮，不必绕去页头；同时保留"打开对账"兜底入口
    const openPanel = vi.fn();
    rerender(<PortfolioValidationSection {...baseProps} runtime={runtime()} queue={null}
      onInspectDay={inspect} onReloadQueue={reload} onOpenReconciliation={openPanel} />);
    const emptyPanel = screen.getByTestId("pi-validation-continuity");
    expect(emptyPanel.textContent).toContain("还未读取到自动对账队列");
    fireEvent.click(within(emptyPanel).getByRole("button", { name: "打开对账" }));
    expect(openPanel).toHaveBeenCalledTimes(1);
    fireEvent.click(within(emptyPanel).getByRole("button", { name: "重新读取" }));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("行动项按服务端派生清单展示：优先级、来源可追，空态明确不需要手动销项", () => {
    const items: ActionItem[] = [
      { id: "a2", source: "readiness", code: "SCORE_STALE", severity: "warning", title: "评分落后两个交易日",
        impact: "新买入暂时关闭", detail: null, family: "data-freshness", primary_action: { action_type: "subtab", target: "governance:state" }, sources: ["readiness"] },
      { id: "a1", source: "hg1_status", code: "RECONCILIATION_BLOCKED", severity: "blocker", title: "对账差异尚未确认",
        impact: "新买入被阻断", detail: "报告 rep-28 仍有一项差异未清零", family: "reconciliation",
        primary_action: { action_type: "subtab", target: "governance:reconciliation" }, sources: ["hg1_status"] },
      { id: "a3", source: "g7_ops", code: "G7_INFO", severity: "info", title: "引擎运维提示",
        impact: "仅展示", detail: null, family: null, primary_action: null, sources: ["g7_ops"] },
    ];
    render(<PortfolioValidationSection {...baseProps} runtime={runtime()} actionItems={items} />);
    fireEvent.click(screen.getByRole("tab", { name: "行动项" }));
    const panel = screen.getByTestId("pi-validation-actions");
    expect(panel).toBeVisible();

    const rows = panel.querySelectorAll(".pi-verify-action-row");
    expect(rows).toHaveLength(3);
    // 优先级排序：blocker → warning → info
    expect([...rows].map((row) => row.getAttribute("data-severity"))).toEqual(["blocker", "warning", "info"]);
    expect(panel.textContent).toContain("高 1 · 中 1 · 低 1");
    // 来源可追，detail 优先于 impact
    expect(panel.textContent).toContain("组合状态机");
    expect(panel.textContent).toContain("报告 rep-28 仍有一项差异未清零");
    expect(panel.textContent).toContain("自动交易门禁 · data-freshness");
    // 没有可验证入口的项不能给一个假「直达」
    const direct = within(rows[2] as HTMLElement).getByRole("button", { name: "直达" });
    expect(direct).toBeDisabled();
    // 闭环口径写在界面上，不能靠口口相传
    expect(panel.textContent).toContain("没有「忽略 / 作废」");
  });

  it("没有待办时说清是服务端派生，不需要用户手动销项", () => {
    render(<PortfolioValidationSection {...baseProps} runtime={runtime()} actionItems={[]} />);
    fireEvent.click(screen.getByRole("tab", { name: "行动项" }));
    expect(screen.getByTestId("pi-validation-actions").textContent).toContain("修复后无需手动销项");
  });

  it("逐日对账会一天一条，清单先给前八条，其余按需展开", () => {
    const many: ActionItem[] = Array.from({ length: 10 }, (_, index) => ({
      id: `recon-${index}`,
      source: "hg1_status",
      code: "RECONCILIATION_BLOCKED",
      severity: "blocker" as const,
      title: `2026-10-${String(index + 1).padStart(2, "0")} 对账尚未完成`,
      impact: "新买入被阻断",
      detail: "DECISION_RUN_NOT_FOUND",
      family: "reconciliation",
      primary_action: { action_type: "subtab" as const, target: "governance:reconciliation" },
      sources: ["hg1_status"],
    }));
    render(<PortfolioValidationSection {...baseProps} runtime={runtime()} actionItems={many} />);
    fireEvent.click(screen.getByRole("tab", { name: "行动项" }));
    const panel = screen.getByTestId("pi-validation-actions");
    expect(panel.querySelectorAll(".pi-verify-action-row")).toHaveLength(8);
    expect(panel.textContent).toContain("已显示 8 / 10 项");
    // detail 里的差异类型枚举同样换成中文，不把 DECISION_RUN_NOT_FOUND 印给用户
    expect(panel.textContent).toContain("当日无决策运行");
    expect(panel.textContent).not.toContain("DECISION_RUN_NOT_FOUND");

    fireEvent.click(within(panel).getByRole("button", { name: /展开其余 2 项/ }));
    expect(panel.querySelectorAll(".pi-verify-action-row")).toHaveLength(10);
  });

  // 第五段「操作记录」只给入口：完整流水在壳层的审计抽屉里，本页不重复拉一遍。
  // 因此这里既要锁住分段数量与顺序，也要锁住「点击只转交一次 onOpenHistory」，
  // 防止后续有人把它做成第二个流水面板或重复挂载同一个抽屉。
  it("第五段「操作记录」只转交抽屉入口，不在本页重复画流水", () => {
    const onOpenHistory = vi.fn();
    render(<PortfolioValidationSection {...baseProps} runtime={runtime()} onOpenHistory={onOpenHistory} />);

    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(5);
    expect(tabs.map((tab) => tab.textContent)).toEqual(["组合状态", "权限矩阵", "对账连续性", "行动项", "操作记录"]);
    expect(screen.getByTestId("pi-validation-audit")).not.toBeVisible();
    expect(onOpenHistory).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("tab", { name: "操作记录" }));
    const panel = screen.getByTestId("pi-validation-audit");
    expect(panel).toBeVisible();
    expect(screen.getByRole("tab", { name: "操作记录" })).toHaveAttribute("aria-selected", "true");
    // 面板只说明去哪看：不画表格、不列流水，避免和审计抽屉两份口径打架
    expect(panel.textContent).toContain("完整记录在独立抽屉里分页回看");
    expect(within(panel).queryByRole("table")).not.toBeInTheDocument();

    fireEvent.click(within(panel).getByRole("button", { name: "打开操作记录" }));
    expect(onOpenHistory).toHaveBeenCalledTimes(1);
  });

  it("刷新期间保留最近一次已核实快照，不整块闪成未知", () => {
    const { rerender } = render(<PortfolioValidationSection {...baseProps} runtime={runtime()} />);
    expect(screen.getByLabelText("新买入权限")).toHaveTextContent("不允许");

    // 重新检测 / 30s 后台刷新：admission 会短暂置为 checking 且 runtime 为 null
    rerender(<PortfolioValidationSection {...baseProps} kind="checking" title="正在核实当前运行条件" runtime={null} />);
    expect(screen.getByRole("heading", { level: 1, name: "新买入已暂停" })).toBeVisible();
    expect(screen.getByLabelText("新买入权限")).toHaveTextContent("不允许");
    expect(screen.getByLabelText("风险退出权限")).toHaveTextContent("允许");
    expect(screen.getByTestId("pi-validation-state").querySelectorAll("tbody tr.is-current")).toHaveLength(1);
    expect(screen.getByText("核实中…")).toBeVisible();

    // 真读不到（unknown）不享受这个兜底，必须回到未知
    rerender(<PortfolioValidationSection {...baseProps} kind="unknown" title="当前交易状态待核实" runtime={null} />);
    expect(screen.getByLabelText("新买入权限")).toHaveTextContent("未知");
    expect(screen.queryByText("核实中…")).toBeNull();
  });
});
