// WP1.7：OpportunityCenter 组件测试
//
// 覆盖：
// - 默认显示候选池页签
// - 点击观察池/已排除/扫描记录 tab 切换
// - 未完成页签显示建设状态，不伪造数据
// - 显示"机会中心"标题
//
// 约束：
// - 不修改已稳定组件实现
// - CandidatePool/ObservationPool 通过 mock 隔离（避免 Discovery 复杂依赖）
// - ExcludedPool/ScanHistory 不 mock，验证真实建设状态文案
// - mock AppContext 提供 portfolioId
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// 使用 vi.hoisted 提升 mock context
const { mockContext } = vi.hoisted(() => ({
  mockContext: {
    portfolioId: 1 as number | null,
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

// Mock antd message（保留组件库，仅覆盖 message）
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

// Mock AppContext：只提供 OpportunityCenter 用到的 portfolioId
vi.mock("../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

// Mock CandidatePool（避免 Discovery 复杂依赖），渲染可识别占位
vi.mock("../opportunity/CandidatePool", () => ({
  __esModule: true,
  default: () => (
    <div data-opportunity-tab="candidate" data-testid="candidate-pool-mock">
      CandidatePool Mock
    </div>
  ),
}));

// Mock ObservationPool（避免 watchlistItems 加载链），渲染可识别占位
vi.mock("../opportunity/ObservationPool", () => ({
  __esModule: true,
  default: () => (
    <div data-opportunity-tab="observation" data-testid="observation-pool-mock">
      ObservationPool Mock
    </div>
  ),
}));

// 注意：ExcludedPool 与 ScanHistory 不 mock，验证真实建设状态文案
import OpportunityCenter from "../OpportunityCenter";

describe("OpportunityCenter 组件测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockContext.portfolioId = 1;
  });

  // 1. 默认显示候选池页签
  it("默认应激活候选池 tab 并显示 CandidatePool 内容", async () => {
    render(<OpportunityCenter />);
    // 候选池 mock 应被渲染
    await waitFor(() => {
      expect(screen.getByTestId("candidate-pool-mock")).toBeInTheDocument();
    });
    // 候选池 tab 应为 active（通过 antd Tabs 的 aria-selected 判定）
    const candidateTab = screen.getByRole("tab", { name: /opportunityTabCandidate/ });
    expect(candidateTab).toHaveAttribute("aria-selected", "true");
    // 容器 data-active-tab 应为 candidate
    const container = document.querySelector(".opportunity-center");
    expect(container?.getAttribute("data-active-tab")).toBe("candidate");
  });

  // 2. 点击观察池 tab 切换
  it("点击观察池 tab 应切换到 ObservationPool", async () => {
    const user = userEvent.setup();
    render(<OpportunityCenter />);
    // 初始为候选池
    expect(screen.getByTestId("candidate-pool-mock")).toBeInTheDocument();
    // 点击观察池 tab
    const observationTab = screen.getByRole("tab", { name: /opportunityTabObservation/ });
    await user.click(observationTab);
    // 观察池 tab 应为 active
    await waitFor(() => {
      expect(observationTab).toHaveAttribute("aria-selected", "true");
    });
    // ObservationPool mock 应被渲染
    expect(screen.getByTestId("observation-pool-mock")).toBeInTheDocument();
    // 容器 data-active-tab 应为 observation
    const container = document.querySelector(".opportunity-center");
    expect(container?.getAttribute("data-active-tab")).toBe("observation");
  });

  // 3. 点击已排除 tab 显示建设状态，不伪造数据
  it("点击已排除 tab 应显示建设状态且不伪造数据", async () => {
    const user = userEvent.setup();
    render(<OpportunityCenter />);
    const excludedTab = screen.getByRole("tab", { name: /opportunityTabExcluded/ });
    await user.click(excludedTab);
    // 应显示建设状态提示
    await waitFor(() => {
      expect(screen.getByText("opportunityExcludedConstructingTitle")).toBeInTheDocument();
    });
    expect(screen.getByText("opportunityExcludedConstructingDesc")).toBeInTheDocument();
    // 应显示"建设中"占位
    expect(screen.getByText("opportunityUnderConstruction")).toBeInTheDocument();
    // 容器 data-active-tab 应为 excluded
    const container = document.querySelector(".opportunity-center");
    expect(container?.getAttribute("data-active-tab")).toBe("excluded");
    // 关键约束：不伪造数据 —— 不应出现候选/观察池的 mock 内容在已排除 tab 下
    // （mock 内容仍可能存在于 DOM 中但隐藏，这里只验证当前 active tab 不是 candidate/observation）
    expect(container?.getAttribute("data-active-tab")).not.toBe("candidate");
  });

  // 4. 点击扫描记录 tab 显示建设状态
  it("点击扫描记录 tab 应显示建设状态", async () => {
    const user = userEvent.setup();
    render(<OpportunityCenter />);
    const scanHistoryTab = screen.getByRole("tab", { name: /opportunityTabScanHistory/ });
    await user.click(scanHistoryTab);
    await waitFor(() => {
      expect(screen.getByText("opportunityScanHistoryConstructingTitle")).toBeInTheDocument();
    });
    expect(screen.getByText("opportunityScanHistoryConstructingDesc")).toBeInTheDocument();
    expect(screen.getByText("opportunityUnderConstruction")).toBeInTheDocument();
    // 容器 data-active-tab 应为 scan-history
    const container = document.querySelector(".opportunity-center");
    expect(container?.getAttribute("data-active-tab")).toBe("scan-history");
  });

  // 5. 未完成页签显示建设标记（"建设中"）
  it("已排除与扫描记录 tab 应显示建设标记", async () => {
    const user = userEvent.setup();
    render(<OpportunityCenter />);

    // 切到已排除 tab，验证建设标记
    await user.click(screen.getByRole("tab", { name: /opportunityTabExcluded/ }));
    await waitFor(() => {
      // antd Tabs 默认渲染所有 pane（隐藏非 active），所以"建设中"可能多次出现
      expect(screen.getAllByText("opportunityUnderConstruction").length).toBeGreaterThan(0);
    });
    // 已排除 tab 内容容器应有 data-state="under-construction"
    const excludedContainer = document.querySelector('[data-opportunity-tab="excluded"]');
    expect(excludedContainer?.getAttribute("data-state")).toBe("under-construction");

    // 切到扫描记录 tab，验证建设标记
    await user.click(screen.getByRole("tab", { name: /opportunityTabScanHistory/ }));
    await waitFor(() => {
      expect(screen.getAllByText("opportunityUnderConstruction").length).toBeGreaterThan(0);
    });
    const scanContainer = document.querySelector('[data-opportunity-tab="scan-history"]');
    expect(scanContainer?.getAttribute("data-state")).toBe("under-construction");
  });

  // 6. 显示机会中心标题
  it("应显示 opportunityCenterTitle 标题", () => {
    render(<OpportunityCenter />);
    // 顶部 Alert message 应为 opportunityCenterTitle
    expect(screen.getByText("opportunityCenterTitle")).toBeInTheDocument();
    // 描述也应出现
    expect(screen.getByText("opportunityCenterDesc")).toBeInTheDocument();
  });

  // 7. portfolioId 通过隐藏 span 暴露用于调试/可观测性
  it("应在隐藏 span 上暴露 data-portfolio-id 用于可观测性", () => {
    mockContext.portfolioId = 42;
    render(<OpportunityCenter />);
    const hiddenSpan = document.querySelector("[data-portfolio-id]");
    expect(hiddenSpan).not.toBeNull();
    expect(hiddenSpan?.getAttribute("data-portfolio-id")).toBe("42");
  });

  // 8. 四个页签都应渲染
  it("应渲染 4 个 tab：候选/观察/已排除/扫描记录", () => {
    render(<OpportunityCenter />);
    expect(screen.getByRole("tab", { name: /opportunityTabCandidate/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /opportunityTabObservation/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /opportunityTabExcluded/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /opportunityTabScanHistory/ })).toBeInTheDocument();
  });

  // 9. portfolioId 为 null 时也应正常渲染（不依赖具体 portfolio）
  it("portfolioId 为 null 时也应正常渲染四个页签", () => {
    mockContext.portfolioId = null;
    render(<OpportunityCenter />);
    expect(screen.getByRole("tab", { name: /opportunityTabCandidate/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /opportunityTabObservation/ })).toBeInTheDocument();
    // 隐藏 span 的 data-portfolio-id 应为空字符串
    const hiddenSpan = document.querySelector("[data-portfolio-id]");
    expect(hiddenSpan?.getAttribute("data-portfolio-id")).toBe("");
  });
});
