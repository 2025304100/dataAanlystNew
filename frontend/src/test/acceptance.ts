import { act } from "@testing-library/react";
import { vi } from "vitest";
import { startAcceptanceMonitor, refreshAcceptanceMode } from "../utils/acceptanceMode";

export async function enterReadOnlyMode() {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
    JSON.stringify({ mode: "inspection_acceptance", read_only: true }), { status: 200 },
  ));
  let stop = () => {};
  await act(async () => { stop = startAcceptanceMonitor(); await refreshAcceptanceMode(); });
  return async () => { await act(async () => { stop(); fetch.mockRestore(); }); };
}
