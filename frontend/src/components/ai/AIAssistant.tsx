import { useState, useCallback, useEffect, useMemo, useRef } from "react";
import {
  Alert,
  Button,
  Drawer,
  Empty,
  Input,
  Space,
  Spin,
  Tag,
  Tooltip,
  message,
  theme,
} from "antd";
import {
  RobotOutlined,
  SendOutlined,
  StopOutlined,
  SettingOutlined,
  ExclamationCircleOutlined,
  DownOutlined,
  PlusOutlined,
} from "@ant-design/icons";
import { api } from "../../api/client";
import { t, template } from "../../i18n";
import type {
  AISession,
  AIMessage,
  AIResponse,
  AIHealth,
} from "../../types";
import { useAIAssistant } from "./AIAssistantContext";
import ExplanationCard from "./ExplanationCard";
import MarkdownText from "./MarkdownText";
import MessageBubble from "./MessageBubble";
import SessionChipBar from "./SessionChipBar";

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

/** 上下文引用 key → 友好译名（评估报告 P1-10：禁止裸 key 上屏）。 */
const REF_KEY_LABELS: Record<string, string> = {
  symbol_id: "aiAssistant.refKey.symbol_id",
  candidate_id: "aiAssistant.refKey.candidate_id",
  task_id: "aiAssistant.refKey.task_id",
  portfolio_id: "aiAssistant.refKey.portfolio_id",
  backtest_id: "aiAssistant.refKey.backtest_id",
  factor_id: "aiAssistant.refKey.factor_id",
  batch_id: "aiAssistant.refKey.batch_id",
  session_id: "aiAssistant.refKey.session_id",
};

function refLabel(key: string): string {
  const i18nKey = REF_KEY_LABELS[key];
  return i18nKey ? t(i18nKey) : key;
}

/** 空状态建议问题：按来源页给 2 条（评估报告 P1-6）。
 *  key 覆盖 ExplainButton 实际 sourcePage（discovery/task_center/symbol_research）
 *  与 App 一级 tab（opportunity/portfolio/settings/...）；未命中走通用问题。 */
const SUGGESTED_BY_PAGE: Record<string, string[]> = {
  discovery: ["aiAssistant.suggested.opportunity1", "aiAssistant.suggested.opportunity2"],
  opportunity: ["aiAssistant.suggested.opportunity1", "aiAssistant.suggested.opportunity2"],
  task: ["aiAssistant.suggested.task1", "aiAssistant.suggested.task2"],
  task_center: ["aiAssistant.suggested.task1", "aiAssistant.suggested.task2"],
  symbol_research: ["aiAssistant.suggested.symbol1", "aiAssistant.suggested.symbol2"],
  portfolio: ["aiAssistant.suggested.portfolio1", "aiAssistant.suggested.portfolio2"],
  "portfolio-inspection": ["aiAssistant.suggested.portfolio1", "aiAssistant.suggested.portfolio2"],
  settings: ["aiAssistant.suggested.settings1", "aiAssistant.suggested.settings2"],
};

/**
 * 抽屉响应式宽度（评估报告 P0-1）。
 * 分档：≥1600 → 560px；≥992 → 520px；≥768 → min(480px, 62vw)；<768 → 全屏。
 * 直接读 window.innerWidth + resize 监听：比 antd Grid 断点更直白，
 * 且在 jsdom（无 matchMedia 语义）下行为确定、可测。
 */
export function useDrawerWidth(): string | number {
  const [width, setWidth] = useState<string | number>(() => computeDrawerWidth());
  useEffect(() => {
    const onResize = () => setWidth(computeDrawerWidth());
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return width;
}

function computeDrawerWidth(): string | number {
  if (typeof window === "undefined") return 520;
  const vw = window.innerWidth;
  if (vw >= 1600) return 560;
  if (vw >= 992) return 520;
  if (vw >= 768) return "min(480px, 62vw)";
  return "100%";
}

interface AIAssistantProps {
  /** 当前活跃的 tab 名（用于自动携带上下文） */
  activeTab: string;
  /** 跳转到 AI 设置页 */
  onGoToSettings: () => void;
}

export default function AIAssistant({ activeTab, onGoToSettings }: AIAssistantProps) {
  const { token } = theme.useToken();
  const { open, context, openAssistant, closeAssistant } = useAIAssistant();
  const drawerWidth = useDrawerWidth();

  const [configured, setConfigured] = useState<boolean | null>(null);
  const [sessions, setSessions] = useState<AISession[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [currentSessionId, setCurrentSessionId] = useState<number | null>(null);
  const [messages, setMessages] = useState<AIMessage[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState<{ type: string; message: string } | null>(null);
  const [showAllContext, setShowAllContext] = useState(false);
  const [showJumpLatest, setShowJumpLatest] = useState(false);

  const messagesScrollRef = useRef<HTMLDivElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const forceScrollRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);

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

  /** 加载会话列表（20 → 50：chip 条与搜索需要更多上下文，评估报告 L9） */
  const loadSessions = useCallback(async () => {
    setSessionsLoading(true);
    try {
      const resp = await api.getAISessions(50, 0);
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
    forceScrollRef.current = true;
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

  /** 卸载时中断在途请求，避免断线后继续 setState */
  useEffect(() => () => {
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  /** 发送耗时计时（评估报告 L10：长等待可见、可停止） */
  useEffect(() => {
    if (!sending) {
      setElapsed(0);
      return;
    }
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [sending]);

  /**
   * 智能滚动（评估报告 P0-4）：
   * 仅当用户本就停在底部附近（<80px）时才自动跟随；否则显示「回到最新」，
   * 不再像此前那样每个流式 delta 都强制 scrollIntoView 劫持用户上翻。
   */
  const handleMessagesScroll = useCallback(() => {
    const el = messagesScrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const atBottom = distance < 80;
    atBottomRef.current = atBottom;
    setShowJumpLatest(!atBottom);
  }, []);

  const jumpToLatest = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    atBottomRef.current = true;
    setShowJumpLatest(false);
  }, []);

  useEffect(() => {
    if (forceScrollRef.current || atBottomRef.current) {
      forceScrollRef.current = false;
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
      setShowJumpLatest(false);
    }
  }, [messages]);

  /**
   * 流式发送核心：sendMessage 与「重新生成」共用。
   * - 新增一轮：追加 user + assistant 占位；
   * - 重新生成：截断到该 assistant 消息处，复用原 user 消息。
   * 修复点：①传入 AbortSignal 支持停止（P0-3）；②用户停止时保留已流出内容
   * 并标注「已停止」，不当成错误清空（P0-3）。
   */
  const runStream = useCallback(async (
    text: string,
    opts: { reuseUserMsgId?: number; truncateFromIndex?: number } = {},
  ) => {
    const trimmed = text.trim();
    if (!trimmed || sending) return;
    // 未配置时不发送
    if (configured === false) return;

    setError(null);
    setSending(true);
    forceScrollRef.current = true;

    const nowIso = new Date().toISOString();
    const userMsgId = opts.reuseUserMsgId ?? -Date.now();
    const addedUserMsg = opts.reuseUserMsgId == null;
    const tempUserMsg: AIMessage = {
      id: userMsgId,
      session_id: currentSessionId ?? -1,
      role: "user",
      content: trimmed,
      context_summary: null,
      prompt_tokens: 0,
      completion_tokens: 0,
      total_tokens: 0,
      latency_ms: null,
      model_used: null,
      provider_used: null,
      metadata_json: null,
      created_at: nowIso,
    };
    const tempAssistantMsg: AIMessage = {
      ...tempUserMsg,
      id: userMsgId - 1,
      role: "assistant",
      content: "",
    };
    if (opts.truncateFromIndex != null) {
      setMessages((prev) => [...prev.slice(0, opts.truncateFromIndex), tempAssistantMsg]);
    } else {
      setMessages((prev) => [...prev, tempUserMsg, tempAssistantMsg]);
    }
    setInput("");

    const controller = new AbortController();
    abortRef.current = controller;
    let streamed = "";
    let completedSessionId: number | null = null;
    try {
      const sourcePage = context?.source_page ?? activeTab;
      const references = context?.references ?? {};
      await api.streamAISession({
        title: trimmed.slice(0, 80),
        source_page: sourcePage,
        message: trimmed,
        references,
        session_id: currentSessionId,
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
      }, controller.signal);

      if (completedSessionId != null) {
        setCurrentSessionId(completedSessionId);
        await loadSessions();
      }
    } catch (err) {
      if (controller.signal.aborted) {
        // 用户主动停止：保留已流出内容并标注，不移除消息、不报错
        const note = t("aiAssistant.stopped");
        setMessages((prev) => prev.map((m) => {
          if (m.id !== tempAssistantMsg.id) return m;
          if (!m.content) return { ...m, content: note };
          // 已完成结构化响应（JSON）时不追加标注，避免污染可解析内容
          if (m.content.trimStart().startsWith("{")) return m;
          return { ...m, content: `${m.content}\n\n${note}` };
        }));
      } else {
        const classified = classifyError(err);
        setError(classified);
        // 移除乐观消息（重新生成场景下原 user 消息要保留）
        setMessages((prev) => prev.filter((m) => (
          m.id !== tempAssistantMsg.id && (!addedUserMsg || m.id !== tempUserMsg.id)
        )));
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setSending(false);
    }
  }, [sending, configured, context, activeTab, currentSessionId, loadSessions]);

  /** 发送消息 */
  const sendMessage = useCallback(() => {
    const text = input.trim();
    if (!text || sending) return;
    void runStream(text);
  }, [input, sending, runStream]);

  /** 停止生成（P0-3） */
  const handleStop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  /** 重新生成：用该 assistant 消息前面最近的一条 user 消息重跑（P1-8） */
  const regenerate = useCallback(async (assistantMsgId: number) => {
    if (sending) return;
    const idx = messages.findIndex((m) => m.id === assistantMsgId);
    if (idx < 0) return;
    let userIdx = -1;
    for (let i = idx - 1; i >= 0; i -= 1) {
      if (messages[i].role === "user") {
        userIdx = i;
        break;
      }
    }
    if (userIdx < 0) return;
    const userMsg = messages[userIdx];
    await runStream(userMsg.content, { reuseUserMsgId: userMsg.id, truncateFromIndex: idx });
  }, [messages, sending, runStream]);

  /** 复制消息文本（P1-8） */
  const copyText = useCallback(async (text: string) => {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      message.success(t("aiAssistant.copied"));
    } catch {
      message.warning(t("aiAssistant.copyFailed"));
    }
  }, []);

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
    forceScrollRef.current = true;
  }, []);

  const contextTags: Array<{ key: string; label: string; value: string }> = [];
  if (context?.source_page) {
    contextTags.push({ key: "source", label: t("aiAssistant.context"), value: context.source_page });
  }
  if (context?.references) {
    for (const [key, value] of Object.entries(context.references)) {
      contextTags.push({ key, label: refLabel(key), value: String(value) });
    }
  }

  const suggestedKeys = useMemo(() => {
    const page = context?.source_page ?? activeTab;
    return SUGGESTED_BY_PAGE[page] ?? ["aiAssistant.suggested.generic1", "aiAssistant.suggested.generic2"];
  }, [context, activeTab]);

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
      {/* 浮动按钮：抽屉打开时隐藏（此前面板与 FAB 同 z-index 靠 DOM 顺序压层，脆弱）；
          bottom/right 叠加安全区适配，移动端避让手势条（P1-9） */}
      {!open && (
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
              right: "calc(24px + env(safe-area-inset-right))",
              bottom: "calc(24px + env(safe-area-inset-bottom))",
              zIndex: 1000,
              width: 56,
              height: 56,
              boxShadow: "0 4px 12px rgba(0,0,0,0.25)",
            }}
          />
        </Tooltip>
      )}

      {/* 抽屉 */}
      <Drawer
        open={open}
        onClose={closeAssistant}
        title={drawerTitle}
        placement="right"
        width={drawerWidth}
        data-testid="ai-assistant-drawer"
        extra={
          <Button
            size="small"
            type="link"
            icon={<PlusOutlined />}
            onClick={handleNewSession}
            data-testid="ai-new-session-header"
          >
            {t("aiAssistant.newSession")}
          </Button>
        }
        styles={{ body: { padding: 0, display: "flex", flexDirection: "column", overflow: "hidden" } }}
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
            style={{ margin: 12, borderRadius: 8, flexShrink: 0 }}
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
            style={{ margin: "0 12px", borderRadius: 8, flexShrink: 0 }}
            data-testid="ai-error-alert"
          />
        )}

        {/* 上下文标签：默认最多 2 条，其余折叠（P1-10 + 头部紧凑化） */}
        {contextTags.length > 0 && (
          <div style={{ padding: "8px 12px 0", flexShrink: 0 }}>
            <Space size={4} wrap>
              {(showAllContext ? contextTags : contextTags.slice(0, 2)).map((tag) => (
                <Tag key={tag.key} color="processing">
                  {tag.label}: {tag.value}
                </Tag>
              ))}
              {contextTags.length > 2 && (
                <Tag
                  style={{ cursor: "pointer" }}
                  onClick={() => setShowAllContext((v) => !v)}
                  data-testid="ai-context-toggle"
                >
                  {showAllContext ? t("aiAssistant.collapse") : `+${contextTags.length - 2}`}
                </Tag>
              )}
            </Space>
          </div>
        )}

        {/* 会话入口条：chip 条 / 展开面板（替代 120px 滚动列表，P0-2） */}
        <SessionChipBar
          sessions={sessions}
          currentSessionId={currentSessionId}
          loading={sessionsLoading}
          onSelect={(id) => setCurrentSessionId(id)}
          onDelete={handleDeleteSession}
        />

        {/* 消息区 */}
        <div
          ref={messagesScrollRef}
          onScroll={handleMessagesScroll}
          style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "8px 12px", position: "relative" }}
          data-testid="ai-messages-area"
        >
          {messagesLoading ? (
            <div style={{ textAlign: "center", padding: 24 }}><Spin /></div>
          ) : messages.length === 0 ? (
            <div style={{ textAlign: "center", padding: "36px 8px" }} data-testid="ai-empty-state">
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("aiAssistant.noMessages")} />
              {configured !== false && (
                <>
                  <div style={{ marginTop: 14, fontSize: 12, color: token.colorTextTertiary }}>
                    {t("aiAssistant.suggestedTitle")}
                  </div>
                  <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 6, alignItems: "center" }}>
                    {suggestedKeys.map((key) => (
                      <Button
                        key={key}
                        size="small"
                        type="dashed"
                        onClick={() => runStream(t(key))}
                        data-testid="ai-suggested-question"
                      >
                        {t(key)}
                      </Button>
                    ))}
                  </div>
                </>
              )}
            </div>
          ) : (
            messages.map((msg) => {
              if (msg.role === "user") {
                return (
                  <MessageBubble
                    key={msg.id}
                    role="user"
                    createdAt={msg.created_at}
                    onCopy={msg.content ? () => copyText(msg.content) : undefined}
                  >
                    <span style={{ whiteSpace: "pre-wrap" }}>{msg.content}</span>
                  </MessageBubble>
                );
              }
              // assistant 消息：content 为结构化 JSON 时走解释卡；流式占位（空 content）走轻渲染
              const parsed = parseMessageContent(msg.content);
              if (parsed) {
                return (
                  <MessageBubble
                    key={msg.id}
                    role="assistant"
                    createdAt={msg.created_at}
                    bare
                    onCopy={() => copyText(parsed.answer)}
                    onRegenerate={sending ? undefined : () => regenerate(msg.id)}
                  >
                    <ExplanationCard response={parsed} />
                  </MessageBubble>
                );
              }
              return (
                <MessageBubble
                  key={msg.id}
                  role="assistant"
                  createdAt={msg.created_at}
                  onRegenerate={sending ? undefined : () => regenerate(msg.id)}
                >
                  <MarkdownText content={msg.content} />
                </MessageBubble>
              );
            })
          )}
          {sending && (
            <div style={{ textAlign: "center", padding: 8 }} data-testid="ai-sending-indicator">
              <Spin size="small" />
              <span style={{ marginLeft: 8, fontSize: 12, color: token.colorTextTertiary }}>
                {elapsed >= 1 ? template("aiAssistant.sendingElapsed", { elapsed }) : t("aiAssistant.sending")}
              </span>
            </div>
          )}
          <div ref={messagesEndRef} />
          {showJumpLatest && (
            <Button
              size="small"
              shape="round"
              icon={<DownOutlined />}
              onClick={jumpToLatest}
              data-testid="ai-jump-latest"
              style={{
                position: "absolute",
                bottom: 12,
                left: "50%",
                transform: "translateX(-50%)",
                zIndex: 2,
                boxShadow: token.boxShadow,
              }}
            >
              {t("aiAssistant.jumpToLatest")}
            </Button>
          )}
        </div>

        {/* 输入区：与发送/停止按钮同行（P1-5），发送中禁用防双发（P0-3） */}
        <div style={{ padding: "8px 12px", borderTop: `1px solid ${token.colorBorderSecondary}`, flexShrink: 0 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <Input.TextArea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder={t("aiAssistant.inputPlaceholder")}
                autoSize={{ minRows: 1, maxRows: 4 }}
                disabled={configured === false || sending}
                onPressEnter={(e) => {
                  if (!e.shiftKey) {
                    e.preventDefault();
                    sendMessage();
                  }
                }}
                data-testid="ai-input"
              />
            </div>
            {sending ? (
              <Button
                danger
                icon={<StopOutlined />}
                onClick={handleStop}
                data-testid="ai-stop-button"
              >
                {t("aiAssistant.stop")}
              </Button>
            ) : (
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
            )}
          </div>
        </div>
      </Drawer>
    </>
  );
}
