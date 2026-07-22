import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    getAIHealth: vi.fn(),
    getAISessions: vi.fn(),
    createAISession: vi.fn(),
    getAIMessages: vi.fn(),
    deleteAISession: vi.fn(),
    getAIProfiles: vi.fn(),
    createAIProfile: vi.fn(),
    updateAIProfile: vi.fn(),
    deleteAIProfile: vi.fn(),
    testAIProfile: vi.fn(),
    discoverAIModels: vi.fn(),
    getAIProfileUsage: vi.fn(),
  },
}));

vi.mock("../../../api/client", () => ({ api: mockApi }));
vi.mock("../../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string) => key,
}));
vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return {
    ...actual,
    message: {
      success: vi.fn(),
      warning: vi.fn(),
      error: vi.fn(),
    },
  };
});

import { AIAssistantProvider } from "../AIAssistantContext";
import AIAssistant from "../AIAssistant";
import AISettings from "../AISettings";
import ExplainButton from "../ExplainButton";
import ExplanationCard from "../ExplanationCard";
import type { AIResponse, AIHealth } from "../../../types";

function renderWithProvider(ui: React.ReactNode) {
  return render(<AIAssistantProvider>{ui}</AIAssistantProvider>);
}

const healthyProfiles: AIHealth[] = [
  {
    id: 1,
    name: "test-profile",
    provider: "openai_compatible",
    model: "gpt-4o-mini",
    priority: 0,
    is_enabled: true,
    is_fallback: false,
    health_status: "healthy",
    last_health_check: null,
    daily_request_count: 0,
    daily_request_limit: 100,
  },
];

const emptyHealth: AIHealth[] = [];

const sampleAIResponse: AIResponse = {
  answer: "This is a test answer.",
  evidence: [
    { type: "score", source: "discovery", content: "Score: 85", confidence: 0.9 },
  ],
  warnings: ["Data may be stale"],
  suggested_actions: [
    { action_type: "view_detail", description: "View symbol detail" },
  ],
  draft: { indicator_name: "RSI" },
  metadata: {
    data_as_of: "2026-07-22",
    model_version: "gpt-4o-mini",
    provider_used: "openai_compatible",
    latency_ms: 500,
    tokens: 150,
  },
};

describe("WP-AI.7 AIAssistant", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.getAISessions.mockResolvedValue({ items: [], limit: 20, offset: 0, include_archived: false });
    mockApi.getAIMessages.mockResolvedValue({ items: [], session_id: 0, limit: 100, offset: 0 });
    mockApi.createAISession.mockResolvedValue({ session_id: 1, response: sampleAIResponse });
    mockApi.getAIProfiles.mockResolvedValue([]);
    mockApi.testAIProfile.mockResolvedValue({ success: true, latency_ms: 100, model_info: null, error: null });
    mockApi.discoverAIModels.mockResolvedValue({ models: [] });
    mockApi.getAIProfileUsage.mockResolvedValue({
      profile_id: 1,
      daily_request_count: 0,
      daily_request_limit: 100,
      remaining: 100,
      daily_request_reset_at: null,
    });
  });

  it("test_floating_button_renders", () => {
    mockApi.getAIHealth.mockResolvedValue(healthyProfiles);
    renderWithProvider(<AIAssistant activeTab="discovery" onGoToSettings={() => {}} />);
    expect(screen.getByTestId("ai-assistant-fab")).toBeInTheDocument();
  });

  it("test_drawer_opens_on_click", async () => {
    const user = userEvent.setup();
    mockApi.getAIHealth.mockResolvedValue(healthyProfiles);
    renderWithProvider(<AIAssistant activeTab="discovery" onGoToSettings={() => {}} />);

    const fab = screen.getByTestId("ai-assistant-fab");
    await user.click(fab);

    await waitFor(() => {
      expect(screen.getByTestId("ai-assistant-drawer")).toBeInTheDocument();
    });
  });

  it("test_not_configured_shows_message", async () => {
    const user = userEvent.setup();
    mockApi.getAIHealth.mockResolvedValue(emptyHealth);
    renderWithProvider(<AIAssistant activeTab="discovery" onGoToSettings={() => {}} />);

    const fab = screen.getByTestId("ai-assistant-fab");
    await user.click(fab);

    await waitFor(() => {
      expect(screen.getByTestId("ai-not-configured-alert")).toBeInTheDocument();
    });
  });

  it("test_explanation_card_renders", () => {
    renderWithProvider(<ExplanationCard response={sampleAIResponse} />);
    expect(screen.getByTestId("ai-explanation-card")).toBeInTheDocument();
    expect(screen.getByText("This is a test answer.")).toBeInTheDocument();
  });

  it("test_explain_button_on_discovery", async () => {
    const user = userEvent.setup();
    mockApi.getAIHealth.mockResolvedValue(healthyProfiles);
    renderWithProvider(
      <>
        <ExplainButton
          sourcePage="discovery"
          references={{ candidate_id: 1, symbol_id: 100 }}
        />
        <AIAssistant activeTab="discovery" onGoToSettings={() => {}} />
      </>,
    );

    const explainBtn = screen.getByTestId("ai-explain-button");
    await user.click(explainBtn);

    await waitFor(() => {
      expect(screen.getByTestId("ai-assistant-drawer")).toBeInTheDocument();
    });
  });

  it("test_ai_settings_page_renders", async () => {
    mockApi.getAIHealth.mockResolvedValue(healthyProfiles);
    renderWithProvider(<AISettings />);

    expect(screen.getByTestId("ai-settings-page")).toBeInTheDocument();
    expect(screen.getByTestId("ai-health-card")).toBeInTheDocument();
  });

  it("test_connection_error_displayed", async () => {
    const user = userEvent.setup();
    // getAIHealth 抛出网络错误 → configured=false → 显示未配置提示
    mockApi.getAIHealth.mockRejectedValue(new Error("Network error: failed to connect"));
    renderWithProvider(<AIAssistant activeTab="discovery" onGoToSettings={() => {}} />);

    const fab = screen.getByTestId("ai-assistant-fab");
    await user.click(fab);

    await waitFor(() => {
      expect(screen.getByTestId("ai-not-configured-alert")).toBeInTheDocument();
    });
  });

  it("test_send_message_creates_session", async () => {
    mockApi.getAIHealth.mockResolvedValue(healthyProfiles);
    renderWithProvider(<AIAssistant activeTab="discovery" onGoToSettings={() => {}} />);

    // 打开抽屉
    fireEvent.click(screen.getByTestId("ai-assistant-fab"));
    await waitFor(() => {
      expect(screen.getByTestId("ai-assistant-drawer")).toBeInTheDocument();
    });

    // 等待输入区域渲染
    const inputEl = await screen.findByTestId("ai-input");
    // data-testid 可能在 textarea 本身或其包装元素上
    const visibleTextarea = inputEl.tagName === "TEXTAREA"
      ? inputEl
      : inputEl.querySelector("textarea");
    expect(visibleTextarea).toBeTruthy();
    if (visibleTextarea) {
      fireEvent.change(visibleTextarea, { target: { value: "Explain this candidate" } });
    }

    // 等待 React 状态更新后发送按钮变为可用并点击
    await waitFor(() => {
      expect(screen.getByTestId("ai-send-button")).not.toBeDisabled();
    });
    fireEvent.click(screen.getByTestId("ai-send-button"));

    await waitFor(() => {
      expect(mockApi.createAISession).toHaveBeenCalledWith(
        expect.objectContaining({
          source_page: "discovery",
          message: "Explain this candidate",
        }),
      );
    }, { timeout: 5000 });
  });
});
