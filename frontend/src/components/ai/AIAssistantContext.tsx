import React, { createContext, useContext, useState, useCallback } from "react";
import type { AIAssistantContext as AIContext } from "../../types";

interface AIAssistantContextValue {
  /** 当前是否打开 */
  open: boolean;
  /** 当前上下文 */
  context: AIContext | null;
  /** 打开助手（可携带上下文） */
  openAssistant: (context?: AIContext) => void;
  /** 关闭助手 */
  closeAssistant: () => void;
}

const AIAssistantCtx = createContext<AIAssistantContextValue | null>(null);

export function AIAssistantProvider({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const [context, setContext] = useState<AIContext | null>(null);

  const openAssistant = useCallback((ctx?: AIContext) => {
    setContext(ctx ?? null);
    setOpen(true);
  }, []);

  const closeAssistant = useCallback(() => {
    setOpen(false);
  }, []);

  return (
    <AIAssistantCtx.Provider value={{ open, context, openAssistant, closeAssistant }}>
      {children}
    </AIAssistantCtx.Provider>
  );
}

export function useAIAssistant() {
  const ctx = useContext(AIAssistantCtx);
  if (!ctx) throw new Error("useAIAssistant must be used within AIAssistantProvider");
  return ctx;
}
