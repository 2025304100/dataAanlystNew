// WP1.7：OpportunityCenter 组件测试
//
// 覆盖：
// - 默认显示候选池页签
// - 点击观察池/已排除/扫描记录 tab 切换
// - 已排除/扫描记录 tab 渲染真实组件（UAT-PAGES.2 移除建设占位）
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

// Mock ExcludedPool（UAT-PAGES.2 已替换建设占位为真实组件，测试验证组件渲染）
vi.mock("../opportunity/ExcludedPool", () => ({
  __esModule: true,
  default: () => (
    <div data-opportunity-tab="excluded" data-testid="excluded-pool-mock">
      ExcludedPool Mock
    </div>
  ),
}));

// Mock ScanHistory（UAT-PAGES.2 已替换建设占位为真实组件，测试验证组件渲染）
vi.mock("../opportunity/ScanHistory", () => ({
  __esModule: true,
  default: () => (
    <div data-opportunity-tab="scan-history" data-testid="scan-history-mock">
      ScanHistory Mock
    </div>
  ),
}));

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

  // 3. 点击已排除 tab 渲染 ExcludedPool 真实组件（UAT-PAGES.2 移除建设占位）
  it("点击已排除 tab 应渲染 ExcludedPool 组件而非建设占位", async () => {
    const user = userEvent.setup();
    render(<OpportunityCenter />);
    const excludedTab = screen.getByRole("tab", { name: /opportunityTabExcluded/ });
    await user.click(excludedTab);
    // 应渲染 ExcludedPool mock（真实组件已在 ExcludedPool.test.tsx 单独覆盖）
    await waitFor(() => {
      expect(screen.getByTestId("excluded-pool-mock")).toBeInTheDocument();
    });
    // 不应再显示"建设中"占位文案
    expect(screen.queryByText("opportunityExcludedConstructingTitle")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityUnderConstruction")).not.toBeInTheDocument();
    // 容器 data-active-tab 应为 excluded
    const container = document.querySelector(".opportunity-center");
    expect(container?.getAttribute("data-active-tab")).toBe("excluded");
  });

  // 4. 点击扫描记录 tab 渲染 ScanHistory 真实组件（UAT-PAGES.2 移除建设占位）
  it("点击扫描记录 tab 应渲染 ScanHistory 组件而非建设占位", async () => {
    const user = userEvent.setup();
    render(<OpportunityCenter />);
    const scanHistoryTab = screen.getByRole("tab", { name: /opportunityTabScanHistory/ });
    await user.click(scanHistoryTab);
    // 应渲染 ScanHistory mock（真实组件已在 ScanHistory.test.tsx 单独覆盖）
    await waitFor(() => {
      expect(screen.getByTestId("scan-history-mock")).toBeInTheDocument();
    });
    // 不应再显示"建设中"占位文案
    expect(screen.queryByText("opportunityScanHistoryConstructingTitle")).not.toBeInTheDocument();
    expect(screen.queryByText("opportunityUnderConstruction")).not.toBeInTheDocument();
    // 容器 data-active-tab 应为 scan-history
    const container = document.querySelector(".opportunity-center");
    expect(container?.getAttribute("data-active-tab")).toBe("scan-history");
  });

  // 5. 已排除与扫描记录 tab 渲染真实组件而非建设占位（UAT-PAGES.2）
  it("已排除与扫描记录 tab 应渲染真实组件且无建设占位标记", async () => {
    const user = userEvent.setup();
    render(<OpportunityCenter />);

    // 切到已排除 tab，验证真实组件渲染且无建设占位
    await user.click(screen.getByRole("tab", { name: /opportunityTabExcluded/ }));
    await waitFor(() => {
      expect(screen.getByTestId("excluded-pool-mock")).toBeInTheDocument();
    });
    // 不应存在"建设中"文案
    expect(screen.queryByText("opportunityUnderConstruction")).not.toBeInTheDocument();
    // 已排除 tab 内容容器不应有 data-state="under-construction"
    const excludedContainer = document.querySelector('[data-opportunity-tab="excluded"]');
    expect(excludedContainer?.getAttribute("data-state")).not.toBe("under-construction");

    // 切到扫描记录 tab，验证真实组件渲染且无建设占位
    await user.click(screen.getByRole("tab", { name: /opportunityTabScanHistory/ }));
    await waitFor(() => {
      expect(screen.getByTestId("scan-history-mock")).toBeInTheDocument();
    });
    expect(screen.queryByText("opportunityUnderConstruction")).not.toBeInTheDocument();
    const scanContainer = document.querySelector('[data-opportunity-tab="scan-history"]');
    expect(scanContainer?.getAttribute("data-state")).not.toBe("under-construction");
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

  // ============================================================================
  // P1-09：discovery → opportunity 兼容跳转后的落地行为 + 导航去重
  // ============================================================================

  // 10. P1-09 跳转后默认显示候选池页签（discovery → opportunity 兼容跳转落地页）
  it("P1-09：discovery → opportunity 兼容跳转后应默认落地候选池页签", async () => {
    // 模拟旧"机会挖掘"入口点击后兼容跳转到"机会中心"的落地状态
    // App.tsx 中点击 discovery 按钮会 setActiveTab("opportunity")，此处渲染 OpportunityCenter
    render(<OpportunityCenter />);
    // 落地后候选池 mock 应被渲染
    await waitFor(() => {
      expect(screen.getByTestId("candidate-pool-mock")).toBeInTheDocument();
    });
    // 候选池 tab 应为 active（aria-selected="true"）
    const candidateTab = screen.getByRole("tab", { name: /opportunityTabCandidate/ });
    expect(candidateTab).toHaveAttribute("aria-selected", "true");
    // 容器 data-active-tab 应为 candidate（跳转后默认候选池，而非其他页签）
    const container = document.querySelector(".opportunity-center");
    expect(container?.getAttribute("data-active-tab")).toBe("candidate");
  });

  // 11. 导航去重：不应渲染 discovery 相关页签（机会中心统一承接，无重复入口）
  it("P1-09：导航去重——不应渲染 tabDiscovery 页签或重复的 discovery 入口", () => {
    render(<OpportunityCenter />);
    // 不应存在名为 tabDiscovery 的 tab（机会挖掘已统一到机会中心）
    expect(screen.queryByRole("tab", { name: /tabDiscovery/ })).not.toBeInTheDocument();
    // 四个页签均为 opportunity 专属，无 discovery 重复
    const allTabs = screen.getAllByRole("tab");
    const tabNames = allTabs.map(tab => tab.textContent);
    // 不应包含 tabDiscovery 文案
    expect(tabNames).not.toContain("tabDiscovery");
  });

  // 12. 导航去重：四个页签 key 应唯一（无重复导航项）
  it("P1-09：导航去重——四个页签应唯一且无重复 key", () => {
    render(<OpportunityCenter />);
    const allTabs = screen.getAllByRole("tab");
    // 应恰好 4 个页签
    expect(allTabs).toHaveLength(4);
    // 页签文案应唯一（无重复导航项）
    const tabNames = allTabs.map(tab => tab.textContent);
    const uniqueNames = new Set(tabNames);
    expect(uniqueNames.size).toBe(4);
  });

  // 13. 导航去重：机会中心不应渲染 discovery 组件内容（同一对象只维护一份状态）
  it("P1-09：导航去重——机会中心容器不应包含 discovery 标记内容", () => {
    render(<OpportunityCenter />);
    const container = document.querySelector(".opportunity-center");
    expect(container).not.toBeNull();
    // 容器 data-active-tab 应为四个有效页签之一，不应为 discovery
    const activeTab = container?.getAttribute("data-active-tab");
    expect(["candidate", "observation", "excluded", "scan-history"]).toContain(activeTab);
    expect(activeTab).not.toBe("discovery");
  });
});
