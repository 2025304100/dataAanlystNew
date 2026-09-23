/**
 * F1 历史经验库 API（B3）。
 *
 * 契约来源：`app/api/routes/factor_experience.py`（前缀 /api/v1/factor-experience）。
 *   - GET  /factor-experience          分页列表（负样本/归档均返回，供管理核验）
 *   - GET  /factor-experience/{id}     详情（含指标历史）
 *   - POST /factor-experience/{id}/archive  归档（抽取/抽样硬化排除）
 *   - POST /factor-experience          存回（挖掘域内部走服务层，外部面为 HTTP）
 */
import { requestJson } from "./client";

const BASE = "/api/v1/factor-experience";

export interface F1ExperienceMetric {
  metric_type: string;
  value?: number | null;
  period?: string | null;
  is_oos?: number;
}

export interface F1Experience {
  experience_id: string;
  formula_template: string;
  category?: string | null;
  source?: string | null;
  complexity?: Record<string, unknown>;
  avg_icir?: number | null;
  use_count?: number;
  success_count?: number;
  success_rate?: number | null;
  status?: string | null;
  is_negative_sample?: number;
  metrics?: F1ExperienceMetric[];
  created_at?: string | null;
}

export interface F1ExperiencePage {
  items: F1Experience[];
  total: number;
  page: number;
  page_size: number;
}

export const factorExperienceApi = {
  list: (
    params: { page?: number; page_size?: number; category?: string; source?: string } = {},
  ): Promise<F1ExperiencePage> => {
    const qs = new URLSearchParams();
    if (params.page) qs.set("page", String(params.page));
    if (params.page_size) qs.set("page_size", String(params.page_size));
    if (params.category) qs.set("category", params.category);
    if (params.source) qs.set("source", params.source);
    const suffix = qs.toString();
    return requestJson<F1ExperiencePage>(`${BASE}${suffix ? `?${suffix}` : ""}`);
  },

  get: (experienceId: string): Promise<F1Experience> =>
    requestJson<F1Experience>(`${BASE}/${encodeURIComponent(experienceId)}`),

  archive: (experienceId: string): Promise<{ experience_id: string; status: string }> =>
    requestJson(`${BASE}/${encodeURIComponent(experienceId)}/archive`, {
      method: "POST",
    }),
};