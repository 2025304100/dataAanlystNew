import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
import { setLocale } from "../../../../i18n";

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
    setLocale("zh-CN");
  });
  afterEach(() => setLocale("zh-CN"));

  it("未激活不拉取（门控）", () => {
    render(<TemplateConfigPage active={false} />);
    expect(factorTemplatesApi.list).not.toHaveBeenCalled();
  });

  it("渲染列表：类型、范围、启停译名以及原始公式与优先级", async () => {
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

    expect(rows[0].textContent).toContain("系统预设");
    expect(rows[0].textContent).toContain("已启用");
    expect(rows[2].textContent).toContain("个人模板");
    expect(rows[2].textContent).toContain("已停用");
  });

  it("英文展示和搜索系统预设译名，经济逻辑本地化且个人内容保持原样", async () => {
    setLocale("en-US");
    vi.mocked(factorTemplatesApi.list).mockResolvedValueOnce({ items: [
      { template_id: "system-ma", name: "均线趋势", scope: "system", owner: "system", enabled: 1,
        created_at: "2026-09-20T15:55:35", rule_config: { category: "trend",
          formula: "mean(close,{n1})/mean(close,{n2})-1", params: { n1: [5, 10], n2: [20] },
          economy_logic_zh: "短期均线上穿长期均线，捕捉趋势启动" } },
      { template_id: "my-ma", name: "均线趋势", scope: "personal", owner: "local_user", enabled: 1,
        description: "我的自定义说明，不应被翻译", rule_config: { formula: "close" } },
    ], total: 2 });
    await renderReady();
    expect(document.querySelector("[data-tpl-table]")?.textContent).toContain("Moving average trend");
    fireEvent.change(document.querySelector("[data-tpl-search]") as HTMLElement, { target: { value: "moving average" } });
    await waitFor(() => expect(document.querySelectorAll("[data-tpl-table] tbody tr").length).toBe(1));
    const created = cellOf(document.querySelector("[data-tpl-table] tbody tr") as Element, "created");
    expect(created).toContain("2026");
    expect(created).not.toContain("T15:55:35");
    fireEvent.click(document.querySelector('[data-tpl-detail="system-ma"]') as HTMLElement);
    const modal = document.querySelector("[data-tpl-detail-modal]") as HTMLElement;
    expect(modal.textContent).toContain("System preset");
    expect(modal.textContent).not.toContain("system");
    expect(modal.textContent).toContain(created);
    expect(modal.querySelector("[data-tpl-detail-economy]")?.textContent).toBe("A short moving average crossing above a long moving average signals the start of a trend.");
    expect(modal.querySelector("[data-tpl-detail-formula]")?.textContent).toBe("mean(close,{n1})/mean(close,{n2})-1");
    expect(modal.querySelector("[data-tpl-detail-params]")?.textContent).toBe("n1: 5~10 · n2: 20");
    fireEvent.click(modal.querySelector("[data-tpl-detail-close]") as HTMLElement);
    fireEvent.change(document.querySelector("[data-tpl-search]") as HTMLElement, { target: { value: "" } });
    fireEvent.click(document.querySelector('[data-tpl-detail="my-ma"]') as HTMLElement);
    expect(document.querySelector("[data-tpl-detail-modal]")?.textContent).toContain("均线趋势");
    expect(document.querySelector("[data-tpl-detail-economy]")?.textContent).toBe("我的自定义说明，不应被翻译");
    expect(document.querySelector("[data-tpl-detail-modal]")?.textContent).toContain("local_user");
  });

  it("中文预设保持中文，未知范围和未知系统模板保留原值", async () => {
    vi.mocked(factorTemplatesApi.list).mockResolvedValueOnce({ items: [
      { template_id: "system-ma", name: "均线趋势", scope: "system", enabled: 1,
        rule_config: { economy_logic_zh: "短期均线上穿长期均线，捕捉趋势启动" } },
      { template_id: "unknown", name: "自定义系统模板", scope: "team", enabled: 1,
        description: "自定义说明" },
    ], total: 2 });
    await renderReady();
    expect(document.querySelector("[data-tpl-table]")?.textContent).toContain("均线趋势");
    expect(document.querySelector("[data-tpl-table]")?.textContent).toContain("team");
    fireEvent.click(document.querySelector('[data-tpl-detail="unknown"]') as HTMLElement);
    expect(document.querySelector("[data-tpl-detail-modal]")?.textContent).toContain("自定义系统模板");
    expect(document.querySelector("[data-tpl-detail-modal]")?.textContent).toContain("team");
    expect(document.querySelector("[data-tpl-detail-modal]")?.textContent).not.toContain("个人模板");
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
    // antd Select：分类是工具条第一个下拉，mousedown 打开弹层后点击「波动」选项
    fireEvent.mouseDown(document.querySelector(".ant-select-selector") as HTMLElement);
    await waitFor(() => {
      expect(document.querySelectorAll(".ant-select-item-option").length).toBeGreaterThan(0);
    });
    const option = Array.from(document.querySelectorAll(".ant-select-item-option"))
      .find((o) => o.textContent === "波动") as HTMLElement;
    fireEvent.click(option);
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

  it("空模板库也能初始化，启停失败保留列表并说明原因", async () => {
    vi.mocked(factorTemplatesApi.list).mockResolvedValueOnce({ items: [], total: 0 });
    const page = render(<TemplateConfigPage active />);
    await waitFor(() => expect(document.querySelector("[data-tpl-empty]")).toBeTruthy());
    fireEvent.click(page.getByRole("button", { name: /系统模板/ }));
    await waitFor(() => expect(factorTemplatesApi.seed).toHaveBeenCalled());
    await waitFor(() => expect(document.querySelector("[data-tpl-table]")).toBeTruthy());
    vi.mocked(factorTemplatesApi.setEnabled).mockRejectedValueOnce(new Error("任务占用，稍后重试"));
    fireEvent.click(document.querySelector("[data-tpl-toggle='tp-1']") as HTMLElement);
    await waitFor(() => expect(page.getByRole("alert").textContent).toContain("任务占用，稍后重试"));
    expect(document.querySelector("[data-tpl-table]")?.textContent).toContain("动量因子");
  });
});
