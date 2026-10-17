import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const inboxApi = vi.hoisted(() => ({
  getInboxNotifications: vi.fn(async () => [] as any[]),
  markInboxItemRead: vi.fn(async () => ({ ok: true, id: 1, read: true })),
  markInboxAllRead: vi.fn(async () => ({ ok: true, count: 2 })),
  viewAllInbox: vi.fn(async () => ({ ok: true, total: 0 })),
}));

vi.mock("../../../api/client", () => ({ api: inboxApi }));
vi.mock("../../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, params: Record<string, string | number>) =>
    `${key}:${JSON.stringify(params)}`,
}));

import MessageInbox from "../MessageInbox";

const items = () => [
  {
    id: 11, type: "error" as const, title: "热榜快照任务失败", description: "任务 9b94c3b4",
    time: "2026-10-04 10:09", created_at: null, read: false,
    source_type: "system", event_type: "task_failed",
  },
  {
    id: 12, type: "warning" as const, title: "数据过期: 000069", description: "尚无 K 线",
    time: "2026-10-04 10:09", created_at: null, read: true,
    source_type: "system", event_type: "data_stale",
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  inboxApi.getInboxNotifications.mockResolvedValue(items() as any);
});

afterEach(() => {
  cleanup();
});

describe("MessageInbox", () => {
  it("读的是 /notifications/inbox，与铃铛同源", async () => {
    render(<MessageInbox onOpenDeliveries={() => {}} />);
    await waitFor(() => expect(inboxApi.getInboxNotifications).toHaveBeenCalledOnce());
    expect(await screen.findByText("热榜快照任务失败")).toBeTruthy();
    expect(screen.getByText("数据过期: 000069")).toBeTruthy();
  });

  it("单条已读调 API 并立刻去掉未读标记", async () => {
    render(<MessageInbox onOpenDeliveries={() => {}} />);
    await screen.findByText("热榜快照任务失败");
    expect(screen.getAllByText("mcUnreadTag").length).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: "mcMarkRead" }));

    await waitFor(() => expect(inboxApi.markInboxItemRead).toHaveBeenCalledWith(11));
    await waitFor(() => expect(screen.queryAllByText("mcUnreadTag").length).toBe(0));
  });

  it("全部已读只调一次，并把未读数归零", async () => {
    render(<MessageInbox onOpenDeliveries={() => {}} />);
    await screen.findByText("热榜快照任务失败");
    expect(screen.getByText('mcInboxSummary:{"unread":1}')).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "mcMarkAllRead" }));

    await waitFor(() => expect(inboxApi.markInboxAllRead).toHaveBeenCalledOnce());
    await waitFor(() => expect(screen.getByText('mcInboxSummary:{"unread":0}')).toBeTruthy());
  });

  it("接口失败时不编造列表，显示失败提示", async () => {
    inboxApi.getInboxNotifications.mockRejectedValueOnce(new Error("network down"));
    render(<MessageInbox onOpenDeliveries={() => {}} />);
    await waitFor(() => expect(screen.getByText("mcInboxLoadFailed")).toBeTruthy());
    expect(screen.queryByText("热榜快照任务失败")).toBeNull();
  });

  it("「查看送达记录」把父级 Tab 切到送达配置", async () => {
    const onOpenDeliveries = vi.fn();
    render(<MessageInbox onOpenDeliveries={onOpenDeliveries} />);
    await screen.findByText("热榜快照任务失败");
    fireEvent.click(screen.getByRole("button", { name: "mcOpenDeliveries" }));
    expect(onOpenDeliveries).toHaveBeenCalledOnce();
  });
});
