import { afterEach, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { setLocale } from "../../../../../i18n";
import PoolImportPanel from "./PoolImportPanel";
import PoolMemberTable from "./PoolMemberTable";

afterEach(() => setLocale("zh-CN"));

it("import status and member catalog labels translate while unknown labels and stock names remain intact", () => {
  setLocale("en-US");
  render(<>
    <PoolImportPanel locked={false} busy={false} onTemplate={vi.fn()} onFile={vi.fn()}
      onDownloadErrors={vi.fn()} onConfirmImport={vi.fn()} result={{ total_rows: 2, admitted_count: 1,
        error_count: 1, has_errors: true, counts: { success: 1, custom_status: 1 },
        counts_label_zh: { success: "成功", custom_status: "供应商自定义说明" }, rows: [
          { row_no: 1, status: "success", status_label_zh: "成功", symbol: "000001", name: "平安银行" },
          { row_no: 2, status: "custom_status", status_label_zh: "供应商自定义说明", symbol: "X" },
        ] }} />
    <PoolMemberTable members={[{ symbol_id: 1, symbol: "000001", name: "平安银行", market: "sz", board: "main",
      source: "manual", integrity: "vendor-integrity" }]} selected={[]} locked={false} onToggle={vi.fn()} onToggleAll={vi.fn()} />
  </>);
  expect(document.querySelector('[data-import-count="success"]')?.textContent).toContain("Success");
  expect(document.querySelector("[data-import-rows]")?.textContent).not.toContain("成功");
  expect(document.querySelector("[data-import-rows]")?.textContent).toContain("供应商自定义说明");
  const members = document.querySelector("[data-pool-member-table]");
  expect(members?.textContent).toContain("Shenzhen / Main");
  expect(members?.textContent).toContain("Manual entry");
  expect(members?.textContent).toContain("vendor-integrity");
  expect(members?.textContent).toContain("平安银行");
  expect(screen.getByRole("checkbox", { name: "Select all" })).toBeTruthy();
});
