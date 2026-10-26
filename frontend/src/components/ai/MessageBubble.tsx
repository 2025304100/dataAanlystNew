import { Button, Tooltip, theme } from "antd";
import { CopyOutlined, ReloadOutlined, RobotOutlined, UserOutlined } from "@ant-design/icons";
import { t } from "../../i18n";
import "./ai-assistant.css";

interface MessageBubbleProps {
  role: "user" | "assistant";
  /** 展示用的时间（ISO 字符串），为空则不显示 */
  createdAt?: string | null;
  /** 复制按钮回调（由父组件决定复制纯文本还是 answer） */
  onCopy?: () => void;
  /** 重新生成（仅 assistant 消息传入） */
  onRegenerate?: () => void;
  /** true 时不画气泡底色（用于结构化解释卡，避免卡片叠卡片） */
  bare?: boolean;
  children: React.ReactNode;
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/**
 * 单条消息气泡：头像 + 气泡 + 元信息行（时间 / 复制 / 重新生成）。
 * 修复点：①补头像角色区分与时间戳；②hover 出复制/重试操作（评估报告 P1-8）。
 */
export default function MessageBubble({
  role,
  createdAt,
  onCopy,
  onRegenerate,
  bare = false,
  children,
}: MessageBubbleProps) {
  const { token } = theme.useToken();
  const isUser = role === "user";
  const time = formatTime(createdAt);

  return (
    <div className={`ai-msg-row${isUser ? " ai-msg-row--user" : ""}`}>
      <div
        className="ai-msg-avatar"
        style={{
          background: isUser ? token.colorPrimary : token.colorFillSecondary,
          color: isUser ? token.colorTextLightSolid : token.colorTextSecondary,
        }}
        aria-hidden
      >
        {isUser ? <UserOutlined /> : <RobotOutlined />}
      </div>
      <div className="ai-msg-main">
        <div
          className="ai-msg-bubble"
          style={
            bare
              ? undefined
              : {
                  background: isUser ? token.colorPrimaryBg : token.colorFillQuaternary,
                  color: token.colorText,
                }
          }
        >
          {children}
        </div>
        {(time || onCopy || onRegenerate) && (
          <div className="ai-msg-meta">
            {time && <span style={{ color: token.colorTextTertiary }}>{time}</span>}
            <div className="ai-msg-actions">
              {onCopy && (
                <Tooltip title={t("aiAssistant.copy")}>
                  <Button
                    type="text"
                    size="small"
                    icon={<CopyOutlined />}
                    onClick={onCopy}
                    data-testid="ai-copy-button"
                    aria-label={t("aiAssistant.copy")}
                    style={{ color: token.colorTextTertiary }}
                  />
                </Tooltip>
              )}
              {onRegenerate && (
                <Tooltip title={t("aiAssistant.regenerate")}>
                  <Button
                    type="text"
                    size="small"
                    icon={<ReloadOutlined />}
                    onClick={onRegenerate}
                    data-testid="ai-regenerate-button"
                    aria-label={t("aiAssistant.regenerate")}
                    style={{ color: token.colorTextTertiary }}
                  />
                </Tooltip>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
