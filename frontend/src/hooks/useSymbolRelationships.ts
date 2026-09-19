// WP1.5/WP1.2：useSymbolRelationships hook
// 调用 GET /api/v1/symbols/{symbol_id}/relationships 获取标的统一关联状态
// 使用项目现有 useState/useEffect 模式（不引入 react-query）
//
// 关键约束：
// - 接口失败时降级为"状态未知"，不误报"未加入"
// - symbolId 为 null/undefined 时不发请求
// - 支持手动刷新
import { useState, useEffect, useCallback, useRef } from "react";
import { api } from "../api/client";
import type { SymbolRelationships } from "../types/symbolRelationships";

export interface UseSymbolRelationshipsResult {
  data: SymbolRelationships | null;
  loading: boolean;
  error: string | null;
  // 是否处于降级状态（接口部分失败）
  degraded: boolean;
  // 手动刷新
  refresh: () => Promise<void>;
}

/**
 * 获取标的统一关联状态。
 * @param symbolId 标的 ID；为 null/0 时不发请求
 */
export function useSymbolRelationships(symbolId: number | null | undefined): UseSymbolRelationshipsResult {
  const [data, setData] = useState<SymbolRelationships | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [degraded, setDegraded] = useState(false);
  // 防止竞态：仅最后一次请求的结果会写入 state
  const requestIdRef = useRef(0);

  const fetchData = useCallback(async () => {
    if (!symbolId) {
      setData(null);
      setError(null);
      setDegraded(false);
      setLoading(false);
      return;
    }
    const requestId = ++requestIdRef.current;
    setLoading(true);
    // 乐观清空错误，但保留旧数据以便 UI 不闪烁
    setError(null);
    try {
      const result = await api.getSymbolRelationships(symbolId);
      // 仅最后一次请求写入 state，避免竞态
      if (requestIdRef.current !== requestId) return;
      setData(result);
      setDegraded(Boolean(result.degraded));
    } catch (err: unknown) {
      if (requestIdRef.current !== requestId) return;
      // 关键约束：接口失败时降级为"状态未知"，不误报"未加入"
      setData(null);
      setDegraded(true);
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg);
    } finally {
      if (requestIdRef.current === requestId) {
        setLoading(false);
      }
    }
  }, [symbolId]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  return {
    data,
    loading,
    error,
    degraded,
    refresh: fetchData,
  };
}
