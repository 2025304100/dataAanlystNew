import { describe, expect, it } from "vitest";
import { groupSessions } from "../SessionChipBar";
import type { AISession } from "../../../types";

const mk = (id: number, createdAt: string | null): AISession => ({
  id,
  title: `会话 ${id}`,
  source_page: null,
  provider: null,
  model: null,
  profile_id: null,
  status: "active",
  context_summary: null,
  total_tokens: 0,
  total_cost: null,
  created_at: createdAt,
  updated_at: null,
  deleted_at: null,
});

describe("groupSessions", () => {
  it("groups by today / past 7 days / earlier", () => {
    const now = new Date();
    const today = now.toISOString();
    const threeDaysAgo = new Date(now.getTime() - 3 * 24 * 3600 * 1000).toISOString();
    const longAgo = new Date(now.getTime() - 30 * 24 * 3600 * 1000).toISOString();

    const groups = groupSessions([
      mk(1, today),
      mk(2, threeDaysAgo),
      mk(3, longAgo),
      mk(4, null),
    ]);

    expect(groups.map((g) => g.key)).toEqual(["today", "week", "earlier"]);
    expect(groups[0].items.map((s) => s.id)).toEqual([1]);
    expect(groups[1].items.map((s) => s.id)).toEqual([2]);
    // created_at 为空的会话归入「更早」，不会凭空消失
    expect(groups[2].items.map((s) => s.id)).toEqual([3, 4]);
  });

  it("drops empty groups", () => {
    const groups = groupSessions([mk(1, new Date().toISOString())]);
    expect(groups).toHaveLength(1);
    expect(groups[0].key).toBe("today");
  });
});
