import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
const { health } = vi.hoisted(() => ({ health: vi.fn() }));
vi.mock("../../api/client", () => ({ api: { getSymbols: async () => [], getSymbolDataHealth: health } }));
vi.mock("../../context/AppContext", () => ({ useApp: () => ({ activeSymbolId: null }) }));
vi.mock("../../i18n", () => ({ t: (key: string) => key }));
import DataDiagnosticPanel from "../DataDiagnosticPanel";
afterEach(cleanup);
it("keeps the selected symbol health when the previous request arrives last", async () => {
  let oldResolve!: (value: unknown) => void;
  health.mockImplementation((id: number) => id === 1 ? new Promise((resolve) => { oldResolve = resolve; }) : Promise.resolve(result(id, 222)));
  const view = render(<DataDiagnosticPanel symbolId={1} />);
  await waitFor(() => expect(health).toHaveBeenCalledWith(1));
  view.rerender(<DataDiagnosticPanel symbolId={2} />);
  expect(await screen.findByText("222")).toBeVisible();
  await act(async () => oldResolve(result(1, 111)));
  expect(screen.getByText("222")).toBeVisible();
  expect(screen.queryByText("111")).not.toBeInTheDocument();
});
function result(symbol_id: number, total_bars: number) {
  return { symbol_id, summary: { total_bars, total_scores: 0 }, coverage: {}, score_coverage: {}, issues: [], suggestions: [] };
}
