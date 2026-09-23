import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, waitFor } from "@testing-library/react";

/**
 * C2 / B4：因子模板配置页（列表渲染 + seed / copy / toggle 回调）。
 */
vi.mock("../../../../api/factorTemplates", () => ({
  factorTemplatesApi: {
    list: vi.fn(async () => ({
      items: [
        { template_id: "tp-1", name: "动量因子", scope: "system", enabled: 1, version: "v1" },
        { template_id: "tp-2", name: "反转因子", scope: "personal", enabled: 0, version: "v2" },
      ],
      total: 2,
    })),
    seed: vi.fn(async () => ({ created: 0, total_system: 25 })),
    copy: vi.fn(async (id: string) => ({ template_id: `${id}-copy`, name: "", scope: "personal", enabled: 1 })),
    setEnabled: vi.fn(async (id: string, enabled: number) => ({ template_id: id, name: "", enabled })),
  },
}));

import { factorTemplatesApi } from "../../../../api/factorTemplates";
import TemplateConfigPage from "./TemplateConfigPage";

describe("TemplateConfigPage（C2 / B4）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("未激活不拉取（门控）", () => {
    render(<TemplateConfigPage active={false} />);
    expect(factorTemplatesApi.list).not.toHaveBeenCalled();
  });

  it("激活时渲染列表（scope/enabled 列）", async () => {
    render(<TemplateConfigPage active />);
    await waitFor(() => {
      expect(document.querySelector("[data-tpl-table]")).toBeTruthy();
    });
    const rows = document.querySelectorAll("[data-tpl-table] tbody tr");
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain("system");
    expect(rows[0].textContent).toContain("已启用");
    expect(rows[1].textContent).toContain("已停用");
  });

  it("点击复制 → 调 copy", async () => {
    render(<TemplateConfigPage active />);
    await waitFor(() => {
      expect(document.querySelector("[data-tpl-copy='tp-1']")).toBeTruthy();
    });
    fireEvent.click(document.querySelector("[data-tpl-copy='tp-1']") as HTMLElement);
    await waitFor(() => {
      expect(factorTemplatesApi.copy).toHaveBeenCalledWith("tp-1");
    });
  });

  it("点击启停 → 调 setEnabled（反向）", async () => {
    render(<TemplateConfigPage active />);
    await waitFor(() => {
      expect(document.querySelector("[data-tpl-toggle='tp-1']")).toBeTruthy();
    });
    fireEvent.click(document.querySelector("[data-tpl-toggle='tp-1']") as HTMLElement);
    await waitFor(() => {
      expect(factorTemplatesApi.setEnabled).toHaveBeenCalledWith("tp-1", 0);
    });
  });

  it("空态：list 返回空数组 → 渲染空态", async () => {
    vi.mocked(factorTemplatesApi.list).mockResolvedValueOnce({ items: [], total: 0 });
    render(<TemplateConfigPage active />);
    await waitFor(() => {
      expect(document.querySelector("[data-tpl-empty]")).toBeTruthy();
    });
  });

  it("点击重置系统模板 → 调 seed", async () => {
    render(<TemplateConfigPage active />);
    await waitFor(() => {
      expect(document.querySelector("[data-tpl-seed]")).toBeTruthy();
    });
    fireEvent.click(document.querySelector("[data-tpl-seed]") as HTMLElement);
    await waitFor(() => {
      expect(factorTemplatesApi.seed).toHaveBeenCalled();
    });
  });
});