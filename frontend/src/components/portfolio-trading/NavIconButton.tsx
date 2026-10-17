import React from "react";

/**
 * NavIconButton — 顶部栏右侧图标按钮（消息通知 / 帮助 共用）
 *
 * 风格单一来源：NotificationDropdown 与 HelpDropdown 的触发按钮必须共用本组件，
 * 避免两处各写一份 inline style 导致漂移（曾出现铃铛与问号视觉不一致）。
 * 样式值与 2026-10-17 统一前逐值一致（32×32、radius 6、transparent 底、
 * hover --pt-surface-3 + --pt-foreground），视觉不回退。
 *
 * - badge：未读红点（消息通知用；帮助无）
 * - hover 用 JS inline 处理（样式为 inline，CSS :hover 不可达）
 * - focus-visible：键盘焦点环，保证两个按钮可达性一致
 */
export interface NavIconButtonProps {
  /** 按钮 title（同时作 aria-label） */
  title: string;
  onClick: () => void;
  /** 图标元素（lucide 图标，size 20） */
  children: React.ReactNode;
  /** 是否显示未读红点 */
  badge?: boolean;
}

export const NavIconButton = React.forwardRef<HTMLButtonElement, NavIconButtonProps>(
  ({ title, onClick, children, badge = false }, ref) => {
  const applyHover = (el: HTMLButtonElement, hover: boolean) => {
    el.style.background = hover ? "var(--pt-surface-3)" : "transparent";
    el.style.color = hover ? "var(--pt-foreground)" : "var(--pt-muted-foreground)";
  };

  return (
    <button
      ref={ref}
      type="button"
      title={title}
      aria-label={title}
      onClick={onClick}
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
      onMouseEnter={(e) => applyHover(e.currentTarget, true)}
      onMouseLeave={(e) => applyHover(e.currentTarget, false)}
      onFocus={(e) => applyHover(e.currentTarget, true)}
      onBlur={(e) => applyHover(e.currentTarget, false)}
    >
      {children}
      {badge && (
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
  );
});

NavIconButton.displayName = "NavIconButton";

export default NavIconButton;
