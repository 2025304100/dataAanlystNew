import React, { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Bell, CircleCheck, TriangleAlert, CircleX, Info } from "lucide-react";
import { message } from "antd";
import { t } from "../../i18n";
import { api } from "../../api/client";
import NavIconButton from "./NavIconButton";

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
  source_type?: string | null;
  event_type?: string | null;
}

/** 点击通知的跳转目标：事件类型优先，来源分组兜底；无映射则不跳转（仅标记已读） */
const NOTIFICATION_NAV: Record<string, { tab: string; section?: string; subtab?: string }> = {
  trade_executed: { tab: "portfolio" },
  discovery_new: { tab: "opportunity" },
  task_failed: { tab: "settings", section: "tasks" },
  data_stale: { tab: "settings", section: "alerts" },
  score_drop: { tab: "settings", section: "alerts" },
  // 行情消息 P0-2：定时采集全部来源失败的告警，点击跳转行情消息页
  market_news_collect_failed: { tab: "news" },
  market_signal_alert: { tab: "news", subtab: "signals" },
};
const SOURCE_TAB_FALLBACK: Record<string, { tab: string }> = {
  portfolio: { tab: "portfolio" },
  opportunity: { tab: "opportunity" },
};

export interface NotificationNavigateTarget {
  tab: string;
  section?: string;
  subtab?: string;
}

interface NotificationDropdownProps {
  /** 下拉打开/关闭时回调父组件，用于实现与 HelpDropdown 互斥 */
  onOpenChange?: (open: boolean) => void;
  /** 父组件强制关闭（互斥用：当 HelpDropdown 打开时置 true） */
  forceClose?: boolean;
  /** Open the unified notification delivery log in Settings. */
  onViewAll?: () => void;
  /** 点击单条通知跳转对应页面（无映射的目标由调用方忽略） */
  onNavigate?: (target: NotificationNavigateTarget) => void;
}

/** 通知类型 → 图标映射（对齐原型图 notif-icon 内联色） */
const NOTIF_ICON_MAP: Record<
  NotificationType,
  { Icon: React.ElementType; color: string }
> = {
  success: { Icon: CircleCheck, color: "var(--pt-state-success)" },
  warning: { Icon: TriangleAlert, color: "var(--pt-state-warning)" },
  error: { Icon: CircleX, color: "var(--pt-state-error)" },
  info: { Icon: Info, color: "var(--pt-state-info)" },
};

const NotificationDropdown: React.FC<NotificationDropdownProps> = ({
  onOpenChange,
  forceClose,
  onViewAll,
  onNavigate,
}) => {
  const [open, setOpen] = useState(false);
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const bellRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  // 面板 portal 到 body + fixed 定位：一级菜单 nav 有 overflow-x:auto，
  // absolute 面板会被裁掉；fixed 相对视口不受裁剪（bell 在 sticky header 内，垂直位置恒定）
  const [panelPos, setPanelPos] = useState<{ top: number; right: number } | null>(null);

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
            source_type: r.source_type ?? null,
            event_type: r.event_type ?? null,
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

  /** 切换下拉展开/收起；打开时拉取 API 数据 + 计算面板 fixed 坐标 */
  const handleToggle = useCallback(() => {
    const next = !open;
    updateOpen(next);
    if (next) {
      const rect = bellRef.current?.getBoundingClientRect();
      if (rect) {
        setPanelPos({
          top: rect.bottom + 8,
          right: Math.max(8, window.innerWidth - rect.right),
        });
      }
      fetchNotifications().catch(() => {});
    }
  }, [open, updateOpen, fetchNotifications]);

  // 打开时监听 resize：bell 水平位置随布局变化，重算面板坐标
  useEffect(() => {
    if (!open) return;
    const onResize = () => {
      const rect = bellRef.current?.getBoundingClientRect();
      if (rect) {
        setPanelPos({
          top: rect.bottom + 8,
          right: Math.max(8, window.innerWidth - rect.right),
        });
      }
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [open]);

  /** forceClose 由父组件控制：true 时关闭本下拉 */
  useEffect(() => {
    if (forceClose) {
      setOpen(false);
    }
  }, [forceClose]);

  /** 点击外部关闭（仅 open 时绑定监听）；bell 与 portal 面板内部不算外部 */
  useEffect(() => {
    if (!open) return;
    function handleMouseDown(e: MouseEvent) {
      const target = e.target as Node;
      if (
        wrapperRef.current &&
        !wrapperRef.current.contains(target) &&
        !(panelRef.current && panelRef.current.contains(target))
      ) {
        updateOpen(false);
      }
    }
    document.addEventListener("mousedown", handleMouseDown);
    return () => document.removeEventListener("mousedown", handleMouseDown);
  }, [open, updateOpen]);

  /** 点击单条通知 → 标记已读 + 跳转对应页面 + toast */
  const handleClickItem = useCallback(
    (item: NotificationItem) => {
      // 先乐观更新 UI（避免慢网络导致看不到已读效果）
      setNotifications((prev) =>
        prev.map((n) => (n.id === item.id ? { ...n, read: true } : n)),
      );
      updateOpen(false);
      // 跳转对应页面（事件类型优先，来源分组兜底；无映射不跳）
      if (onNavigate) {
        const target =
          NOTIFICATION_NAV[item.event_type ?? ""] ??
          SOURCE_TAB_FALLBACK[item.source_type ?? ""];
        if (target) onNavigate(target);
      }
      api.markInboxItemRead(item.id)
        .then(() => {
          message.success(
            t("portfolioTrading.notification.viewed").replace("{title}", item.title),
          );
        })
        .catch((err: any) => {
          // eslint-disable-next-line no-console
          console.warn("[notifications] mark read failed:", err?.message ?? err);
          setNotifications((prev) =>
            prev.map((n) => (n.id === item.id ? { ...n, read: item.read } : n)),
          );
          message.error(err?.message ?? "标记通知已读失败");
        });
    },
    [onNavigate, updateOpen],
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
      {/* ========== 通知按钮（bell，与帮助按钮共用 NavIconButton 保证风格统一） ========== */}
      <NavIconButton
        ref={bellRef}
        title={t("portfolioTrading.notification.title")}
        onClick={handleToggle}
        badge={hasUnread}
      >
        <Bell size={20} />
      </NavIconButton>

      {/* ========== 通知下拉面板（portal 到 body + fixed，避开 nav 的 overflow 裁剪；
          外层 nav-tools-light 保持米白变量作用域随面板一起 portal） ========== */}
      {open && panelPos && createPortal(
        <div className="nav-tools-light">
        <div
          ref={panelRef}
          className="pt-dropdown"
          style={{
            position: "fixed",
            top: panelPos.top,
            right: panelPos.right,
            zIndex: 100,
            width: 380,
            minWidth: 380,
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
              const { Icon, color } = mapEntry;
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
                    background: "transparent",
                    transition: "background 0.15s ease",
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.background = "var(--pt-surface-3)";
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.background = "transparent";
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
                  {/* 图标容器：中性底圆形，语义色只留在 16px 图标上，避免整片色块 */}
                  <div
                    style={{
                      width: 32,
                      height: 32,
                      borderRadius: "50%",
                      background: "var(--pt-surface-3)",
                      color,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      flexShrink: 0,
                    }}
                  >
                    <Icon size={16} />
                  </div>
                  {/* 内容：标题 + 时间同行（减少堆行），描述 2 行截断 */}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div
                      style={{
                        display: "flex",
                        alignItems: "baseline",
                        justifyContent: "space-between",
                        gap: 8,
                        marginBottom: 2,
                      }}
                    >
                      <div
                        style={{
                          fontSize: 13,
                          fontWeight: 500,
                          color: "var(--pt-foreground)",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {item.title}
                      </div>
                      <div
                        style={{
                          fontSize: 11,
                          color: "var(--pt-muted-foreground)",
                          flexShrink: 0,
                        }}
                      >
                        {item.time}
                      </div>
                    </div>
                    <div
                      style={
                        {
                          fontSize: 12,
                          color: "var(--pt-muted-foreground)",
                          lineHeight: 1.5,
                          display: "-webkit-box",
                          WebkitLineClamp: 2,
                          WebkitBoxOrient: "vertical",
                          overflow: "hidden",
                        } as React.CSSProperties
                      }
                    >
                      {item.description}
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
        </div>
        ,
        document.body
      )}
    </div>
  );
};

export default NotificationDropdown;
