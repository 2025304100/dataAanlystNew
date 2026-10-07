import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, waitFor } from "@testing-library/react";

/**
 * C2 / B4：因子模板配置页（设计 §6.3.11）。
 *
 * 覆盖三层：
 *   1. 列表渲染（类型译名 / 公式结构 / 优先级 / scope / enabled）；
 *   2. 工具条（名称搜索 / 类型筛选 / 优先级排序 + 方向切换 / 无命中态）；
 *   3. 详情弹窗（只读：公式 / 经济逻辑 / 参数范围 / 依赖字段，且**无表单控件**）。
 */
vi.mock("../../../../api/factorTemplates", () => ({
  factorTemplatesApi: {
    list: vi.fn(async () => ({
      items: [
        {
          template_id: "tp-1", name: "动量因子", scope: "system", owner: "system",
          enabled: 1, version: "v1", created_at: "2026-09-20T15:55:35",
          rule_config: {
            category: "trend", formula: "ema(close,{n1})-ema(close,{n2})",
            priority: 1, params: { n1: [12], n2: [26] },
            required_fields: ["close"],
            economy_logic_zh: "快慢均线差值，趋势强度指标",
          },
        },
        {
          template_id: "tp-2", name: "反转因子", scope: "personal", owner: "local_user",
          enabled: 0, version: "v2", created_at: "2026-09-21T10:00:00",
          rule_config: {
            category: "reversal", formula: "-cs_rank(pb)",
            priority: 3, params: {},
            required_fields: ["pb"],
            economy_logic_zh: "低 PB 股票未来收益可能更高（价值效应）",
          },
        },
        {
          template_id: "tp-3", name: "波动因子", scope: "system", owner: "system",
          enabled: 1, version: "v1", created_at: "2026-09-22T09:00:00",
          rule_config: {
            category: "volatility", formula: "ts_atr(close,{n1})/close",
            priority: 2, params: { n1: [14, 20] },
            required_fields: ["close"],
            economy_logic_zh: "真实波幅占价格比例，波动率水平",
          },
        },
      ],
      total: 3,
    })),
    seed: vi.fn(async () => ({ created: 0, total_system: 25 })),
    copy: vi.fn(async (id: string) => ({ template_id: `${id}-copy`, name: "", scope: "personal", enabled: 1 })),
    setEnabled: vi.fn(async (id: string, enabled: number) => ({ template_id: id, name: "", enabled })),
  },
}));

import { factorTemplatesApi } from "../../../../api/factorTemplates";
import TemplateConfigPage from "./TemplateConfigPage";

/** 等表格出现并取行（默认排序：优先级升序 → tp-1(1) / tp-3(2) / tp-2(3)） */
async function renderReady(): Promise<Element[]> {
  render(<TemplateConfigPage active />);
  await waitFor(() => {
    expect(document.querySelector("[data-tpl-table]")).toBeTruthy();
  });
  return Array.from(document.querySelectorAll("[data-tpl-table] tbody tr"));
}

function cellOf(row: Element, key: string): string {
  return (row.querySelector(`[data-tpl-cell='${key}']`)?.textContent ?? "").trim();
}

describe("TemplateConfigPage（C2 / B4）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("未激活不拉取（门控）", () => {
    render(<TemplateConfigPage active={false} />);
    expect(factorTemplatesApi.list).not.toHaveBeenCalled();
  });

  it("渲染列表：类型译名 + 公式结构 + 优先级 + scope/enabled 原值", async () => {
    const rows = await renderReady();
    expect(rows.length).toBe(3);

    // 默认按优先级升序
    expect(rows[0].textContent).toContain("动量因子");
    expect(rows[1].textContent).toContain("波动因子");
    expect(rows[2].textContent).toContain("反转因子");

    // 类型已翻译（§3.4 i18n 红线：不得把英文 key 直接上屏）
    expect(cellOf(rows[0], "category")).toBe("趋势");
    expect(cellOf(rows[1], "category")).toBe("波动");
    expect(cellOf(rows[2], "category")).toBe("反转");
    const body = document.querySelector("[data-tpl-table] tbody")!.textContent ?? "";
    expect(body).not.toContain("trend");
    expect(body).not.toContain("volatility");
    expect(body).not.toContain("reversal");

    // 公式结构 / 优先级
    expect(cellOf(rows[0], "formula")).toBe("ema(close,{n1})-ema(close,{n2})");
    expect(cellOf(rows[1], "priority")).toBe("2");

    // scope 保持后端原值（审计按原值核对），enabled 走译名
    expect(rows[0].textContent).toContain("system");
    expect(rows[0].textContent).toContain("已启用");
    expect(rows[2].textContent).toContain("personal");
    expect(rows[2].textContent).toContain("已停用");
  });

  it("按名称搜索（同时命中公式）", async () => {
    await renderReady();
    const box = document.querySelector("[data-tpl-search]") as HTMLInputElement;
    fireEvent.change(box, { target: { value: "反转" } });
    await waitFor(() => {
      expect(document.querySelectorAll("[data-tpl-table] tbody tr").length).toBe(1);
    });

    // 公式也参与匹配
    fireEvent.change(box, { target: { value: "cs_rank" } });
    await waitFor(() => {
      expect(document.querySelectorAll("[data-tpl-table] tbody tr").length).toBe(1);
    });
    expect(document.querySelector("[data-tpl-table] tbody tr")!.textContent)
      .toContain("反转因子");
  });

  it("搜索无命中 → 显示无匹配态（与「库为空」区分）", async () => {
    await renderReady();
    fireEvent.change(document.querySelector("[data-tpl-search]") as HTMLElement, {
      target: { value: "不存在的模板名" },
    });
    await waitFor(() => {
      expect(document.querySelector("[data-tpl-no-match]")).toBeTruthy();
    });
    expect(document.querySelector("[data-tpl-empty]")).toBeNull();
    expect(document.querySelector("[data-tpl-table]")).toBeNull();
  });

  it("按类型筛选", async () => {
    await renderReady();
    fireEvent.change(document.querySelector("[data-tpl-cat]") as HTMLElement, {
      target: { value: "volatility" },
    });
    await waitFor(() => {
      expect(document.querySelectorAll("[data-tpl-table] tbody tr").length).toBe(1);
    });
    expect(document.querySelector("[data-tpl-table] tbody tr")!.textContent)
      .toContain("波动因子");
  });

  it("切换排序方向 → 顺序反转", async () => {
    await renderReady();
    fireEvent.click(document.querySelector("[data-tpl-sort-dir]") as HTMLElement);
    await waitFor(() => {
      expect(document.querySelector("[data-tpl-table] tbody tr")!.textContent)
        .toContain("反转因子"); // 优先级 3 升到第一位
    });
  });

  it("详情弹窗：只读展示公式/经济逻辑/参数范围/依赖字段，且无表单控件", async () => {
    const rows = await renderReady();
    fireEvent.click(rows[1].querySelector("[data-tpl-detail]") as HTMLElement); // 波动因子
    await waitFor(() => {
      expect(document.querySelector("[data-tpl-detail-modal]")).toBeTruthy();
    });

    const modal = document.querySelector("[data-tpl-detail-modal]") as HTMLElement;
    expect(modal.querySelector("[data-tpl-detail-formula]")!.textContent)
      .toBe("ts_atr(close,{n1})/close");
    expect(modal.querySelector("[data-tpl-detail-economy]")!.textContent)
      .toBe("真实波幅占价格比例，波动率水平");
    expect(modal.querySelector("[data-tpl-detail-params]")!.textContent)
      .toBe("n1: 14~20");
    expect(modal.querySelector("[data-tpl-detail-fields]")!.textContent).toBe("close");

    // 只读约束（同 PoolAnalysisModal）：弹窗内不得出现输入控件
    expect(modal.querySelectorAll("input, select, textarea").length).toBe(0);

    fireEvent.click(modal.querySelector("[data-tpl-detail-close]") as HTMLElement);
    await waitFor(() => {
      expect(document.querySelector("[data-tpl-detail-modal]")).toBeNull();
    });
  });

  it("点击复制 → 调 copy", async () => {
    const rows = await renderReady();
    fireEvent.click(rows[0].querySelector("[data-tpl-copy]") as HTMLElement);
    await waitFor(() => {
      expect(factorTemplatesApi.copy).toHaveBeenCalledWith("tp-1");
    });
  });

  it("点击启停 → 调 setEnabled（反向）", async () => {
    const rows = await renderReady();
    fireEvent.click(rows[0].querySelector("[data-tpl-toggle]") as HTMLElement);
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
    await renderReady();
    fireEvent.click(document.querySelector("[data-tpl-seed]") as HTMLElement);
    await waitFor(() => {
      expect(factorTemplatesApi.seed).toHaveBeenCalled();
    });
  });
});
