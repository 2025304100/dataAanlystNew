import { useState, useCallback, useEffect, useRef } from "react";
import {
  Button,
  Drawer,
  Input,
  Space,
  Tag,
  List,
  Spin,
  Empty,
  Alert,
  Tooltip,
  Popconfirm,
  Typography,
  Divider,
} from "antd";
import {
  RobotOutlined,
  SendOutlined,
  DeleteOutlined,
  PlusOutlined,
  SettingOutlined,
  CloseOutlined,
  ExclamationCircleOutlined,
} from "@ant-design/icons";
import { api } from "../../api/client";
import { t } from "../../i18n";
import type {
  AISession,
  AIMessage,
  AIResponse,
  AIHealth,
} from "../../types";
import { useAIAssistant } from "./AIAssistantContext";
import ExplanationCard from "./ExplanationCard";

const { Text, Paragraph } = Typography;

/** 错误分类：根据错误消息判断类型。 */
function classifyError(error: unknown): { type: "connection" | "timeout" | "rateLimited" | "format" | "unknown"; message: string } {
  const msg = error instanceof Error ? error.message : String(error);
  const lower = msg.toLowerCase();
  if (lower.includes("timeout") || lower.includes("请求超时") || lower.includes("request timeout")) {
    return { type: "timeout", message: t("aiAssistant.timeout") };
  }
  if (lower.includes("rate") || lower.includes("限流") || lower.includes("429") || lower.includes("rate limit")) {
    return { type: "rateLimited", message: t("aiAssistant.rateLimited") };
  }
  if (lower.includes("format") || lower.includes("json") || lower.includes("parse") || lower.includes("格式")) {
    return { type: "format", message: t("aiAssistant.formatError") };
  }
  if (lower.includes("connect") || lower.includes("network") || lower.includes("fetch") || lower.includes("连接")) {
    return { type: "connection", message: t("aiAssistant.connectionError") };
  }
  return { type: "unknown", message: msg };
}

/** 从 AIMessage.content 尝试解析 AIResponse。 */
function parseMessageContent(content: string): AIResponse | null {
  if (!content) return null;
  try {
    const data = JSON.parse(content);
    if (data && typeof data === "object" && "answer" in data) {
      return data as AIResponse;
    }
  } catch {
    // 非JSON，返回纯文本响应
  }
  return { answer: content, evidence: [], warnings: [], suggested_actions: [], draft: null, metadata: {} };
}

interface AIAssistantProps {
  /** 当前活跃的 tab 名（用于自动携带上下文） */
  activeTab: string;
  /** 跳转到 AI 设置页 */
  onGoToSettings: () => void;
}

export default function AIAssistant({ activeTab, onGoToSettings }: AIAssistantProps) {
  const { open, context, openAssistant, closeAssistant } = useAIAssistant();

  const [configured, setConfigured] = useState<boolean | null>(null);
  const [sessions, setSessions] = useState<AISession[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [currentSessionId, setCurrentSessionId] = useState<number | null>(null);
  const [messages, setMessages] = useState<AIMessage[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<{ type: string; message: string } | null>(null);

  const messagesEndRef = useRef<HTMLDivElement>(null);

  /** 检查 AI 是否已配置 */
  const checkConfigured = useCallback(async () => {
    try {
      const health = await api.getAIHealth();
      const hasEnabled = health.some((h: AIHealth) => h.is_enabled);
      setConfigured(hasEnabled);
    } catch {
      // 健康检查失败也视为未配置（不阻塞用户）
      setConfigured(false);
    }
  }, []);

  /** 加载会话列表 */
  const loadSessions = useCallback(async () => {
    setSessionsLoading(true);
    try {
      const resp = await api.getAISessions(20, 0);
      setSessions(resp.items || []);
    } catch {
      setSessions([]);
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  /** 加载会话消息 */
  const loadMessages = useCallback(async (sessionId: number) => {
    setMessagesLoading(true);
    setError(null);
    try {
      const resp = await api.getAIMessages(sessionId, 100);
      setMessages(resp.items || []);
    } catch {
      setMessages([]);
    } finally {
      setMessagesLoading(false);
    }
  }, []);

  /** 抽屉打开时初始化 */
  useEffect(() => {
    if (open) {
      checkConfigured();
      loadSessions();
    }
  }, [open, checkConfigured, loadSessions]);

  /** 选中会话时加载消息 */
  useEffect(() => {
    if (currentSessionId != null) {
      loadMessages(currentSessionId);
    } else {
      setMessages([]);
    }
  }, [currentSessionId, loadMessages]);

  /** 滚动到底部 */
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  /** 发送消息 */
  const sendMessage = useCallback(async () => {
    const text = input.trim();
    if (!text || sending) return;

    // 未配置时不发送
    if (configured === false) {
      return;
    }

    setError(null);
    setSending(true);

    // 乐观添加用户消息
    const tempUserMsg: AIMessage = {
      id: -Date.now(),
      session_id: currentSessionId ?? -1,
      role: "user",
      content: text,
      context_summary: null,
      prompt_tokens: 0,
      completion_tokens: 0,
      total_tokens: 0,
      latency_ms: null,
      model_used: null,
      provider_used: null,
      metadata_json: null,
      created_at: new Date().toISOString(),
    };
    const tempAssistantMsg: AIMessage = {
      ...tempUserMsg,
      id: tempUserMsg.id - 1,
      role: "assistant",
      content: "",
    };
    setMessages((prev) => [...prev, tempUserMsg, tempAssistantMsg]);
    setInput("");

    try {
      const sourcePage = context?.source_page ?? activeTab;
      const references = context?.references ?? {};
      let streamed = "";
      let completedSessionId: number | null = null;
      await api.streamAISession({
        title: text.slice(0, 80),
        source_page: sourcePage,
        message: text,
        references,
      }, {
        onDelta: (content) => {
          streamed += content;
          setMessages((prev) => prev.map((msg) => (
            msg.id === tempAssistantMsg.id ? { ...msg, content: streamed } : msg
          )));
        },
        onDone: (sessionId, response) => {
          completedSessionId = sessionId;
          setMessages((prev) => prev.map((msg) => (
            msg.id === tempAssistantMsg.id
              ? { ...msg, session_id: sessionId, content: JSON.stringify(response) }
              : msg.id === tempUserMsg.id ? { ...msg, session_id: sessionId } : msg
          )));
        },
      });

      if (completedSessionId != null) {
        setCurrentSessionId(completedSessionId);
        await loadSessions();
      }
    } catch (err) {
      const classified = classifyError(err);
      setError(classified);
      // 移除乐观消息
      setMessages((prev) => prev.filter((m) => m.id !== tempUserMsg.id && m.id !== tempAssistantMsg.id));
    } finally {
      setSending(false);
    }
  }, [input, sending, configured, context, activeTab, loadMessages, loadSessions]);

  /** 删除会话 */
  const handleDeleteSession = useCallback(async (sessionId: number) => {
    try {
      await api.deleteAISession(sessionId);
      if (currentSessionId === sessionId) {
        setCurrentSessionId(null);
        setMessages([]);
      }
      await loadSessions();
    } catch {
      // 静默失败
    }
  }, [currentSessionId, loadSessions]);

  /** 新建会话 */
  const handleNewSession = useCallback(() => {
    setCurrentSessionId(null);
    setMessages([]);
    setError(null);
  }, []);

  /** 处理上下文标签 */
  const contextTags: Array<{ key: string; label: string; value: string }> = [];
  if (context?.source_page) {
    contextTags.push({ key: "source", label: t("aiAssistant.context"), value: context.source_page });
  }
  if (context?.references) {
    for (const [key, value] of Object.entries(context.references)) {
      contextTags.push({ key, label: key, value: String(value) });
    }
  }

  const drawerTitle = (
    <Space>
      <RobotOutlined />
      <span>{t("aiAssistant.title")}</span>
      {configured === false && (
        <Tag color="red">{t("aiAssistant.notConfigured")}</Tag>
      )}
    </Space>
  );

  return (
    <>
      {/* 浮动按钮 */}
      <Tooltip title={t("aiAssistant.open")} placement="left">
        <Button
          type="primary"
          shape="circle"
          size="large"
          icon={<RobotOutlined />}
          onClick={() => openAssistant()}
          data-testid="ai-assistant-fab"
          aria-label={t("aiAssistant.open")}
          style={{
            position: "fixed",
            right: 24,
            bottom: 24,
            zIndex: 1000,
            width: 56,
            height: 56,
            boxShadow: "0 4px 12px rgba(0,0,0,0.25)",
          }}
        />
      </Tooltip>

      {/* 抽屉 */}
      <Drawer
        open={open}
        onClose={closeAssistant}
        title={drawerTitle}
        placement="right"
        width={520}
        data-testid="ai-assistant-drawer"
        styles={{ body: { padding: 0, display: "flex", flexDirection: "column" } }}
      >
        {/* 未配置提示 */}
        {configured === false && (
          <Alert
            type="info"
            showIcon
            icon={<ExclamationCircleOutlined />}
            message={t("aiAssistant.notConfigured")}
            action={
              <Button size="small" type="link" icon={<SettingOutlined />} onClick={onGoToSettings}>
                {t("aiAssistant.goToSettings")}
              </Button>
            }
            style={{ margin: 12, borderRadius: 8 }}
            data-testid="ai-not-configured-alert"
          />
        )}

        {/* 错误提示 */}
        {error && (
          <Alert
            type="error"
            showIcon
            message={error.message}
            closable
            onClose={() => setError(null)}
            style={{ margin: "0 12px", borderRadius: 8 }}
            data-testid="ai-error-alert"
          />
        )}

        {/* 上下文标签 */}
        {contextTags.length > 0 && (
          <div style={{ padding: "8px 12px" }}>
            <Space size={4} wrap>
              {contextTags.map((tag) => (
                <Tag key={tag.key} color="processing">
                  {tag.label}: {tag.value}
                </Tag>
              ))}
            </Space>
          </div>
        )}

        {/* 会话列表 + 消息区 */}
        <div style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column" }}>
          {/* 会话列表条 */}
          <div style={{ maxHeight: 120, overflowY: "auto", borderBottom: "1px solid #f0f0f0" }}>
            <div style={{ padding: "4px 12px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <Text type="secondary" style={{ fontSize: 12 }}>{t("aiAssistant.sessions")}</Text>
              <Button
                size="small"
                type="link"
                icon={<PlusOutlined />}
                onClick={handleNewSession}
              >
                {t("aiAssistant.newSession")}
              </Button>
            </div>
            {sessionsLoading ? (
              <div style={{ textAlign: "center", padding: 8 }}><Spin size="small" /></div>
            ) : sessions.length === 0 ? (
              <div style={{ textAlign: "center", padding: "4px 0 8px" }}>
                <Text type="secondary" style={{ fontSize: 12 }}>{t("aiAssistant.noSessions")}</Text>
              </div>
            ) : (
              <List
                size="small"
                dataSource={sessions}
                renderItem={(session) => (
                  <List.Item
                    style={{
                      padding: "4px 12px",
                      cursor: "pointer",
                      background: currentSessionId === session.id ? "#e6f4ff" : undefined,
                    }}
                    onClick={() => setCurrentSessionId(session.id)}
                    actions={[
                      <Popconfirm
                        key="delete"
                        title={t("aiAssistant.deleteSessionConfirm")}
                        onConfirm={(e) => {
                          e?.stopPropagation();
                          handleDeleteSession(session.id);
                        }}
                      >
                        <Button
                          size="small"
                          type="text"
                          danger
                          icon={<DeleteOutlined />}
                          onClick={(e) => e.stopPropagation()}
                        />
                      </Popconfirm>,
                    ]}
                  >
                    <List.Item.Meta
                      title={<Text ellipsis style={{ fontSize: 13, maxWidth: 280 }}>{session.title}</Text>}
                      description={
                        <Space size={4}>
                          {session.source_page && <Tag style={{ fontSize: 10 }}>{session.source_page}</Tag>}
                          <Text type="secondary" style={{ fontSize: 11 }}>
                            {session.created_at ? new Date(session.created_at).toLocaleString() : ""}
                          </Text>
                        </Space>
                      }
                    />
                  </List.Item>
                )}
              />
            )}
          </div>

          {/* 消息区 */}
          <div style={{ flex: 1, overflowY: "auto", padding: "8px 12px" }} data-testid="ai-messages-area">
            {messagesLoading ? (
              <div style={{ textAlign: "center", padding: 24 }}><Spin /></div>
            ) : messages.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("aiAssistant.noSessions")} />
            ) : (
              messages.map((msg) => {
                if (msg.role === "user") {
                  return (
                    <div key={msg.id} style={{ marginBottom: 12, textAlign: "right" }}>
                      <div
                        style={{
                          display: "inline-block",
                          maxWidth: "80%",
                          padding: "8px 12px",
                          background: "#e6f4ff",
                          borderRadius: 8,
                          textAlign: "left",
                          fontSize: 13,
                          whiteSpace: "pre-wrap",
                        }}
                      >
                        {msg.content}
                      </div>
                    </div>
                  );
                }
                // assistant 消息：尝试解析为 AIResponse
                const parsed = parseMessageContent(msg.content);
                if (parsed) {
                  return <ExplanationCard key={msg.id} response={parsed} />;
                }
                return (
                  <div key={msg.id} style={{ marginBottom: 12 }}>
                    <div style={{ padding: "8px 12px", background: "#f5f5f5", borderRadius: 8, fontSize: 13, whiteSpace: "pre-wrap" }}>
                      {msg.content}
                    </div>
                  </div>
                );
              })
            )}
            {sending && (
              <div style={{ textAlign: "center", padding: 8 }}>
                <Spin size="small" />
                <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>{t("aiAssistant.sending")}</Text>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>

          {/* 输入区 */}
          <div style={{ padding: "8px 12px", borderTop: "1px solid #f0f0f0" }}>
            <Input.TextArea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={t("aiAssistant.inputPlaceholder")}
              autoSize={{ minRows: 1, maxRows: 4 }}
              disabled={configured === false}
              onPressEnter={(e) => {
                if (!e.shiftKey) {
                  e.preventDefault();
                  sendMessage();
                }
              }}
              data-testid="ai-input"
            />
            <div style={{ marginTop: 4, display: "flex", justifyContent: "flex-end" }}>
              <Button
                type="primary"
                icon={<SendOutlined />}
                onClick={sendMessage}
                loading={sending}
                disabled={configured === false || !input.trim()}
                data-testid="ai-send-button"
              >
                {t("aiAssistant.send")}
              </Button>
            </div>
          </div>
        </div>
      </Drawer>
    </>
  );
}
