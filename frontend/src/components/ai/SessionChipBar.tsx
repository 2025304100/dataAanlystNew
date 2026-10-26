import { useMemo, useState } from "react";
import { Button, Input, Popconfirm, Space, Spin, Tag, Tooltip } from "antd";
import { CloseOutlined, DeleteOutlined, HistoryOutlined, SearchOutlined } from "@ant-design/icons";
import { theme } from "antd";
import { t, template } from "../../i18n";
import type { AISession } from "../../types";

interface SessionChipBarProps {
  sessions: AISession[];
  currentSessionId: number | null;
  loading: boolean;
  onSelect: (sessionId: number) => void;
  onDelete: (sessionId: number) => void;
}

const DAY_MS = 24 * 60 * 60 * 1000;

/** 按 今天 / 近 7 天 / 更早 分组（空组剔除）。导出供单测。 */
export function groupSessions(
  sessions: AISession[],
): Array<{ key: "today" | "week" | "earlier"; items: AISession[] }> {
  const now = new Date();
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const weekStart = todayStart - 6 * DAY_MS;
  const today: AISession[] = [];
  const week: AISession[] = [];
  const earlier: AISession[] = [];
  for (const s of sessions) {
    const ts = s.created_at ? new Date(s.created_at).getTime() : 0;
    if (ts >= todayStart) today.push(s);
    else if (ts >= weekStart) week.push(s);
    else earlier.push(s);
  }
  const groups: Array<{ key: "today" | "week" | "earlier"; items: AISession[] }> = [
    { key: "today", items: today },
    { key: "week", items: week },
    { key: "earlier", items: earlier },
  ];
  return groups.filter((g) => g.items.length > 0);
}

function groupLabel(key: "today" | "week" | "earlier"): string {
  if (key === "today") return t("aiAssistant.groupToday");
  if (key === "week") return t("aiAssistant.groupThisWeek");
  return t("aiAssistant.groupEarlier");
}

function sessionTime(s: AISession): string {
  return s.created_at ? new Date(s.created_at).toLocaleString() : "";
}

/**
 * 会话入口条：收起态 = 横向 chip（当前会话 + 最近 2 个 + 历史按钮）；
 * 展开态 = 300px 面板（搜索 + 分组列表 + 删除）。
 * 修复点：替代原 120px 滚动窗（14 个会话只露 2 条、第 2 条日期行被裁），
 * 并补搜索与时间分组（评估报告 P0-2 / P2-13 治标）。
 */
export default function SessionChipBar({
  sessions,
  currentSessionId,
  loading,
  onSelect,
  onDelete,
}: SessionChipBarProps) {
  const { token } = theme.useToken();
  const [expanded, setExpanded] = useState(false);
  const [query, setQuery] = useState("");

  const current = sessions.find((s) => s.id === currentSessionId) ?? null;
  const recent = sessions.filter((s) => s.id !== currentSessionId).slice(0, 2);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter(
      (s) =>
        s.title.toLowerCase().includes(q) ||
        (s.source_page ?? "").toLowerCase().includes(q),
    );
  }, [sessions, query]);

  const groups = useMemo(() => groupSessions(filtered), [filtered]);

  const chipStyle = (active: boolean) => ({
    cursor: "pointer" as const,
    maxWidth: 168,
    overflow: "hidden" as const,
    textOverflow: "ellipsis" as const,
    whiteSpace: "nowrap" as const,
    marginInlineEnd: 0,
    background: active ? token.colorPrimaryBg : token.colorFillQuaternary,
    borderColor: active ? token.colorPrimary : token.colorBorderSecondary,
    color: token.colorText,
  });

  return (
    <div style={{ borderBottom: `1px solid ${token.colorBorderSecondary}`, flexShrink: 0 }}>
      {/* 收起态：chip 条 */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, padding: "6px 12px" }}>
        {loading ? (
          <Spin size="small" />
        ) : sessions.length === 0 ? (
          <span style={{ fontSize: 12, color: token.colorTextTertiary }}>
            {t("aiAssistant.noSessions")}
          </span>
        ) : (
          <div style={{ display: "flex", gap: 6, overflowX: "auto", minWidth: 0, flex: 1 }}>
            {current && (
              <Tooltip title={current.title}>
                <Tag style={chipStyle(true)} onClick={() => onSelect(current.id)} data-testid="ai-session-chip-current">
                  {current.title}
                </Tag>
              </Tooltip>
            )}
            {recent.map((s) => (
              <Tooltip key={s.id} title={s.title}>
                <Tag style={chipStyle(false)} onClick={() => onSelect(s.id)} data-testid="ai-session-chip">
                  {s.title}
                </Tag>
              </Tooltip>
            ))}
          </div>
        )}
        {sessions.length > 0 && (
          <Button
            type="link"
            size="small"
            icon={<HistoryOutlined />}
            onClick={() => setExpanded((v) => !v)}
            data-testid="ai-session-history-toggle"
            style={{ flexShrink: 0 }}
          >
            {expanded ? t("aiAssistant.collapse") : template("aiAssistant.historyWithCount", { count: sessions.length })}
          </Button>
        )}
      </div>

      {/* 展开态：搜索 + 分组列表 */}
      {expanded && (
        <div
          data-testid="ai-session-panel"
          style={{
            maxHeight: 300,
            overflowY: "auto",
            padding: "0 12px 8px",
            background: token.colorBgContainer,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
            <Input
              size="small"
              allowClear
              prefix={<SearchOutlined style={{ color: token.colorTextTertiary }} />}
              placeholder={t("aiAssistant.sessionSearchPlaceholder")}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              data-testid="ai-session-search"
            />
            <Button
              type="text"
              size="small"
              icon={<CloseOutlined />}
              onClick={() => setExpanded(false)}
              aria-label={t("aiAssistant.collapse")}
            />
          </div>
          {groups.length === 0 ? (
            <div style={{ fontSize: 12, color: token.colorTextTertiary, padding: "8px 0" }}>
              {t("aiAssistant.noSessionMatch")}
            </div>
          ) : (
            groups.map((g) => (
              <div key={g.key}>
                <div style={{ fontSize: 11, color: token.colorTextTertiary, padding: "4px 0 2px" }}>
                  {groupLabel(g.key)}
                </div>
                {g.items.map((s) => (
                  <div
                    key={s.id}
                    onClick={() => {
                      onSelect(s.id);
                      setExpanded(false);
                    }}
                    data-testid="ai-session-item"
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 6,
                      padding: "5px 8px",
                      borderRadius: token.borderRadius,
                      cursor: "pointer",
                      background: s.id === currentSessionId ? token.colorPrimaryBg : "transparent",
                    }}
                  >
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div
                        style={{
                          fontSize: 13,
                          color: token.colorText,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {s.title}
                      </div>
                      <Space size={4} style={{ fontSize: 11 }}>
                        {s.source_page && (
                          <Tag style={{ fontSize: 10, marginInlineEnd: 0 }}>{s.source_page}</Tag>
                        )}
                        <span style={{ color: token.colorTextTertiary }}>{sessionTime(s)}</span>
                      </Space>
                    </div>
                    <Popconfirm
                      title={t("aiAssistant.deleteSessionConfirm")}
                      onConfirm={(e) => {
                        e?.stopPropagation();
                        onDelete(s.id);
                      }}
                    >
                      <Button
                        type="text"
                        size="small"
                        danger
                        icon={<DeleteOutlined />}
                        onClick={(e) => e.stopPropagation()}
                        aria-label={t("aiAssistant.deleteSession")}
                      />
                    </Popconfirm>
                  </div>
                ))}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
