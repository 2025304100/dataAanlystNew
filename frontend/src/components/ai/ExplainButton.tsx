import { Button, Tooltip } from "antd";
import { RobotOutlined } from "@ant-design/icons";
import { t } from "../../i18n";
import { useAIAssistant } from "./AIAssistantContext";
import type { AIAssistantContext } from "../../types";

interface ExplainButtonProps {
  /** 来源页面标识 */
  sourcePage: string;
  /** 引用 ID（如 candidate_id, symbol_id, task_id 等） */
  references: Record<string, number | string>;
  /** 可选的初始问题 */
  initialQuestion?: string;
  /** 按钮尺寸 */
  size?: "small" | "middle" | "large";
  /** 按钮文字（覆盖默认） */
  label?: string;
  /** 额外样式 */
  style?: React.CSSProperties;
}

/**
 * WP-AI.7："让 AI 解释"统一按钮。
 * 点击后打开 AI 助手抽屉，自动携带当前上下文。
 */
export default function ExplainButton({
  sourcePage,
  references,
  initialQuestion,
  size = "small",
  label,
  style,
}: ExplainButtonProps) {
  const { openAssistant } = useAIAssistant();

  const handleClick = () => {
    const ctx: AIAssistantContext = {
      source_page: sourcePage,
      references,
      initial_question: initialQuestion,
    };
    openAssistant(ctx);
  };

  return (
    <Tooltip title={label ?? t("aiAssistant.explain")}>
      <Button
        type="link"
        size={size}
        icon={<RobotOutlined />}
        onClick={handleClick}
        data-testid="ai-explain-button"
        aria-label={t("aiAssistant.explain")}
        style={style}
      >
        {label ?? t("aiAssistant.explain")}
      </Button>
    </Tooltip>
  );
}
