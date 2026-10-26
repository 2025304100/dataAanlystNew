import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    getAIHealth: vi.fn(),
    getAISessions: vi.fn(),
    createAISession: vi.fn(),
    streamAISession: vi.fn(),
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
    mockApi.streamAISession.mockImplementation(async (_payload, handlers) => {
      handlers.onSession?.(1);
      handlers.onDelta?.("partial answer");
      handlers.onDone?.(1, sampleAIResponse);
    });
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
      expect(mockApi.streamAISession).toHaveBeenCalledWith(
        expect.objectContaining({
          source_page: "discovery",
          message: "Explain this candidate",
        }),
        expect.any(Object),
        expect.any(AbortSignal),
      );
    }, { timeout: 5000 });
  });

  it("test_stop_button_aborts_stream_and_keeps_partial_content", async () => {
    mockApi.getAIHealth.mockResolvedValue(healthyProfiles);
    // 流式挂起，直到 abort 才 reject（模拟真实 SSE 中断）
    mockApi.streamAISession.mockImplementation((_payload, handlers, signal) => new Promise((_resolve, reject) => {
      handlers.onDelta?.("部分回答");
      signal?.addEventListener("abort", () => {
        const err = new Error("Aborted");
        err.name = "AbortError";
        reject(err);
      });
    }));
    const user = userEvent.setup();
    renderWithProvider(<AIAssistant activeTab="discovery" onGoToSettings={() => {}} />);

    await user.click(screen.getByTestId("ai-assistant-fab"));
    const textarea = await screen.findByTestId("ai-input");
    await user.type(textarea, "讲个长回答");
    await user.click(screen.getByTestId("ai-send-button"));

    // 发送中：出现停止按钮，输入框禁用（防双发）
    const stopBtn = await screen.findByTestId("ai-stop-button");
    expect(textarea).toBeDisabled();

    await user.click(stopBtn);

    // 停止后：已流出内容保留 + 停止标注；不出现错误提示
    await waitFor(() => {
      expect(screen.getByText(/部分回答/)).toBeInTheDocument();
      expect(screen.getByText(/aiAssistant\.stopped/)).toBeInTheDocument();
      expect(screen.queryByTestId("ai-error-alert")).not.toBeInTheDocument();
    });
    // 停止后回到可发送状态
    await waitFor(() => {
      expect(screen.getByTestId("ai-send-button")).toBeInTheDocument();
    });
  });

  it("test_suggested_questions_send_on_click", async () => {
    mockApi.getAIHealth.mockResolvedValue(healthyProfiles);
    mockApi.streamAISession.mockImplementation(async (_payload, handlers) => {
      handlers.onDelta?.("ok");
      handlers.onDone?.(1, sampleAIResponse);
    });
    const user = userEvent.setup();
    renderWithProvider(<AIAssistant activeTab="discovery" onGoToSettings={() => {}} />);

    await user.click(screen.getByTestId("ai-assistant-fab"));
    const suggestions = await screen.findAllByTestId("ai-suggested-question");
    expect(suggestions.length).toBeGreaterThan(0);

    await user.click(suggestions[0]);
    await waitFor(() => {
      expect(mockApi.streamAISession).toHaveBeenCalledWith(
        expect.objectContaining({ message: "aiAssistant.suggested.opportunity1" }),
        expect.any(Object),
        expect.any(AbortSignal),
      );
    });
  });

  it("test_session_chip_bar_expands_with_search", async () => {
    mockApi.getAIHealth.mockResolvedValue(healthyProfiles);
    mockApi.getAISessions.mockResolvedValue({
      items: [
        { id: 1, title: "组合 1 检查与对账诊断", source_page: "portfolio-inspection", provider: null, model: null, profile_id: null, status: "active", context_summary: null, total_tokens: 0, total_cost: null, created_at: new Date().toISOString(), updated_at: null, deleted_at: null },
        { id: 2, title: "你好，你能干什么", source_page: "decision", provider: null, model: null, profile_id: null, status: "active", context_summary: null, total_tokens: 0, total_cost: null, created_at: new Date().toISOString(), updated_at: null, deleted_at: null },
        { id: 3, title: "旧会话甲", source_page: "settings", provider: null, model: null, profile_id: null, status: "active", context_summary: null, total_tokens: 0, total_cost: null, created_at: "2026-08-01T10:00:00.000Z", updated_at: null, deleted_at: null },
      ],
      limit: 50, offset: 0, include_archived: false,
    });
    const user = userEvent.setup();
    renderWithProvider(<AIAssistant activeTab="discovery" onGoToSettings={() => {}} />);

    await user.click(screen.getByTestId("ai-assistant-fab"));
    // chip 条：默认收起，展示历史按钮
    const historyBtn = await screen.findByTestId("ai-session-history-toggle");
    expect(historyBtn).toBeInTheDocument();

    // 展开面板 + 搜索过滤
    await user.click(historyBtn);
    const search = await screen.findByTestId("ai-session-search");
    await user.type(search, "组合");
    await waitFor(() => {
      const items = screen.getAllByTestId("ai-session-item");
      expect(items).toHaveLength(1);
      expect(items[0]).toHaveTextContent("组合 1 检查与对账诊断");
    });
  });

  it("test_copy_button_copies_message_text", async () => {
    mockApi.getAIHealth.mockResolvedValue(healthyProfiles);
    mockApi.getAISessions.mockResolvedValue({
      items: [
        { id: 7, title: "有消息的会话", source_page: "settings", provider: null, model: null, profile_id: null, status: "active", context_summary: null, total_tokens: 0, total_cost: null, created_at: new Date().toISOString(), updated_at: null, deleted_at: null },
      ],
      limit: 50, offset: 0, include_archived: false,
    });
    mockApi.getAIMessages.mockResolvedValue({
      items: [
        { id: 71, session_id: 7, role: "user", content: "帮我解释这个候选", context_summary: null, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, latency_ms: null, model_used: null, provider_used: null, metadata_json: null, created_at: new Date().toISOString() },
      ],
      session_id: 7, limit: 100, offset: 0,
    });
    const user = userEvent.setup();
    renderWithProvider(<AIAssistant activeTab="discovery" onGoToSettings={() => {}} />);

    await user.click(screen.getByTestId("ai-assistant-fab"));
    await user.click(await screen.findByTestId("ai-session-history-toggle"));
    await user.click(await screen.findByTestId("ai-session-item"));

    const copyBtn = await screen.findByTestId("ai-copy-button");
    await user.click(copyBtn);
    // user-event 会把 navigator.clipboard 换成自带 stub，写进去的内容可回读
    expect(await navigator.clipboard.readText()).toBe("帮我解释这个候选");
  });

  it("test_regenerate_resends_previous_user_message", async () => {
    mockApi.getAIHealth.mockResolvedValue(healthyProfiles);
    mockApi.getAISessions.mockResolvedValue({
      items: [
        { id: 8, title: "可重生成的会话", source_page: "settings", provider: null, model: null, profile_id: null, status: "active", context_summary: null, total_tokens: 0, total_cost: null, created_at: new Date().toISOString(), updated_at: null, deleted_at: null },
      ],
      limit: 50, offset: 0, include_archived: false,
    });
    mockApi.getAIMessages.mockResolvedValue({
      items: [
        { id: 81, session_id: 8, role: "user", content: "第一次提问", context_summary: null, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, latency_ms: null, model_used: null, provider_used: null, metadata_json: null, created_at: new Date().toISOString() },
        { id: 82, session_id: 8, role: "assistant", content: JSON.stringify(sampleAIResponse), context_summary: null, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, latency_ms: null, model_used: null, provider_used: null, metadata_json: null, created_at: new Date().toISOString() },
      ],
      session_id: 8, limit: 100, offset: 0,
    });
    mockApi.streamAISession.mockImplementation(async (_payload, handlers) => {
      handlers.onDelta?.("重新生成的回答");
      handlers.onDone?.(8, sampleAIResponse);
    });

    const user = userEvent.setup();
    renderWithProvider(<AIAssistant activeTab="discovery" onGoToSettings={() => {}} />);

    await user.click(screen.getByTestId("ai-assistant-fab"));
    await user.click(await screen.findByTestId("ai-session-history-toggle"));
    await user.click(await screen.findByTestId("ai-session-item"));

    const regenBtn = await screen.findByTestId("ai-regenerate-button");
    await user.click(regenBtn);

    await waitFor(() => {
      expect(mockApi.streamAISession).toHaveBeenCalledWith(
        expect.objectContaining({ message: "第一次提问", session_id: 8 }),
        expect.any(Object),
        expect.any(AbortSignal),
      );
    });
    // 重新生成后原 user 消息仍在（不被错误清理逻辑删掉）
    expect(screen.getByText("第一次提问")).toBeInTheDocument();
  });

  it("test_drawer_width_responds_to_viewport", async () => {
    mockApi.getAIHealth.mockResolvedValue(healthyProfiles);
    const originalWidth = window.innerWidth;
    const setWidth = (value: number) => {
      Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value });
      window.dispatchEvent(new Event("resize"));
    };
    try {
      setWidth(1366);
      const user = userEvent.setup();
      const { unmount } = renderWithProvider(<AIAssistant activeTab="discovery" onGoToSettings={() => {}} />);
      await user.click(screen.getByTestId("ai-assistant-fab"));
      await waitFor(() => {
        const wrappers = document.querySelectorAll(".ant-drawer-content-wrapper");
        const style = wrappers.length ? wrappers[wrappers.length - 1].getAttribute("style") : null;
        expect(style).toContain("width: 520px");
      });
      unmount();

      setWidth(390);
      const user2 = userEvent.setup();
      renderWithProvider(<AIAssistant activeTab="discovery" onGoToSettings={() => {}} />);
      await user2.click(screen.getByTestId("ai-assistant-fab"));
      await waitFor(() => {
        const wrappers = document.querySelectorAll(".ant-drawer-content-wrapper");
        const style = wrappers.length ? wrappers[wrappers.length - 1].getAttribute("style") : null;
        expect(style).toContain("width: 100%");
      });
    } finally {
      setWidth(originalWidth);
    }
  });
});
