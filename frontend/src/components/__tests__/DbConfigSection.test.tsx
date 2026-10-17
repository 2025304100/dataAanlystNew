import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ get: vi.fn(), migrate: vi.fn(), migrationStatus: vi.fn() }));
vi.mock("../../api/dbConfig", () => ({ dbConfigApi: mocks }));
vi.mock("../../i18n", () => ({ t: (key: string) => key, template: (text: string, values: object) => `${text} ${Object.values(values).join(" ")}` }));
import DbConfigSection from "../DbConfigSection";

afterEach(() => vi.restoreAllMocks());

it("ends migration busy state and shows failed tables after partial completion", async () => {
  mocks.get.mockResolvedValue({ use_mysql: true, mysql: { host: "db", database: "qa", user: "qa" } });
  mocks.migrate.mockResolvedValue({ status: "ok" });
  mocks.migrationStatus.mockResolvedValue({ status: "partial", tables_done: 2, tables_total: 2, rows_migrated: 7, failed_tables: ["daily_bars"] });
  let poll: (() => Promise<void>) | undefined;
  const originalInterval = globalThis.setInterval;
  vi.spyOn(globalThis, "setInterval").mockImplementation(((callback: () => Promise<void>, delay?: number) => {
    if (delay === 1000) { poll = callback; return 123; }
    return originalInterval(callback, delay);
  }) as typeof setInterval);
  const user = userEvent.setup();
  render(<DbConfigSection />);
  const start = await screen.findByRole("button", { name: /dbMigrate$/ });
  await user.click(start);
  await user.click(await screen.findByRole("button", { name: "ok" }));
  await waitFor(() => expect(poll).toBeDefined());
  await act(async () => { await poll!(); });
  expect(screen.getByText(/dbMigratePartial.*daily_bars/)).toBeInTheDocument();
  expect(start).toBeEnabled();
});
