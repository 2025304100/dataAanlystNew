import React, { useCallback, useEffect, useRef, useState } from "react";
import { Bell, CircleCheck, TriangleAlert, CircleX, Info } from "lucide-react";
import { message } from "antd";
import { t } from "../../i18n";
import { api } from "../../api/client";

/**
 * NotificationDropdown — 顶部栏右侧消息提醒下拉
 *
 * Uses the unified notification Outbox configured in Settings:
 *   - GET `/api/v1/notifications/inbox` for real in-app messages
 *   - PUT `/notifications/inbox/{id}/read` for persisted read receipts
 *   - PUT `/notifications/inbox/read-all` for persisted bulk receipts
 *   - "View all" opens Settings > notification delivery history
 * API failures are shown explicitly; no mock or seed messages are rendered.
 *
 * 互斥：通过 onOpenChange / forceClose 与父组件协作实现与 HelpDropdown 互斥，
 * 实际互斥逻辑由父组件（App.tsx）管理。
 *
 * i18n：使用 t('portfolioTrading.notification.xxx')，缺失时 t() 返回 key 字符串，不阻塞编译。
 */

type NotificationType = "success" | "warning" | "error" | "info";

interface NotificationItem {
  id: number;
  type: NotificationType;
  title: string;
  description: string;
  time: string;
  read: boolean;
  created_at?: string | null;
}

interface NotificationDropdownProps {
  /** 下拉打开/关闭时回调父组件，用于实现与 HelpDropdown 互斥 */
  onOpenChange?: (open: boolean) => void;
  /** 父组件强制关闭（互斥用：当 HelpDropdown 打开时置 true） */
  forceClose?: boolean;
  /** Open the unified notification delivery log in Settings. */
  onViewAll?: () => void;
}

/** 通知类型 → 图标 + 颜色映射（对齐原型图 notif-icon 内联色） */
const NOTIF_ICON_MAP: Record<
  NotificationType,
  { Icon: React.ElementType; color: string; dim: string }
> = {
  success: {
    Icon: CircleCheck,
    color: "var(--pt-state-success)",
    dim: "var(--pt-state-success-dim)",
  },
  warning: {
    Icon: TriangleAlert,
    color: "var(--pt-state-warning)",
    dim: "var(--pt-state-warning-dim)",
  },
  error: {
    Icon: CircleX,
    color: "var(--pt-state-error)",
    dim: "var(--pt-state-error-dim)",
  },
  info: {
    Icon: Info,
    color: "var(--pt-state-info)",
    dim: "var(--pt-state-info-dim)",
  },
};

const NotificationDropdown: React.FC<NotificationDropdownProps> = ({
  onOpenChange,
  forceClose,
  onViewAll,
}) => {
  const [open, setOpen] = useState(false);
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);

  const hasUnread = notifications.some((n) => !n.read);

  /** 统一更新 open 状态并通知父组件（forceClose 触发的关闭不回调，父组件已知） */
  const updateOpen = useCallback(
    (next: boolean) => {
      setOpen(next);
      onOpenChange?.(next);
    },
    [onOpenChange],
  );

  /** P2-FIX: 打开下拉时从 API 拉取最新通知列表（失败就用本地 fallback） */
  const fetchNotifications = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const res = await api.getInboxNotifications({ limit: 50 });
      const normalized = (Array.isArray(res) ? res : [])
          .filter((r) => r && typeof r.id !== "undefined")
          .map((r) => ({
            id: Number(r.id),
            type: (r.type as NotificationType) ?? "info",
            title: String(r.title ?? "(无标题)"),
            description: String(r.description ?? ""),
            time: String(r.time ?? r.created_at ?? ""),
            read: !!r.read,
            created_at: r.created_at ?? null,
          }));
      setNotifications(normalized);
    } catch (err: any) {
      const msg = err?.message || String(err);
      // eslint-disable-next-line no-console
      console.warn("[notifications] inbox fetch failed:", msg);
      setNotifications([]);
      setLoadError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  // Keep the bell badge in sync even before the dropdown is opened.
  useEffect(() => {
    fetchNotifications().catch(() => {});
    const timer = window.setInterval(() => {
      fetchNotifications().catch(() => {});
    }, 30_000);
    return () => window.clearInterval(timer);
  }, [fetchNotifications]);

  /** 切换下拉展开/收起；打开时拉取 API 数据 */
  const handleToggle = useCallback(() => {
    const next = !open;
    updateOpen(next);
    if (next) {
      fetchNotifications().catch(() => {});
    }
  }, [open, updateOpen, fetchNotifications]);

  /** forceClose 由父组件控制：true 时关闭本下拉 */
  useEffect(() => {
    if (forceClose) {
      setOpen(false);
    }
  }, [forceClose]);

  /** 点击外部关闭（仅 open 时绑定监听） */
  useEffect(() => {
    if (!open) return;
    function handleMouseDown(e: MouseEvent) {
      if (
        wrapperRef.current &&
        !wrapperRef.current.contains(e.target as Node)
      ) {
        updateOpen(false);
      }
    }
    document.addEventListener("mousedown", handleMouseDown);
    return () => document.removeEventListener("mousedown", handleMouseDown);
  }, [open, updateOpen]);

  /** P2-FIX: 点击单条通知 → 调 API 标记已读 + 关闭下拉 + toast */
  const handleClickItem = useCallback(
    async (item: NotificationItem) => {
      // 先乐观更新 UI（避免慢网络导致看不到已读效果）
      setNotifications((prev) =>
        prev.map((n) => (n.id === item.id ? { ...n, read: true } : n)),
      );
      updateOpen(false);
      try {
        await api.markInboxItemRead(item.id);
        message.success(
          t("portfolioTrading.notification.viewed").replace("{title}", item.title),
        );
      } catch (err: any) {
        // eslint-disable-next-line no-console
        console.warn("[notifications] mark read failed:", err?.message ?? err);
        setNotifications((prev) =>
          prev.map((n) => (n.id === item.id ? { ...n, read: item.read } : n)),
        );
        message.error(err?.message ?? "标记通知已读失败");
      }
    },
    [updateOpen],
  );

  /** P2-FIX: 全部已读 → 调 API；成功后根据 count 提示，失败保留本地已读态 */
  const handleMarkAllRead = useCallback(async () => {
    const unreadIds = new Set(notifications.filter((n) => !n.read).map((n) => n.id));
    setNotifications((prev) => prev.map((n) => ({ ...n, read: true })));
    try {
      const res = await api.markInboxAllRead();
      const count = res?.count ?? 0;
      const base = t("portfolioTrading.notification.markAllReadToast");
      message.success(count > 0 ? `${base}（共 ${count} 条）` : base);
    } catch (err: any) {
      // eslint-disable-next-line no-console
      console.warn("[notifications] mark all read failed:", err?.message ?? err);
      setNotifications((prev) =>
        prev.map((n) => (unreadIds.has(n.id) ? { ...n, read: false } : n)),
      );
      message.error(err?.message ?? "全部标记已读失败");
    }
  }, [notifications]);

  /** P2-FIX: 查看全部通知 → 调 API 拿总数；根据返回拼接提示（未来可跳通知中心页） */
  const handleViewAll = useCallback(async () => {
    updateOpen(false);
    try {
      await api.viewAllInbox();
    } catch (err: any) {
      // eslint-disable-next-line no-console
      console.warn("[notifications] view all failed:", err?.message ?? err);
    }
    onViewAll?.();
  }, [onViewAll, updateOpen]);

  return (
    <div ref={wrapperRef} style={{ position: "relative" }}>
      {/* ========== 通知按钮（bell） ========== */}
      <button
        type="button"
        title={t("portfolioTrading.notification.title")}
        onClick={handleToggle}
        style={{
          width: 32,
          height: 32,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          borderRadius: 6,
          background: "transparent",
          border: "none",
          color: "var(--pt-muted-foreground)",
          cursor: "pointer",
          position: "relative",
          transition: "background 0.15s ease, color 0.15s ease",
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.background = "var(--pt-surface-3)";
          e.currentTarget.style.color = "var(--pt-foreground)";
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.background = "transparent";
          e.currentTarget.style.color = "var(--pt-muted-foreground)";
        }}
      >
        <Bell size={20} />
        {/* 未读红点 */}
        {hasUnread && (
          <span
            style={{
              position: "absolute",
              top: -2,
              right: -2,
              width: 6,
              height: 6,
              borderRadius: "50%",
              background: "var(--pt-state-error)",
              border: "2px solid var(--pt-surface-2)",
            }}
          />
        )}
      </button>

      {/* ========== 通知下拉面板 ========== */}
      {open && (
        <div
          className="pt-dropdown"
          style={{
            position: "absolute",
            top: "calc(100% + 8px)",
            right: 0,
            zIndex: 100,
            minWidth: 340,
            padding: 0,
            background: "var(--pt-popover)",
            border: "1px solid var(--pt-border)",
            borderRadius: "var(--pt-radius-lg)",
            boxShadow: "var(--pt-shadow-lg)",
          }}
        >
          {/* 标题栏 */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "12px 16px",
              borderBottom: "1px solid var(--pt-border)",
            }}
          >
            <span
              style={{
                fontSize: 14,
                fontWeight: 600,
                color: "var(--pt-foreground)",
              }}
            >
              {t("portfolioTrading.notification.title")}
            </span>
            <button
              type="button"
              className="pt-btn pt-btn-ghost pt-btn-sm"
              onClick={handleMarkAllRead}
              disabled={loading || notifications.length === 0}
              style={{
                height: "auto",
                padding: "4px 8px",
                fontSize: 12,
                color: "var(--pt-primary)",
                background: "transparent",
                border: "none",
                cursor: loading ? "progress" : "pointer",
                borderRadius: "var(--pt-radius-sm)",
                transition: "background 0.15s ease",
                opacity: loading || notifications.length === 0 ? 0.5 : 1,
              }}
              onMouseEnter={(e) => {
                if (!loading) e.currentTarget.style.background = "var(--pt-surface-3)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = "transparent";
              }}
            >
              {loading ? "加载中…" : t("portfolioTrading.notification.markAllRead")}
            </button>
          </div>

          {/* 可滚动通知列表 */}
          <div
            className="thin-scrollbar"
            style={{ maxHeight: 320, overflowY: "auto" }}
          >
            {loading && notifications.length === 0 && (
              <div style={{ padding: 24, textAlign: "center", fontSize: 12, color: "var(--pt-muted-foreground)" }}>
                加载通知中…
              </div>
            )}
            {!loading && loadError && (
              <div style={{ padding: 20, textAlign: "center", fontSize: 12, color: "var(--pt-state-error)" }}>
                <div style={{ marginBottom: 8 }}>真实通知加载失败：{loadError}</div>
                <button
                  type="button"
                  onClick={() => fetchNotifications().catch(() => {})}
                  style={{ color: "var(--pt-primary)", background: "transparent", border: 0, cursor: "pointer" }}
                >
                  重试
                </button>
              </div>
            )}
            {!loading && !loadError && notifications.length === 0 && (
              <div style={{ padding: 24, textAlign: "center", fontSize: 12, color: "var(--pt-muted-foreground)" }}>
                暂无站内通知，请在设置中启用站内消息渠道并关联通知策略
              </div>
            )}
            {notifications.map((item) => {
              const mapEntry =
                NOTIF_ICON_MAP[item.type] ?? NOTIF_ICON_MAP.info;
              const { Icon, color, dim } = mapEntry;
              return (
                <div
                  key={item.id}
                  onClick={() => handleClickItem(item)}
                  style={{
                    position: "relative",
                    display: "flex",
                    gap: 12,
                    padding: "12px 16px",
                    cursor: loading ? "progress" : "pointer",
                    background: item.read
                      ? "transparent"
                      : "rgba(6, 182, 212, 0.08)",
                    transition: "background 0.15s ease",
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.background = "var(--pt-surface-3)";
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.background = item.read
                      ? "transparent"
                      : "rgba(6, 182, 212, 0.08)";
                  }}
                >
                  {/* 未读左侧 3px 青色竖条 */}
                  {!item.read && (
                    <span
                      style={{
                        position: "absolute",
                        left: 0,
                        top: 8,
                        bottom: 8,
                        width: 3,
                        background: "var(--pt-primary)",
                        borderRadius: "0 2px 2px 0",
                      }}
                    />
                  )}
                  {/* 彩色图标容器 */}
                  <div
                    style={{
                      width: 32,
                      height: 32,
                      borderRadius: "var(--pt-radius-md)",
                      background: dim,
                      color,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      flexShrink: 0,
                    }}
                  >
                    <Icon size={16} />
                  </div>
                  {/* 内容：标题 + 描述（2 行截断）+ 时间 */}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div
                      style={{
                        fontSize: 13,
                        fontWeight: 500,
                        color: "var(--pt-foreground)",
                        marginBottom: 2,
                      }}
                    >
                      {item.title}
                    </div>
                    <div
                      style={
                        {
                          fontSize: 12,
                          color: "var(--pt-muted-foreground)",
                          lineHeight: 1.5,
                          marginBottom: 4,
                          display: "-webkit-box",
                          WebkitLineClamp: 2,
                          WebkitBoxOrient: "vertical",
                          overflow: "hidden",
                        } as React.CSSProperties
                      }
                    >
                      {item.description}
                    </div>
                    <div
                      style={{
                        fontSize: 11,
                        color: "var(--pt-slate-500)",
                      }}
                    >
                      {item.time}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>

          {/* 底部：查看全部通知 */}
          <button
            type="button"
            onClick={handleViewAll}
            disabled={loading}
            style={{
              width: "100%",
              padding: 12,
              textAlign: "center",
              fontSize: 13,
              color: "var(--pt-primary)",
              background: "transparent",
              border: "none",
              borderTop: "1px solid var(--pt-border)",
              cursor: loading ? "progress" : "pointer",
              transition: "background 0.15s ease",
              opacity: loading ? 0.6 : 1,
            }}
            onMouseEnter={(e) => {
              if (!loading) e.currentTarget.style.background = "var(--pt-surface-3)";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = "transparent";
            }}
          >
            {t("portfolioTrading.notification.viewAll")}
          </button>
        </div>
      )}
    </div>
  );
};

export default NotificationDropdown;
