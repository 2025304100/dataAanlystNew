import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  BookOpen,
  MessageCircleQuestionMark,
  Headphones,
  MessageSquarePlus,
  CircleQuestionMark,
} from "lucide-react";
import { message } from "antd";
import { t } from "../../i18n";

/**
 * HelpDropdown — 顶部栏右侧问答帮助下拉
 *
 * 一比一对齐原型图「应用主框架.html」app-nav-right 帮助区域。
 * 包含 help-circle 图标按钮 + 下拉面板（4 菜单项 + 分隔线 + 版本信息）。
 *
 * 互斥：通过 onOpenChange / forceClose 与父组件协作实现与 NotificationDropdown 互斥，
 * 实际互斥逻辑由父组件（App.tsx）管理。
 *
 * i18n：使用 t('portfolioTrading.help.xxx')，对应 key 由 Task 13 补全，
 * 缺失时 t() 返回 key 字符串，不阻塞编译。
 *
 * 注：lucide-react@1.29.0 无 HelpCircle / MessageCircleQuestion 导出，
 * 使用 CircleQuestionMark 等价 help-circle，MessageCircleQuestionMark 等价 message-circle-question。
 */

type HelpAction = "manual" | "faq" | "support" | "feedback";

interface HelpItem {
  key: HelpAction;
  Icon: React.ElementType;
  title: string;
  subtitle: string;
  color: string;
  dim: string;
  toastKey: string;
}

interface HelpDropdownProps {
  /** 下拉打开/关闭时回调父组件，用于实现与 NotificationDropdown 互斥 */
  onOpenChange?: (open: boolean) => void;
  /** 父组件强制关闭（互斥用：当 NotificationDropdown 打开时置 true） */
  forceClose?: boolean;
}

/** 帮助菜单项（对齐原型图 help-dropdown 结构与配色） */
const HELP_ITEMS: HelpItem[] = [
  {
    key: "manual",
    Icon: BookOpen,
    title: t("portfolioTrading.help.manual"),
    subtitle: t("portfolioTrading.help.manualSub"),
    color: "var(--pt-state-info)",
    dim: "var(--pt-state-info-dim)",
    toastKey: "portfolioTrading.help.manualToast",
  },
  {
    key: "faq",
    Icon: MessageCircleQuestionMark,
    title: t("portfolioTrading.help.faq"),
    subtitle: t("portfolioTrading.help.faqSub"),
    color: "var(--pt-primary)",
    dim: "rgba(6, 182, 212, 0.12)",
    toastKey: "portfolioTrading.help.faqToast",
  },
  {
    key: "support",
    Icon: Headphones,
    title: t("portfolioTrading.help.support"),
    subtitle: t("portfolioTrading.help.supportSub"),
    color: "var(--pt-state-success)",
    dim: "var(--pt-state-success-dim)",
    toastKey: "portfolioTrading.help.supportToast",
  },
  {
    key: "feedback",
    Icon: MessageSquarePlus,
    title: t("portfolioTrading.help.feedback"),
    subtitle: t("portfolioTrading.help.feedbackSub"),
    color: "var(--pt-state-warning)",
    dim: "var(--pt-state-warning-dim)",
    toastKey: "portfolioTrading.help.feedbackToast",
  },
];

const HelpDropdown: React.FC<HelpDropdownProps> = ({
  onOpenChange,
  forceClose,
}) => {
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);

  /** 统一更新 open 状态并通知父组件（forceClose 触发的关闭不回调，父组件已知） */
  const updateOpen = useCallback(
    (next: boolean) => {
      setOpen(next);
      onOpenChange?.(next);
    },
    [onOpenChange],
  );

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

  /** 切换下拉展开/收起 */
  const handleToggle = useCallback(() => {
    updateOpen(!open);
  }, [open, updateOpen]);

  /** 点击菜单项：关闭下拉 + toast */
  const handleClickItem = useCallback(
    (item: HelpItem) => {
      updateOpen(false);
      message.success(t(item.toastKey));
    },
    [updateOpen],
  );

  return (
    <div ref={wrapperRef} style={{ position: "relative" }}>
      {/* ========== 帮助按钮（help-circle） ========== */}
      <button
        type="button"
        title={t("portfolioTrading.help.title")}
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
        <CircleQuestionMark size={20} />
      </button>

      {/* ========== 帮助下拉面板 ========== */}
      {open && (
        <div
          className="pt-dropdown"
          style={{
            position: "absolute",
            top: "calc(100% + 8px)",
            right: 0,
            zIndex: 100,
            minWidth: 280,
            padding: 8,
            background: "var(--pt-popover)",
            border: "1px solid var(--pt-border)",
            borderRadius: "var(--pt-radius-lg)",
            boxShadow: "var(--pt-shadow-lg)",
          }}
        >
          {HELP_ITEMS.map((item, idx) => (
            <React.Fragment key={item.key}>
              {/* 常见问题与联系客服之间分隔线（对齐原型图） */}
              {idx === 2 && (
                <div
                  style={{
                    height: 1,
                    background: "var(--pt-border)",
                    margin: "8px 0",
                  }}
                />
              )}
              <div
                onClick={() => handleClickItem(item)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                  padding: "10px 12px",
                  borderRadius: "var(--pt-radius-md)",
                  cursor: "pointer",
                  transition: "background 0.15s ease",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "var(--pt-surface-3)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "transparent";
                }}
              >
                {/* 图标容器 */}
                <div
                  style={{
                    width: 32,
                    height: 32,
                    borderRadius: "var(--pt-radius-md)",
                    background: item.dim,
                    color: item.color,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    flexShrink: 0,
                  }}
                >
                  <item.Icon size={16} />
                </div>
                {/* 标题 + 副标题 */}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      fontSize: 13,
                      fontWeight: 500,
                      color: "var(--pt-foreground)",
                    }}
                  >
                    {item.title}
                  </div>
                  <div
                    style={{
                      fontSize: 11,
                      color: "var(--pt-muted-foreground)",
                    }}
                  >
                    {item.subtitle}
                  </div>
                </div>
              </div>
            </React.Fragment>
          ))}

          {/* 分隔线 + 版本信息 */}
          <div
            style={{
              height: 1,
              background: "var(--pt-border)",
              margin: "8px 0",
            }}
          />
          <div
            style={{
              padding: "8px 12px",
              fontSize: 11,
              color: "var(--pt-slate-500)",
            }}
          >
            {t("portfolioTrading.help.version")}
          </div>
        </div>
      )}
    </div>
  );
};

export default HelpDropdown;
