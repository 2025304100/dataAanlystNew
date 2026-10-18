// 复盘抽屉（承接原孤儿面板的 reviews 能力，体检报告 §二十七）。
// 关键锁定点：
// 1. 提交 payload 必须是后端 ReviewCreate 认的字段（start_date/end_date 必填、
//    不再有 attribution_snapshot）—— 这正是 PT-DEF-26 的根因；
// 2. 加载/保存失败必须显式可见，不能静默空列表（静默空会被当成"没有复盘"）；
// 3. 反向时间范围在前端就拦下，不发起无意义请求。
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    getReviews: vi.fn(),
    createReview: vi.fn(),
  },
}));

vi.mock("../../../api/client", () => ({ api: mockApi }));
vi.mock("../../../i18n", () => ({ t: (key: string) => key }));

// 只替换 antd 的 DatePicker：真实 rc-picker 的受控 input 无法用原生事件可靠驱动——
// 直接改 DOM value 后，负载下 rc-picker 的 onChange 会回传 null/非法值，range 变非法，
// handleSubmit 在区间校验处提前 return（createReview 记为 0 次，界面显示 invalidRange）。
// 换成受控 <input> 并在 change 时回传 dayjs，即可确定性地设置区间。
// 本用例锁定的是抽屉的 payload / 区间校验契约，与 rc-picker 内部实现无关。
vi.mock("antd", async (importOriginal) => {
  const actual = await importOriginal<typeof import("antd")>();
  const dayjsLib = (await import("dayjs")).default;
  const DatePickerMock = (props: any) => {
    const { value, onChange, id, "data-testid": testId } = props;
    return (
      <input
        id={id}
        data-testid={testId}
        value={value ? value.format("YYYY-MM-DD") : ""}
        onChange={(e) => onChange?.(e.target.value ? dayjsLib(e.target.value) : null)}
      />
    );
  };
  return { ...actual, DatePicker: DatePickerMock };
});

import PortfolioReviewDrawer from "../PortfolioReviewDrawer";

const openDrawer = () =>
  render(
    <PortfolioReviewDrawer open onClose={vi.fn()} portfolioId={3} />,
  );

/** 驱动被 mock 的 DatePicker（受控 input）：change 即触发组件的 onChange。 */
const setPickerDate = (testid: string, value: string) => {
  fireEvent.change(screen.getByTestId(testid), { target: { value } });
};

beforeEach(() => {
  vi.clearAllMocks();
  mockApi.getReviews.mockResolvedValue([]);
  mockApi.createReview.mockResolvedValue({ id: 91 });
});

describe("PortfolioReviewDrawer", () => {
  it("打开即按组合拉取复盘记录并渲染", async () => {
    mockApi.getReviews.mockResolvedValueOnce([
      {
        id: 1, portfolio_id: 3, start_date: "2026-06-01", end_date: "2026-06-30",
        note: "六月回撤主要来自动量", created_at: "2026-07-01T02:00:00Z",
      },
    ]);
    openDrawer();
    await waitFor(() => expect(mockApi.getReviews).toHaveBeenCalledWith(3));
    const item = await screen.findByTestId("review-item");
    expect(item.textContent).toContain("六月回撤主要来自动量");
    expect(item.textContent).toContain("2026-06-01");
  });

  it("备注为空时不允许提交", async () => {
    openDrawer();
    await screen.findByTestId("review-drawer");
    expect(screen.getByTestId("review-submit-button")).toBeDisabled();
    expect(mockApi.createReview).not.toHaveBeenCalled();
  });

  it("提交使用后端认的字段名与必填时间范围", async () => {
    openDrawer();
    await screen.findByTestId("review-drawer");

    const user = userEvent.setup();
    setPickerDate("review-start-date", "2026-05-01");
    setPickerDate("review-end-date", "2026-05-31");
    // 备注与提交走 userEvent：每步 await 会 flush React 状态更新，避免并行负载下
    // 提交按钮仍读到未生效的备注（disabled 时点击不会触发提交，createReview 记 0 次）。
    await user.type(screen.getByTestId("review-note-input"), "五月复盘");
    // 并行负载（CPU 争抢）下备注状态可能尚未回灌到渲染，按钮仍是 disabled；
    // 显式等待按钮可用再点击，消除这一时序依赖。
    const submit = screen.getByTestId("review-submit-button") as HTMLButtonElement;
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);

    await waitFor(() => expect(mockApi.createReview).toHaveBeenCalledTimes(1));
    const [portfolioId, payload] = mockApi.createReview.mock.calls[0] as [number, Record<string, unknown>];
    expect(portfolioId).toBe(3);
    expect(payload).toEqual({ start_date: "2026-05-01", end_date: "2026-05-31", note: "五月复盘" });
    // 旧孤儿面板的错字段，不能再出现
    expect(payload.attribution_snapshot).toBeUndefined();
    // 保存成功后重新拉列表
    await waitFor(() => expect(mockApi.getReviews).toHaveBeenCalledTimes(2));
    expect(await screen.findByTestId("review-created")).toBeInTheDocument();
  });

  it("反向时间范围在前端拦下，不发请求", async () => {
    openDrawer();
    await screen.findByTestId("review-drawer");
    const user = userEvent.setup();
    setPickerDate("review-start-date", "2026-08-31");
    setPickerDate("review-end-date", "2026-08-01");
    await user.type(screen.getByTestId("review-note-input"), "任意备注");
    await user.click(screen.getByTestId("review-submit-button"));

    expect(await screen.findByTestId("review-error")).toBeInTheDocument();
    expect(mockApi.createReview).not.toHaveBeenCalled();
  });

  it("列表接口失败时显式报错，不伪装成空列表", async () => {
    mockApi.getReviews.mockRejectedValueOnce(new Error("gateway 502"));
    openDrawer();
    const box = await screen.findByTestId("review-error");
    expect(box.textContent).toContain("gateway 502");
    expect(screen.queryByTestId("review-empty")).not.toBeInTheDocument();
  });

  it("没有复盘时给出明确空态", async () => {
    openDrawer();
    await waitFor(() => expect(screen.getByTestId("review-empty")).toBeInTheDocument());
  });
});
