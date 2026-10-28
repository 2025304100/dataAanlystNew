import { requestJson } from "./client";
import type { SignalAssessment, SignalDashboard, SignalEvidence, SignalJob, SignalMetadata, SignalPage, SignalPrediction, SignalRule, SignalRuleDraft, SignalSettings } from "../types/marketSignal";
const ROOT = "/api/v1/market-signals";
const send = <T>(path: string, method: string, body?: unknown) => requestJson<T>(ROOT+path, {
  method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body),
});
export const signalApi = {
  metadata: (keyword = "") => requestJson<SignalMetadata>(`${ROOT}/metadata?keyword=${encodeURIComponent(keyword)}`),
  settings: () => requestJson<SignalSettings>(ROOT+"/settings"),
  saveSettings: (value: Partial<SignalSettings>) => send<SignalSettings>("/settings", "PUT", Object.fromEntries(
    ["enabled", "ai_profile_id", "global_daily_limit", "max_analysis_per_run", "auto_analyze", "collect_on_monitor"].filter(key => key in value).map(key => [key, value[key as keyof SignalSettings]]))),
  rules: () => requestJson<SignalRule[]>(ROOT+"/rules"),
  createRule: (value: SignalRuleDraft) => send<SignalRule>("/rules", "POST", value),
  updateRule: (id: number, value: Partial<SignalRuleDraft>) => send<SignalRule>(`/rules/${id}`, "PUT", value),
  ruleVersions: (id: number) => requestJson<{ revision: number; config: SignalRuleDraft; created_at: string }[]>(`${ROOT}/rules/${id}/versions`),
  dashboard: () => requestJson<SignalDashboard>(ROOT+"/dashboard"),
  evidence: (offset = 0) => requestJson<SignalPage<SignalEvidence>>(`${ROOT}/evidence?offset=${offset}&limit=50`),
  addEvidence: (value: Record<string, unknown>) => send<SignalEvidence>("/evidence", "POST", value),
  assessments: (offset = 0) => requestJson<SignalPage<SignalAssessment>>(`${ROOT}/assessments?offset=${offset}&limit=50`),
  predictions: (offset = 0) => requestJson<SignalPage<SignalPrediction>>(`${ROOT}/predictions?offset=${offset}&limit=50`),
  recheck: (id: number) => send<SignalPrediction>(`/predictions/${id}/recheck`, "POST"),
  outcomeVersions: (id: number) => requestJson<{ version: number; status: string; result: Record<string, unknown> }[]>(`${ROOT}/predictions/${id}/outcomes`),
  startJob: (value: Record<string, unknown>) => send<SignalJob>("/jobs", "POST", value),
  job: (id: string) => requestJson<SignalJob>(`${ROOT}/jobs/${id}`),
};
