import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
});
