import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const { mockRequestJson } = vi.hoisted(() => ({
  mockRequestJson: vi.fn(),
}));

vi.mock("../../../api/client", () => ({
  requestJson: mockRequestJson,
  api: {},
}));

vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return {
    ...actual,
    message: {
      ...actual.message,
      success: vi.fn(),
      error: vi.fn(),
    },
  };
});

import { setLocale } from "../../../i18n";
import {
  SYSTEM_VARIABLE_GROUPS,
  extractVariableNames,
  parseVariables,
  renderTemplate,
  TemplateEditor,
} from "../TemplateEditor";

const savedTemplate = {
  id: 1,
  name: "系统告警",
  title_template: "【系统告警】{title}",
  body_template: "标的：{symbol}",
  body_text_template: "标的：{symbol}",
  variables_json:
    '[{"name":"title","description":"原始标题","example":"评分下降预警"},{"name":"symbol","description":"标的","example":"000001.SZ"}]',
  version: 1,
  is_active: true,
};

describe("TemplateEditor 模板编辑体验", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockRequestJson.mockReset();
    setLocale("zh-CN");
  });

  afterEach(() => {
    cleanup();
    document.body.innerHTML = "";
  });

  it("兼容单双大括号变量并清洗历史变量定义", () => {
    expect(
      renderTemplate("{symbol} / {{ symbol }}", { symbol: "000001.SZ" }),
    ).toBe("000001.SZ / 000001.SZ");
    expect(extractVariableNames("{symbol} {{ price }} {symbol}")).toEqual([
      "symbol",
      "price",
    ]);
    expect(renderTemplate("{1foo} / {中文}", { "1foo": "A", 中文: "B" })).toBe(
      "A / B",
    );
    expect(extractVariableNames("{1foo} / {{ 中文 }}")).toEqual([
      "1foo",
      "中文",
    ]);
    expect(
      renderTemplate("{value}", { value: "**bold** [x] <tag>&" }),
    ).toBe("\\*\\*bold\\*\\* \\[x\\] \\<tag\\>\\&");
    expect(
      parseVariables(
        '[{"name":" symbol ","description":"标的代码","example":"000001.SZ"}]',
      ),
    ).toEqual([
      {
        name: "symbol",
        description: "标的代码",
        example: "000001.SZ",
      },
    ]);
    expect(SYSTEM_VARIABLE_GROUPS.map((group) => group.key)).toEqual([
      "alert_event",
      "trade_executed",
      "discovery_new",
      "auto_trade_blocked",
      "drawdown_warning",
      "max_loss_warning",
    ]);
  });

  it("应用场景示例前会确认覆盖已有标题和正文", async () => {
    const user = userEvent.setup();
    mockRequestJson.mockResolvedValue([]);
    render(<TemplateEditor />);

    await user.click(await screen.findByRole("button", { name: /添加模板/ }));
    await user.type(screen.getByTestId("template-title-input"), "用户标题");
    await user.type(screen.getByTestId("template-body-input"), "用户正文");
    await user.click(screen.getByTestId("apply-template-example"));

    expect(screen.getByTestId("template-title-input")).toHaveValue("用户标题");
    expect(screen.getByText("覆盖当前模板内容？")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "确认覆盖" }));
    expect(screen.getByTestId("template-title-input")).toHaveValue(
      "【系统告警】{title}",
    );
  });

  it("英文环境的系统变量示例值不包含中文", async () => {
    const user = userEvent.setup();
    setLocale("en-US");
    mockRequestJson.mockResolvedValue([]);
    render(<TemplateEditor />);

    await user.click(await screen.findByRole("button", { name: /Add Template/i }));
    await user.click(screen.getByTestId("apply-template-example"));

    expect(screen.getByText("{title} = Score drop warning")).toBeInTheDocument();
    expect(screen.queryByText(/评分下降预警/)).not.toBeInTheDocument();
  });

  it("点击系统变量会插入标题并自动生成结构化变量定义", async () => {
    const user = userEvent.setup();
    mockRequestJson.mockResolvedValue([]);
    render(<TemplateEditor />);

    await user.click(await screen.findByRole("button", { name: /添加模板/ }));
    await user.click(screen.getByTestId("system-variable-symbol"));

    expect(screen.getByTestId("template-title-input")).toHaveValue("{symbol}");
    expect(screen.getByTestId("template-variable-name")).toHaveValue("symbol");
    expect(screen.getByTestId("template-variable-example")).toHaveValue(
      "000001.SZ",
    );
    expect(screen.getByText("实时预览")).toBeInTheDocument();
    expect(screen.getByText("{symbol} = 000001.SZ")).toBeInTheDocument();
    expect(
      screen.queryByText("正文引用了尚未定义的变量"),
    ).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/JSON/i)).not.toBeInTheDocument();
  });

  it("场景示例可直接生成标题正文并按原接口序列化变量", async () => {
    const user = userEvent.setup();
    mockRequestJson.mockImplementation(
      async (_url: string, options?: RequestInit) =>
        options?.method === "POST" ? savedTemplate : [],
    );
    render(<TemplateEditor />);

    await user.click(await screen.findByRole("button", { name: /添加模板/ }));
    await user.type(screen.getByPlaceholderText("请输入模板名称"), "系统告警");
    await user.click(screen.getByRole("button", { name: /使用场景示例/ }));

    expect(screen.getByTestId("template-title-input")).toHaveValue(
      "【系统告警】{title}",
    );
    expect(screen.getByTestId("template-body-input")).toHaveValue(
      "告警类型：{alert_type}\n标的：{symbol}\n详情：{title}",
    );
    expect(screen.getByText("【系统告警】评分下降预警")).toBeInTheDocument();
    expect(
      screen.queryByText("正文引用了尚未定义的变量"),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /创\s*建/ }));

    await waitFor(() => {
      expect(
        mockRequestJson.mock.calls.some(
          ([url, options]) =>
            url === "/api/v1/notifications/templates" &&
            options?.method === "POST",
        ),
      ).toBe(true);
    });

    const createCall = mockRequestJson.mock.calls.find(
      ([url, options]) =>
        url === "/api/v1/notifications/templates" && options?.method === "POST",
    );
    const payload = JSON.parse(String(createCall?.[1]?.body));
    const variables = JSON.parse(payload.variables_json);

    expect(payload.title_template).toBe("【系统告警】{title}");
    expect(variables.map((variable: { name: string }) => variable.name)).toEqual([
      "alert_type",
      "symbol",
      "title",
    ]);
    expect(variables[1].example).toBe("000001.SZ");
  });

  it("编辑历史模板时回填变量含义和示例值", async () => {
    const user = userEvent.setup();
    mockRequestJson.mockResolvedValue([savedTemplate]);
    render(<TemplateEditor />);

    await user.click(await screen.findByRole("button", { name: /编辑/ }));

    expect(screen.getByTestId("template-title-input")).toHaveValue(
      savedTemplate.title_template,
    );
    expect(screen.getAllByTestId("template-variable-name")[0]).toHaveValue(
      "title",
    );
    expect(screen.getAllByTestId("template-variable-example")[1]).toHaveValue(
      "000001.SZ",
    );
  });
});
