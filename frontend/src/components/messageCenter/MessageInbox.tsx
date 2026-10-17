import { useCallback, useEffect, useState } from "react";
import { Button, Empty, Spin, Tag } from "antd";
import { api } from "../../api/client";
import { t, template } from "../../i18n";

type InboxItem = {
  id: number;
  type: "success" | "warning" | "error" | "info";
  title: string;
  description: string;
  time: string;
  created_at: string | null;
  read: boolean;
  source_type?: string | null;
  event_type?: string | null;
};

/**
 * 消息中心「收件箱」：站内消息（in_app 渠道）的列表与已读操作。
 *
 * 与顶部铃铛下拉读的是同一个 `/notifications/inbox`，不另造数据源；
 * 铃铛只给最近 50 条摘要，这里提供可操作的完整视图。
 */
export default function MessageInbox({ onOpenDeliveries }: { onOpenDeliveries: () => void }) {
  const [items, setItems] = useState<InboxItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const res = await api.getInboxNotifications({ limit: 200 });
      setItems(Array.isArray(res) ? res : []);
    } catch (err: any) {
      setItems([]);
      setLoadError(err?.message ?? String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const markRead = async (id: number) => {
    try {
      await api.markInboxItemRead(id);
      setItems((prev) => prev.map((item) => (item.id === id ? { ...item, read: true } : item)));
    } catch {
      // 后端已记日志；这里保留原态，下一次刷新自然纠正
    }
  };

  const markAllRead = async () => {
    try {
      await api.markInboxAllRead();
      setItems((prev) => prev.map((item) => ({ ...item, read: true })));
    } catch {
      // 同上
    }
  };

  const unread = items.filter((item) => !item.read).length;

  return (
    <div className="notification-inbox" data-tab-content="inbox">
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 12 }}>
        <span>{template("mcInboxSummary", { unread })}</span>
        <Button size="small" onClick={() => void markAllRead()} disabled={unread === 0}>
          {t("mcMarkAllRead")}
        </Button>
        <Button size="small" onClick={onOpenDeliveries}>
          {t("mcOpenDeliveries")}
        </Button>
      </div>
      {loadError && <div style={{ marginBottom: 12 }}>{t("mcInboxLoadFailed")}</div>}
      {loading ? (
        <Spin />
      ) : items.length === 0 ? (
        <Empty description={t("mcInboxEmpty")} />
      ) : (
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
          {items.map((item) => (
            <li
              key={item.id}
              style={{
                border: "1px solid var(--pt-border, #eee)",
                borderRadius: 8,
                padding: "10px 12px",
                display: "flex",
                gap: 8,
                alignItems: "flex-start",
                justifyContent: "space-between",
              }}
            >
              <div>
                <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                  <strong>{item.title}</strong>
                  {!item.read && <Tag color="blue">{t("mcUnreadTag")}</Tag>}
                  {item.event_type && <Tag>{item.event_type}</Tag>}
                </div>
                <div>{item.description}</div>
                <div style={{ opacity: 0.65, fontSize: 12 }}>{item.time}</div>
              </div>
              {!item.read && (
                <Button size="small" onClick={() => void markRead(item.id)}>
                  {t("mcMarkRead")}
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
