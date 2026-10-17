import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    getAiConfig: vi.fn(),
    updateAiConfig: vi.fn(),
    testAiConnection: vi.fn(),
    listAiModels: vi.fn(),
  },
}));

vi.mock("../../api/client", () => ({ api: mockApi }));
vi.mock("../../i18n", () => ({ t: (key: string) => key }));
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

import AiConfigSection from "../AiConfigSection";

describe("AiConfigSection persistence controls", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.getAiConfig.mockResolvedValue({
      provider: "openai_compatible",
      service_url: "",
      api_key: "",
      model: "",
      enabled: false,
      auth_type: "bearer",
      auth_header: "Authorization",
      chat_path: "/chat/completions",
      models_path: "/models",
      timeout_seconds: 30,
      temperature: 0.3,
      max_tokens: 1024,
      extra_headers: {},
      api_key_set: false,
      persisted: false,
      updated_at: null,
    });
    mockApi.updateAiConfig.mockResolvedValue({ status: "ok" });
    mockApi.listAiModels.mockResolvedValue({ models: [] });
    mockApi.testAiConnection.mockResolvedValue({ success: true, message: "ok" });
  });

  it("reactively enables save and submits entered values", async () => {
    const user = userEvent.setup();
    render(<AiConfigSection />);

    await waitFor(() => expect(mockApi.getAiConfig).toHaveBeenCalled());
    const saveButton = screen.getByRole("button", { name: "aiSave" });
    expect(saveButton).toBeDisabled();

    await user.type(
      screen.getByPlaceholderText("https://api.openai.com/v1"),
      "https://gateway.example/v1",
    );
    await user.type(screen.getByPlaceholderText("sk-..."), "secret-key");

    await waitFor(() => expect(saveButton).toBeEnabled());
    await user.click(saveButton);

    await waitFor(() => expect(mockApi.updateAiConfig).toHaveBeenCalledTimes(1));
    expect(mockApi.updateAiConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        provider: "openai_compatible",
        service_url: "https://gateway.example/v1",
        api_key: "secret-key",
        auth_type: "bearer",
      }),
    );
  });

  it("shows the saved configuration's verified connection status after reopening", async () => {
    mockApi.getAiConfig.mockResolvedValueOnce({ service_url: "https://offline.example/v1", api_key: "****", model: "verified", enabled: true, auth_type: "bearer", persisted: true, connection_test: { status: "passed", tested_at: "2026-10-15T07:00:00Z" } });
    render(<AiConfigSection />);
    expect(await screen.findByText("aiConnectionTestPassed")).toBeInTheDocument();
  });

  it("clears verified status as soon as the displayed configuration changes", async () => {
    mockApi.getAiConfig.mockResolvedValueOnce({ service_url: "https://offline.example/v1", api_key: "", model: "verified", enabled: true, auth_type: "none", persisted: true, connection_test: { status: "passed", tested_at: "2026-10-15T07:00:00Z" } });
    render(<AiConfigSection />);
    await screen.findByText("aiConnectionTestPassed");
    fireEvent.change(screen.getByRole("combobox", { name: /aiModel/ }), { target: { value: "changed-model" } });
    expect(screen.queryByText("aiConnectionTestPassed")).not.toBeInTheDocument();
    expect(screen.getByText("aiConnectionTestChanged")).toBeInTheDocument();
  });

  it("does not show a late success for a model edited while its connection test was running", async () => {
    mockApi.getAiConfig.mockResolvedValueOnce({ service_url: "https://offline.example/v1", api_key: "", model: "model-A", enabled: true, auth_type: "none", persisted: true });
    let resolveTest!: (value: unknown) => void;
    mockApi.testAiConnection.mockReturnValueOnce(new Promise((resolve) => { resolveTest = resolve; }));
    render(<AiConfigSection />);
    const model = await screen.findByRole("combobox", { name: /aiModel/ });
    await waitFor(() => expect(model).toHaveValue("model-A"));
    const testButton = screen.getByRole("button", { name: /aiTestConn/ });
    await waitFor(() => expect(testButton).toBeEnabled());
    fireEvent.click(testButton);
    fireEvent.change(model, { target: { value: "model-B" } });
    await act(async () => resolveTest({ success: true, message: "STALE_OK", connection_test: { status: "passed", tested_at: "2026-10-15T07:00:00Z" } }));
    expect(screen.queryByText("STALE_OK")).not.toBeInTheDocument();
    expect(screen.getByText("aiConnectionTestChanged")).toBeInTheDocument();
  });
});
