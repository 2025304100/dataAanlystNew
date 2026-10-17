// 消息管理域统一视觉规范（2026-10-17 用户要求「所有界面统一下风格」）
//
// 单一来源：色板、状态 tag、公共样式 token。四个页签（渠道配置/推送策略/消息模板/
// 发送记录）+ 详情抽屉 + 顶部铃铛下拉全部从这里取色，禁各页签自建色值。
//
// 设计基线（实测页面主体，勿拍脑袋改）：
// - 主色 teal #0f766e（与一级 tab active / antd ConfigProvider colorPrimary 一致）
// - 页面近白 #f6f7f9 / 文字 #1f2937 / 次要文字 #64748b / 边框 #e2e8f0
// - 语义色不用 antd 预设名（green #52c41a / blue #1677ff / magenta 与 teal 体系脱节），
//   统一为浅底 + 语义色字 + 淡边的 Tag 风格

export const NOTIF = {
  primary: "#0f766e",
  success: "#0f766e",
  warning: "#b45309",
  error: "#dc2626",
  critical: "#991b1b",
  info: "#2563eb",
  neutral: "#64748b",
  border: "#e2e8f0",
} as const;

/** 语义色 → Tag 样式（浅底 + 语义色字 + 淡边） */
export function semanticTagStyle(tone: keyof typeof NOTIF | "neutral"): React.CSSProperties {
  const color = NOTIF[tone];
  return {
    color,
    background: `color-mix(in srgb, ${color} 8%, transparent)`,
    border: `1px solid color-mix(in srgb, ${color} 24%, transparent)`,
    borderRadius: 4,
    marginInlineEnd: 0,
  };
}

// ── Outbox 状态（发送记录页签 + 详情抽屉共用） ──────────────────────
export type OutboxTone = "neutral" | "info" | "success" | "warning" | "error" | "critical";

export const OUTBOX_STATUS_TONE: Record<string, OutboxTone> = {
  pending: "neutral",
  sending: "info",
  sent: "success",
  failed: "error",
  dead_letter: "critical",
};

export const DELIVERY_STATUS_TONE: Record<string, OutboxTone> = {
  success: "success",
  failed: "error",
  timeout: "warning",
  auth_failed: "critical",
  rate_limited: "warning",
};

// ── 渠道状态（渠道配置页签） ────────────────────────────────────────
export const CHANNEL_STATUS_TONE: Record<string, OutboxTone> = {
  unconfigured: "neutral",
  pending_test: "warning",
  test_success: "info",
  test_failed: "error",
  enabled: "success",
  disabled: "neutral",
};

// ── 严重级别（推送策略页签） ────────────────────────────────────────
export const SEVERITY_TONE: Record<string, OutboxTone> = {
  info: "info",
  warn: "warning",
  error: "error",
  critical: "critical",
};

// ── 公共样式 token（四页签工具栏/计数/分区标题统一） ──────────────────
export const NOTIF_LAYOUT = {
  toolbarGap: 8,
  toolbarMarginBottom: 16,
  countText: { color: NOTIF.neutral, fontSize: 12 } as React.CSSProperties,
  sectionTitle: { fontSize: 13, fontWeight: 500, color: "#1f2937" } as React.CSSProperties,
  labelText: { color: NOTIF.neutral, fontSize: 12 } as React.CSSProperties,
} as const;
