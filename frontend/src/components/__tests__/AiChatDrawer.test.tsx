import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { streamAIChat } = vi.hoisted(() => ({ streamAIChat: vi.fn() }));

vi.mock("../../api/client", () => ({ api: { streamAIChat } }));
vi.mock("../../i18n", () => ({ t: (key: string) => key }));

import AiChatDrawer from "../AiChatDrawer";

describe("AiChatDrawer factor mode", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    streamAIChat.mockImplementation(async (_payload: any, handlers: any) => {
      handlers.onDelta("```formula\n");
      handlers.onDelta("(turnover_rate - mean(turnover_rate, 20)) / max(stddev(turnover_rate, 20), 0.000001)");
      handlers.onDelta("\n```");
      handlers.onDone({
        ok: true,
        reply: "```formula\n(turnover_rate - mean(turnover_rate, 20)) / max(stddev(turnover_rate, 20), 0.000001)\n```",
        error: "",
      });
    });
  });

  it("requests a factor formula and only applies it after confirmation", async () => {
    const onInsertFormula = vi.fn();
    render(
      <AiChatDrawer
        open
        formula="turnover_rate"
        formulaMode="factor"
        onClose={() => {}}
        onInsertFormula={onInsertFormula}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("aiChatPlaceholder"), {
      target: { value: "写一个换手率标准分" },
    });
    fireEvent.click(screen.getByRole("button", { name: /aiSend/ }));

    await waitFor(() => expect(streamAIChat).toHaveBeenCalledWith(expect.objectContaining({
      formula: "turnover_rate",
      formula_mode: "factor",
    }), expect.any(Object), expect.any(Object)));
    expect(onInsertFormula).not.toHaveBeenCalled();

    fireEvent.click(await screen.findByText("aiInsertFormula"));
    expect(onInsertFormula).toHaveBeenCalledWith(
      "(turnover_rate - mean(turnover_rate, 20)) / max(stddev(turnover_rate, 20), 0.000001)",
    );
  });

  it("renders the final reply when the gateway sends no deltas", async () => {
    streamAIChat.mockImplementationOnce(async (_payload: any, handlers: any) => {
      handlers.onDone({ ok: true, reply: "最终回复", error: "" });
    });
    render(
      <AiChatDrawer
        open
        formula="close"
        formulaMode="factor"
        onClose={() => {}}
        onInsertFormula={() => {}}
      />,
    );
    fireEvent.change(screen.getByPlaceholderText("aiChatPlaceholder"), {
      target: { value: "解释当前公式" },
    });
    fireEvent.click(screen.getByRole("button", { name: /aiSend/ }));
    await waitFor(() => expect(screen.getByText("最终回复")).toBeInTheDocument());
  });

  it("ignores callbacks from an aborted stream after closing and reopening the chat", async () => {
    let oldHandlers: any;
    let newHandlers: any;
    streamAIChat.mockImplementationOnce((_payload: any, handlers: any) => { oldHandlers = handlers; return new Promise(() => {}); });
    streamAIChat.mockImplementationOnce((_payload: any, handlers: any) => { newHandlers = handlers; return new Promise(() => {}); });
    const props = { formula: "close", formulaMode: "factor" as const, onClose: vi.fn(), onInsertFormula: vi.fn() };
    const chat = render(<AiChatDrawer {...props} open />);
    fireEvent.change(screen.getByPlaceholderText("aiChatPlaceholder"), { target: { value: "old request" } });
    fireEvent.click(screen.getByRole("button", { name: /aiSend/ }));
    await waitFor(() => expect(streamAIChat).toHaveBeenCalledTimes(1));
    chat.rerender(<AiChatDrawer {...props} open={false} />);
    chat.rerender(<AiChatDrawer {...props} open />);
    fireEvent.change(screen.getByPlaceholderText("aiChatPlaceholder"), { target: { value: "new request" } });
    fireEvent.click(screen.getByRole("button", { name: /aiSend/ }));
    await waitFor(() => expect(streamAIChat).toHaveBeenCalledTimes(2));
    await act(async () => { newHandlers.onDelta("current answer"); oldHandlers.onDelta(" obsolete answer"); oldHandlers.onDone({ ok: false, error: "obsolete failure" }); });
    expect(screen.getByText("current answer")).toBeInTheDocument();
    expect(screen.queryByText(/obsolete answer|obsolete failure/)).not.toBeInTheDocument();
  });
});
