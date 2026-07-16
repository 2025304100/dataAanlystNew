import { useEffect, useRef, useState } from "react";
import { Button, Input, Modal, Space, Tag, message } from "antd";
import { RobotOutlined, SendOutlined, UserOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { t } from "../i18n";

type ChatMessage = {
  role: "user" | "assistant";
  content: string;
};

type AiChatDrawerProps = {
  open: boolean;
  formula: string;
  onClose: () => void;
  onInsertFormula: (formula: string) => void;
};

export default function AiChatDrawer({ open, formula, onClose, onInsertFormula }: AiChatDrawerProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<any>(null);

  useEffect(() => {
    if (open) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
      setTimeout(() => inputRef.current?.focus(), 300);
    }
  }, [open, messages]);

  // 当公式变化时，更新系统上下文（不发送消息，仅作为下次请求的上下文）
  // 这里不需要额外处理，因为每次发送都会带上当前 formula

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || loading) return;

    const userMsg: ChatMessage = { role: "user", content: text };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setLoading(true);

    try {
      const history = messages.map((m) => ({ role: m.role, content: m.content }));
      const result = await api.aiChat({ message: text, formula, history });
      if (result.ok) {
        setMessages((prev) => [...prev, { role: "assistant", content: result.reply }]);
      } else {
        message.error(result.error || t("aiChatFailed"));
        setMessages((prev) => [...prev, { role: "assistant", content: `️ ${result.error || t("aiChatFailed")}` }]);
      }
    } catch (error: any) {
      const errMsg = error?.message || t("aiChatFailed");
      message.error(errMsg);
      setMessages((prev) => [...prev, { role: "assistant", content: `⚠️ ${errMsg}` }]);
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  const clearChat = () => {
    setMessages([]);
  };

  // 从 AI 回复中提取公式（严格匹配项目框架内的公式模式）
  const extractFormula = (text: string): string | null => {
    // 先去掉后端校验不通过时追加的警告后缀
    const cleanText = text.split("\n\n⚠️")[0];

    // 优先匹配 ``` 代码块中的公式
    const codeBlockMatch = cleanText.match(/```(?:formula)?\s*\n?([\s\S]*?)```/);
    if (codeBlockMatch) {
      const extracted = codeBlockMatch[1].trim();
      if (extracted) return extracted;
    }

    // 匹配行内公式：必须包含系统允许的函数名 + 比较运算符
    const allowedFuncs = "sma|ema|rsi|macd|macd_signal|macd_hist|atr|boll_upper|boll_mid|boll_lower|kdj_k|kdj_d|kdj_j|highest|lowest|ref|pct_change|volume_ratio|cross_over|cross_under|abs|min|max|round";
    const allowedVars = "open|high|low|close|volume|amount|turnover_rate|prev_close|quality_score|timing_score|trend_score|momentum_score|True|False";
    const pattern = new RegExp(
      `((?:${allowedFuncs})\\([^)]*\\)\\s*(?:[><=!]=?\\s*(?:${allowedFuncs}\\([^)]*\\)|${allowedVars}|\\d+(?:\\.\\d+)?)|(?:${allowedVars}|\\d+(?:\\.\\d+)?)\\s*[><=!]=?\\s*(?:${allowedFuncs}\\([^)]*\\))))` +
      `(?:\\s+(?:and|or)\\s+(?:(?:${allowedFuncs})\\([^)]*\\)\\s*[><=!]=?\\s*(?:${allowedFuncs}\\([^)]*\\)|${allowedVars}|\\d+(?:\\.\\d+)?)|(?:${allowedVars}|\\d+(?:\\.\\d+)?)\\s*[><=!]=?\\s*(?:${allowedFuncs}\\([^)]*\\))))*`,
      "i"
    );
    const inlineMatch = cleanText.match(pattern);
    if (inlineMatch) return inlineMatch[1].trim();

    return null;
  };

  const renderMessage = (msg: ChatMessage, index: number) => {
    const isUser = msg.role === "user";
    const formulaInReply = !isUser ? extractFormula(msg.content) : null;

    return (
      <div key={index} style={{ display: "flex", gap: 8, marginBottom: 16, flexDirection: isUser ? "row-reverse" : "row" }}>
        <div style={{
          width: 32, height: 32, borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center",
          background: isUser ? "#1677ff" : "#f0f0f0", color: isUser ? "#fff" : "#666", flexShrink: 0, fontSize: 14,
        }}>
          {isUser ? <UserOutlined /> : <RobotOutlined />}
        </div>
        <div style={{
          maxWidth: "75%", padding: "10px 14px", borderRadius: 12,
          background: isUser ? "#1677ff" : "#f5f5f5",
          color: isUser ? "#fff" : "#333", fontSize: 13, lineHeight: 1.6,
          whiteSpace: "pre-wrap", wordBreak: "break-word",
        }}>
          {msg.content}
          {!isUser && formulaInReply && (
            <div style={{ marginTop: 8, display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
              <Tag color="blue" style={{ margin: 0, fontSize: 12 }}>{formulaInReply}</Tag>
              <Button size="small" type="link" style={{ padding: 0, height: "auto", fontSize: 12 }} onClick={() => onInsertFormula(formulaInReply)}>
                {t("aiInsertFormula")}
              </Button>
            </div>
          )}
        </div>
      </div>
    );
  };

  return (
    <Modal
      title={
        <Space>
          <RobotOutlined />
          {t("aiChatTitle")}
          <Tag color="blue" style={{ marginLeft: 8 }}>{t("aiChatSubtitle")}</Tag>
        </Space>
      }
      open={open}
      onCancel={onClose}
      footer={null}
      width={520}
      styles={{ body: { padding: "16px 20px", maxHeight: "60vh", overflow: "hidden", display: "flex", flexDirection: "column" } }}
    >
      <div style={{ flex: 1, overflowY: "auto", marginBottom: 12 }}>
        {messages.length === 0 ? (
          <div style={{ textAlign: "center", color: "#999", padding: "40px 0", fontSize: 13 }}>
            <RobotOutlined style={{ fontSize: 32, marginBottom: 12, display: "block", color: "#d9d9d9" }} />
            {t("aiChatWelcome")}
            <div style={{ marginTop: 8, fontSize: 12, color: "#bbb" }}>
              {t("aiChatHint")}
            </div>
          </div>
        ) : (
          messages.map((msg, i) => renderMessage(msg, i))
        )}
        <div ref={messagesEndRef} />
      </div>

      <div style={{ borderTop: "1px solid #f0f0f0", paddingTop: 12, display: "flex", gap: 8 }}>
        <Input.TextArea
          ref={inputRef}
          rows={2}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={t("aiChatPlaceholder")}
          maxLength={500}
          showCount={false}
          style={{ resize: "none" }}
        />
        <div style={{ display: "flex", flexDirection: "column", gap: 4, justifyContent: "flex-end" }}>
          <Button
            type="primary"
            icon={<SendOutlined />}
            onClick={sendMessage}
            loading={loading}
            disabled={!input.trim()}
            size="small"
          >
            {t("aiSend")}
          </Button>
          {messages.length > 0 && (
            <Button size="small" onClick={clearChat} disabled={loading}>
              {t("aiClearChat")}
            </Button>
          )}
        </div>
      </div>
    </Modal>
  );
}
