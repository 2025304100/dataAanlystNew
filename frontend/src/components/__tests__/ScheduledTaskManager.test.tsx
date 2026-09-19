import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const { mockApi, showToast } = vi.hoisted(() => ({
  showToast: vi.fn(),
  mockApi: {
    getScheduledTaskDefinitions: vi.fn(async () => [
      {
        task_type: "factor_pipeline",
        name: "因子流水线",
        description: "",
        default_payload: { train_model: false },
      },
    ]),
    getScheduledTasks: vi.fn(async () => [
      {
        id: 1,
        name: "每日因子评分",
        task_type: "factor_pipeline",
        frequency: "daily",
        time_of_day: "18:10",
        weekdays: [],
        interval_minutes: null,
        timezone: "Asia/Shanghai",
        payload: { train_model: false },
        enabled: true,
        next_run_at: "2026-07-15T10:10:00",
        last_run_at: null,
        last_status: null,
        last_task_id: null,
        last_task_status: null,
        last_message: null,
        last_error: null,
        created_at: "2026-07-14T10:00:00",
        updated_at: "2026-07-14T10:00:00",
      },
    ]),
    getScheduledTaskRuns: vi.fn(async () => []),
    createScheduledTask: vi.fn(),
    updateScheduledTask: vi.fn(async (_id: number, payload: unknown) => payload),
    deleteScheduledTask: vi.fn(),
    runScheduledTask: vi.fn(async () => ({
      id: 9,
      schedule_id: 1,
      schedule_name: "每日因子评分",
      task_type: "factor_pipeline",
      trigger_source: "manual",
      task_source: "async",
      task_id: "task-9",
      status: "queued",
      message: "queued",
      created_at: "2026-07-14T10:00:00",
      finished_at: null,
    })),
  },
}));

vi.mock("../../api/client", () => ({ api: mockApi }));
vi.mock("../../context/AppContext", () => ({
  useApp: () => ({ locale: "zh-CN", showToast }),
}));

import ScheduledTaskManager from "../ScheduledTaskManager";

describe("ScheduledTaskManager", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("lists schedules and supports manual run and enable toggle", async () => {
    render(<ScheduledTaskManager />);

    await waitFor(() => {
      expect(screen.getByText("每日因子评分")).toBeInTheDocument();
    });
    expect(screen.getByText("每天 18:10")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /立即执行/ }));
    await waitFor(() => {
      expect(mockApi.runScheduledTask).toHaveBeenCalledWith(1);
    });

    fireEvent.click(screen.getByRole("switch"));
    await waitFor(() => {
      expect(mockApi.updateScheduledTask).toHaveBeenCalledWith(1, { enabled: false });
    });
  });
});
