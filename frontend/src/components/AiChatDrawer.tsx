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
  formulaMode?: "indicator" | "factor";
  /** 额外上下文：方向、版本说明、参数等，会拼接到用户消息中传给AI */
  factorContext?: {
    direction?: string;
    changeNote?: string;
    paramsText?: string;
  };
  onClose: () => void;
  onInsertFormula: (formula: string) => void;
};

const FACTOR_TOKEN = /\b(open|high|low|close|volume|amount|turnover_rate|prev_close|pe_ttm|pb|main_net_inflow|roe_ttm|lhb_institution_net|hot_rank_pct|proxy_score|sma|ema|stddev|sum|mean|count|highest|lowest|ref|pct_change)\b/;

export default function AiChatDrawer({
  open,
  formula,
  formulaMode = "indicator",
  factorContext,
  onClose,
  onInsertFormula,
}: AiChatDrawerProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<any>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (open) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
      window.setTimeout(() => inputRef.current?.focus(), 300);
      return;
    }
    abortRef.current?.abort();
    abortRef.current = null;
    setLoading(false);
  }, [open, messages]);

  const updateLastAssistant = (updater: (content: string) => string) => {
    setMessages((prev) => {
      const next = [...prev];
      for (let i = next.length - 1; i >= 0; i -= 1) {
        if (next[i].role === "assistant") {
          next[i] = { ...next[i], content: updater(next[i].content) };
          break;
        }
      }
      return next;
    });
  };

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || loading) return;

    const history = messages.map((m) => ({ role: m.role, content: m.content }));
    setMessages((prev) => [...prev, { role: "user", content: text }, { role: "assistant", content: "" }]);
    setInput("");
    setLoading(true);

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await api.streamAIChat(
        {
          message: text,
          formula,
          history,
          formula_mode: formulaMode,
          factor_direction: factorContext?.direction,
          factor_change_note: factorContext?.changeNote,
          factor_params_text: factorContext?.paramsText,
        },
        {
          onDelta: (content) => updateLastAssistant((current) => `${current}${content}`),
          onDone: (result) => {
            if (!result.ok && result.error) {
              updateLastAssistant((current) => (current ? `${current}\n\n${result.error}` : result.error));
            }
          },
        },
        controller.signal,
      );
    } catch (error: any) {
      if (error?.name === "AbortError") return;
      const errMsg = error?.message || t("aiChatFailed");
      message.error(errMsg);
      updateLastAssistant((current) => (current ? `${current}\n\n${errMsg}` : errMsg));
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
        setLoading(false);
      }
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  const clearChat = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setLoading(false);
    setMessages([]);
  };

  const extractFormula = (text: string): string | null => {
    const cleanText = text.split("\n\n注意：")[0];
    const codeBlockMatch = cleanText.match(/```(?:formula)?\s*\n?([\s\S]*?)```/);
    if (codeBlockMatch?.[1]?.trim()) return codeBlockMatch[1].trim();

    if (formulaMode === "factor") {
      const inlineCode = cleanText.match(/`([^`\n]+)`/);
      if (inlineCode?.[1]?.trim()) return inlineCode[1].trim();
      const candidate = cleanText
        .split("\n")
        .map((line) => line.trim().replace(/^(?:公式|formula)\s*[:：]\s*/i, ""))
        .find((line) => FACTOR_TOKEN.test(line) && /[()+\-*/><]/.test(line));
      return candidate || null;
    }

    const allowedFuncs = "sma|ema|rsi|macd|macd_signal|macd_hist|atr|boll_upper|boll_mid|boll_lower|kdj_k|kdj_d|kdj_j|highest|lowest|ref|pct_change|volume_ratio|cross_over|cross_under|abs|min|max|round";
    const allowedVars = "open|high|low|close|volume|amount|turnover_rate|prev_close|quality_score|timing_score|trend_score|momentum_score|True|False";
    const pattern = new RegExp(
      `((?:${allowedFuncs})\\([^)]*\\)\\s*(?:[><=!]=?\\s*(?:${allowedFuncs}\\([^)]*\\)|${allowedVars}|\\d+(?:\\.\\d+)?)|(?:${allowedVars}|\\d+(?:\\.\\d+)?)\\s*[><=!]=?\\s*(?:${allowedFuncs}\\([^)]*\\))))` +
        `(?:\\s+(?:and|or)\\s+(?:(?:${allowedFuncs})\\([^)]*\\)\\s*[><=!]=?\\s*(?:${allowedFuncs}\\([^)]*\\)|${allowedVars}|\\d+(?:\\.\\d+)?)|(?:${allowedVars}|\\d+(?:\\.\\d+)?)\\s*[><=!]=?\\s*(?:${allowedFuncs}\\([^)]*\\))))*`,
      "i",
    );
    const inlineMatch = cleanText.match(pattern);
    return inlineMatch?.[1]?.trim() || null;
  };

  const renderMessage = (msg: ChatMessage, index: number) => {
    const isUser = msg.role === "user";
    const formulaInReply = !isUser ? extractFormula(msg.content) : null;

    return (
      <div key={index} style={{ display: "flex", gap: 8, marginBottom: 16, flexDirection: isUser ? "row-reverse" : "row" }}>
        <div
          style={{
            width: 32,
            height: 32,
            borderRadius: "50%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            background: isUser ? "#1677ff" : "#f0f0f0",
            color: isUser ? "#fff" : "#666",
            flexShrink: 0,
            fontSize: 14,
          }}
        >
          {isUser ? <UserOutlined /> : <RobotOutlined />}
        </div>
        <div
          style={{
            maxWidth: "75%",
            padding: "10px 14px",
            borderRadius: 12,
            background: isUser ? "#1677ff" : "#f5f5f5",
            color: isUser ? "#fff" : "#333",
            fontSize: 13,
            lineHeight: 1.6,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            minHeight: 42,
          }}
        >
          {msg.content}
          {!isUser && formulaInReply && (
            <div style={{ marginTop: 8, display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
              <Tag color="blue" style={{ margin: 0, fontSize: 12 }}>
                {formulaInReply}
              </Tag>
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
          <Tag color="blue" style={{ marginLeft: 8 }}>
            {t("aiChatSubtitle")}
          </Tag>
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
            <div style={{ marginTop: 8, fontSize: 12, color: "#bbb" }}>{t("aiChatHint")}</div>
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
            disabled={!input.trim() || loading}
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
