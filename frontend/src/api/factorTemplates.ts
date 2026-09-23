/**
 * 因子实验模板 API（C2 / B4）。
 *
 * 契约来源：`app/api/routes/factor_mining.py`（前缀 /api/v1/factor-mining）：
 *   - POST /templates/seed       25 系统经典模板入表（幂等，scope=system 按 name 去重）
 *   - GET  /templates            列表（可按 scope/enabled 过滤）
 *   - GET  /templates/{id}       详情
 *   - POST /templates            个人模板创建（rule_config 必须含 formula）
 *   - POST /templates/{id}/copy  复制为个人模板（副本启停/版本独立）
 *   - POST /templates/{id}/enabled  启停（enabled: 0/1）
 *
 * 视图字段与 `template_service._to_view` 返回一致。
 */
import { requestJson } from "./client";

const BASE = "/api/v1/factor-mining/templates";
const JSON_HEADERS = { "Content-Type": "application/json" };

/** `_to_view` 返回的 rule_config 内层（B4 模板规则配置）。 */
export interface TemplateRuleConfig {
  name?: string;
  category?: string;
  formula?: string;
  params?: Record<string, unknown>;
  required_fields?: string[];
  economy_logic_zh?: string | null;
  priority?: number;
  enabled?: boolean;
}

/** `_to_view` 返回的模板行（C2）。 */
export interface FactorTemplate {
  template_id: string;
  name: string;
  description?: string | null;
  scope?: string | null;
  owner?: string | null;
  enabled?: number;
  version?: string | null;
  rule_config?: TemplateRuleConfig;
  created_at?: string | null;
}

export interface FactorTemplatePage {
  items: FactorTemplate[];
  total: number;
}

export const factorTemplatesApi = {
  list: (
    params: { scope?: string; enabled?: number | null; limit?: number } = {},
  ): Promise<FactorTemplatePage> => {
    const qs = new URLSearchParams();
    if (params.scope) qs.set("scope", params.scope);
    if (params.enabled != null) qs.set("enabled", String(params.enabled));
    if (params.limit != null) qs.set("limit", String(params.limit));
    const suffix = qs.toString();
    return requestJson<FactorTemplatePage>(
      `${BASE}${suffix ? `?${suffix}` : ""}`,
    );
  },

  /** 25 系统经典模板种子（幂等）。 */
  seed: (): Promise<{ created: number; total_system: number }> =>
    requestJson(`${BASE}/seed`, { method: "POST" }),

  /** 复制为个人模板（副本启停/版本独立）。 */
  copy: (
    templateId: string,
    owner = "local_user",
  ): Promise<FactorTemplate> =>
    requestJson<FactorTemplate>(
      `${BASE}/${encodeURIComponent(templateId)}/copy`,
      {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify({ owner }),
      },
    ),

  /** 启停模板（enabled: 0/1）。 */
  setEnabled: (
    templateId: string,
    enabled: number,
    owner = "local_user",
  ): Promise<FactorTemplate> =>
    requestJson<FactorTemplate>(
      `${BASE}/${encodeURIComponent(templateId)}/enabled`,
      {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify({ enabled: enabled ? 1 : 0, owner }),
      },
    ),
};